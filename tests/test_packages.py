import asyncio
import hashlib
import io
import os
import tarfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import ClientDisconnect

from gdeploy.db import Database, PackageStateError
from gdeploy.packages import FREE_SPACE_RESERVE, MAX_UPLOAD_BYTES, PackageError, SplunkPackageManager


def package_bytes(*, machine=62, root="splunk", extra=None):
    output = io.BytesIO()
    header = bytearray(20)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = machine.to_bytes(2, "little")
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in ((f"{root}/bin/splunk", b"launcher"), (f"{root}/bin/splunkd", bytes(header))):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        if extra is not None:
            archive.addfile(extra)
    return output.getvalue()


class StreamRequest:
    def __init__(self, chunks, *, length=None, error=None):
        self.headers = {} if length is None else {"content-length": str(length)}
        self.chunks = chunks
        self.error = error

    async def stream(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error


def upload(manager, name="splunk-10.0.0-linux-amd64.tgz", body=None):
    body = body or package_bytes()
    return asyncio.run(manager.upload(StreamRequest([body]), name, hashlib.sha256(body).hexdigest()))


def write_package(path, **kwargs):
    body = package_bytes(**kwargs)
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


@pytest.fixture
def manager(config):
    return SplunkPackageManager(Database(config.data_dir, config.secret_key), config)


def test_native_server_filename_selection_persists_independently(manager, config):
    path = config.splunk_package.with_name("splunk-10.0.0-build-Linux-x86_64.tgz")
    checksum = write_package(path)
    manager.db.set_settings({"host": "esxi.invalid", "password": "keep-esxi-secret"})
    assert not manager.catalog()["ready"]
    item = manager.catalog()["items"][0]
    assert item["name"] == path.name
    assert not item["can_delete"]
    assert manager.select(item["id"], checksum.upper())["ready"]
    reopened = SplunkPackageManager(Database(config.data_dir, config.secret_key), config)
    assert reopened.selected()["name"] == path.name
    assert reopened.validate_snapshot(reopened.selected()) == path
    assert reopened.db.media_settings() is None
    assert reopened.db.settings()["password"] == "keep-esxi-secret"
    with reopened.db.connect() as connection:
        encrypted = connection.execute("SELECT value FROM splunk_package_settings").fetchone()[0]
    assert path.name not in encrypted
    assert checksum not in encrypted


def test_env_fallback_and_saved_selection_are_independent(manager, config):
    checksum = write_package(config.splunk_package)
    manager = SplunkPackageManager(manager.db, replace(config, splunk_sha256=checksum))
    legacy = manager.legacy()
    assert manager.selected() == legacy
    assert manager.catalog()["ready"]
    assert not manager.catalog()["has_saved_selection"]
    selected = upload(manager)["selected"]
    assert manager.selected() == selected
    assert manager.legacy() == legacy
    assert manager.clear_selection()["selected"] == legacy
    assert manager.validate_snapshot(selected).exists()


@pytest.mark.parametrize("checksum", ["", "a" * 63, "z" * 64, "../../etc/passwd", None])
def test_selection_requires_publisher_checksum(manager, config, checksum):
    write_package(config.splunk_package)
    with pytest.raises(PackageError, match="publisher"):
        manager.select(manager.catalog()["items"][0]["id"], checksum)
    assert manager.db.splunk_package_settings() is None


def test_checksum_mismatch_preserves_prior_selection(manager, config):
    before = upload(manager)["selected"]
    write_package(config.splunk_package)
    with pytest.raises(PackageError, match="does not match"):
        manager.select(manager._server_id(config.splunk_package), "0" * 64)
    assert manager.selected() == before


@pytest.mark.parametrize("body", [b"not a tar archive", package_bytes(machine=183), package_bytes(root="splunkforwarder")])
def test_package_must_be_enterprise_linux_x86_64(manager, config, body):
    config.splunk_package.write_bytes(body)
    with pytest.raises(PackageError, match="Splunk"):
        manager.select(manager.catalog()["items"][0]["id"], hashlib.sha256(body).hexdigest())
    assert manager.db.splunk_package_settings() is None


@pytest.mark.parametrize("unsafe", ["../../outside", "/etc/passwd", "splunk/../../outside"])
def test_archive_traversal_is_rejected_before_selection(manager, unsafe):
    body = package_bytes(extra=tarfile.TarInfo(unsafe))
    with pytest.raises(PackageError, match="unsafe file path"):
        upload(manager, body=body)
    assert manager.db.splunk_package_settings() is None
    assert list(manager.upload_dir.iterdir()) == []


def test_archive_link_outside_installation_is_rejected(manager):
    link = tarfile.TarInfo("splunk/bin/unsafe")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    with pytest.raises(PackageError, match="link outside"):
        upload(manager, body=package_bytes(extra=link))


def test_truncated_gzip_has_actionable_error_and_discards_upload(manager):
    body = package_bytes()[:30]
    with pytest.raises(PackageError, match="Splunk"):
        upload(manager, body=body)
    assert list(manager.upload_dir.iterdir()) == []


def test_changed_and_missing_package_fail_readiness_and_reverification(manager):
    selected = upload(manager)["selected"]
    path = Path(selected["path"])
    path.write_bytes(b"changed")
    assert not manager.catalog()["ready"]
    with pytest.raises(PackageError, match="does not match"):
        manager.validate_snapshot(selected)
    path.unlink()
    assert not manager.catalog()["ready"]
    assert not manager.catalog()["items"][0]["available"]
    with pytest.raises(PackageError, match="missing or unreadable"):
        manager.validate_snapshot(selected)


def test_unchanged_metadata_does_not_bypass_digest_validation(manager):
    selected = upload(manager)["selected"]
    path = Path(selected["path"])
    before = path.stat()
    path.write_bytes(b"x" * before.st_size)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert manager.catalog()["ready"]  # Status is inexpensive; deployment checks the full digest.
    with pytest.raises(PackageError, match="does not match"):
        manager.validate_snapshot(selected)


def test_digest_and_archive_inspection_use_same_open_file(manager, config, monkeypatch):
    checksum = write_package(config.splunk_package)
    from gdeploy.guest import _validate_splunk_archive

    def replace_path_then_validate(path, *, fileobj=None):
        assert fileobj is not None
        original = path.with_name("original.tgz")
        path.rename(original)
        path.write_bytes(b"replacement")
        _validate_splunk_archive(path, fileobj=fileobj)

    monkeypatch.setattr("gdeploy.packages._validate_splunk_archive", replace_path_then_validate)
    with pytest.raises(PackageError, match="changed during verification"):
        manager.select(manager._server_id(config.splunk_package), checksum)


def test_symlinks_and_unlisted_filesystem_paths_are_rejected(manager, config, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "private.tgz"
    checksum = write_package(target)
    config.splunk_package.symlink_to(target)
    assert manager.catalog()["items"] == []
    assert manager.selected() is None
    for identifier in (str(target), "../../outside/private.tgz", manager._server_id(target)):
        with pytest.raises(PackageError) as error:
            manager.select(identifier, checksum)
        assert error.value.status_code == 404
    with pytest.raises(PackageError, match="outside the managed"):
        manager.validate_snapshot(manager._snapshot(target, sha256=checksum))


def test_filesystem_symlink_swap_after_selection_is_rejected(manager, tmp_path):
    selected = upload(manager)["selected"]
    path = Path(selected["path"])
    outside = tmp_path / "outside.tgz"
    write_package(outside)
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(PackageError, match="symbolic link"):
        manager.validate_snapshot(selected)


def test_chunked_upload_retains_native_name_and_survives_restart(manager, config):
    body = package_bytes()
    checksum = hashlib.sha256(body).hexdigest()
    result = asyncio.run(manager.upload(StreamRequest([body[:30], body[30:]]), "splunk-10.0.0-linux-x86_64.tgz", checksum))
    assert result["ready"] and result["has_saved_selection"]
    assert result["selected"]["name"] == "splunk-10.0.0-linux-x86_64.tgz"
    assert result["selected"]["source"] == "upload"
    assert result["items"][0]["sha256"] == checksum
    path = manager.validate_snapshot(result["selected"])
    assert path.parent == config.data_dir / "packages"
    assert path.name != result["selected"]["name"]
    assert path.stat().st_mode & 0o077 == 0
    reopened = SplunkPackageManager(Database(config.data_dir, config.secret_key), config)
    assert reopened.catalog() == result
    assert not list(path.parent.glob("*.partial"))


def test_same_name_reupload_never_overwrites_queued_snapshot(manager, spec):
    first = upload(manager)["selected"]
    manager.db.create("first", spec, {"splunk_package": first})
    second = upload(manager)["selected"]
    assert first["path"] != second["path"]
    assert manager.validate_snapshot(first).exists()
    assert manager.db.get("first", private=True)["secrets"]["splunk_package"] == first
    assert len(manager.catalog()["items"]) == 2


@pytest.mark.parametrize("filename", ["../installer.tgz", "/installer.tgz", "folder\\installer.tgz", "bad\n.tgz", "bad.deb", ".tgz"])
def test_upload_rejects_unsafe_or_wrong_filenames_before_writing(manager, filename):
    with pytest.raises(PackageError, match="filename"):
        asyncio.run(manager.upload(StreamRequest([]), filename, "0" * 64))
    assert not manager.upload_dir.exists()


@pytest.mark.parametrize("error", [ClientDisconnect(), asyncio.CancelledError(), RuntimeError("unexpected stop")])
def test_disconnected_or_cancelled_upload_removes_partial_files(manager, error):
    body = package_bytes()
    expected_error = PackageError if isinstance(error, ClientDisconnect) else type(error)
    with pytest.raises(expected_error):
        asyncio.run(manager.upload(StreamRequest([body[:20]], error=error), "splunk.tgz", hashlib.sha256(body).hexdigest()))
    assert list(manager.upload_dir.iterdir()) == []
    assert manager.db.splunk_package_settings() is None
    assert manager.db.splunk_package_files() == []


@pytest.mark.parametrize("length", [10, 100000])
def test_upload_rejects_misdeclared_size(manager, length):
    body = package_bytes()
    with pytest.raises(PackageError, match="Content-Length|incomplete"):
        asyncio.run(manager.upload(StreamRequest([body], length=length), "splunk.tgz", hashlib.sha256(body).hexdigest()))
    assert list(manager.upload_dir.iterdir()) == []


@pytest.mark.parametrize("length", [MAX_UPLOAD_BYTES + 1, -1, 0, "invalid"])
def test_invalid_size_rejected_before_reading_body(manager, length):
    with pytest.raises(PackageError):
        asyncio.run(manager.upload(StreamRequest([], length=length, error=AssertionError("Body read")), "splunk.tgz", "0" * 64))
    assert not manager.upload_dir.exists()


def test_chunked_size_limit_cannot_be_bypassed(manager, monkeypatch):
    monkeypatch.setattr("gdeploy.packages.MAX_UPLOAD_BYTES", 20)
    with pytest.raises(PackageError) as error:
        asyncio.run(manager.upload(StreamRequest([b"x" * 15, b"x" * 15]), "splunk.tgz", "0" * 64))
    assert error.value.status_code == 413
    assert list(manager.upload_dir.iterdir()) == []


def test_space_check_prevents_upload_and_preserves_prior_selection(manager, monkeypatch):
    before = upload(manager)["selected"]
    monkeypatch.setattr("gdeploy.packages.shutil.disk_usage", lambda _: SimpleNamespace(free=FREE_SPACE_RESERVE))
    with pytest.raises(PackageError) as error:
        asyncio.run(manager.upload(StreamRequest([], length=100), "splunk.tgz", "0" * 64))
    assert error.value.status_code == 507
    assert manager.selected() == before


def test_free_space_is_rechecked_during_streaming(manager, monkeypatch):
    checks = iter([SimpleNamespace(free=FREE_SPACE_RESERVE + 100), SimpleNamespace(free=FREE_SPACE_RESERVE)])
    monkeypatch.setattr("gdeploy.packages.shutil.disk_usage", lambda _: next(checks))
    with pytest.raises(PackageError) as error:
        asyncio.run(manager.upload(StreamRequest([b"x" * 20]), "splunk.tgz", "0" * 64))
    assert error.value.status_code == 507
    assert list(manager.upload_dir.iterdir()) == []


def test_empty_chunked_upload_is_rejected(manager):
    with pytest.raises(PackageError, match="nonempty"):
        asyncio.run(manager.upload(StreamRequest([]), "splunk.tgz", hashlib.sha256(b"").hexdigest()))
    assert list(manager.upload_dir.iterdir()) == []


def test_selected_upload_cannot_be_deleted(manager):
    selected = upload(manager)["selected"]
    assert not manager.catalog()["items"][0]["can_delete"]
    with pytest.raises(PackageStateError, match="saved selection"):
        manager.delete(selected["id"])
    assert manager.validate_snapshot(selected).exists()
    result = manager.clear_selection()
    assert result["items"][0]["can_delete"]
    assert manager.delete(selected["id"])["items"] == []


@pytest.mark.parametrize("status", ["queued", "running", "cleaning"])
def test_active_job_package_cannot_be_deleted(manager, spec, status):
    selected = upload(manager)["selected"]
    manager.db.create("job", spec, {"splunk_package": selected})
    manager.db.update("job", status=status)
    manager.clear_selection()
    with pytest.raises(PackageStateError, match="queued, running, or cleaning"):
        manager.delete(selected["id"])
    assert not manager.catalog()["items"][0]["can_delete"]
    assert manager.validate_snapshot(selected).exists()


@pytest.mark.parametrize("status", ["failed", "completed", "cleaned"])
def test_inactive_job_does_not_block_unselected_package_deletion(manager, spec, status):
    selected = upload(manager)["selected"]
    manager.db.create("job", spec, {"splunk_package": selected})
    manager.db.update("job", status=status)
    manager.clear_selection()
    assert manager.delete(selected["id"])["items"] == []
    assert manager.db.get("job", private=True)["secrets"]["splunk_package"] == selected


def test_queue_and_selection_reject_upload_deleted_after_preflight(manager, spec):
    selected = upload(manager)["selected"]
    manager.clear_selection()
    manager.delete(selected["id"])
    # Even replacing the file cannot resurrect its deleted registration.
    Path(selected["path"]).write_bytes(package_bytes())
    with pytest.raises(PackageStateError, match="removed or changed"):
        manager.db.create("job", spec, {"splunk_package": selected})
    with pytest.raises(PackageStateError, match="removed or changed"):
        manager.db.set_splunk_package_settings(selected)
    assert manager.db.get("job") is None


def test_delete_never_removes_server_mounted_package(manager, config):
    write_package(config.splunk_package)
    with pytest.raises(PackageError, match="Only Splunk packages uploaded"):
        manager.delete(manager._server_id(config.splunk_package))
    assert config.splunk_package.exists()


def test_delete_can_remove_missing_unselected_registry_entry(manager):
    selected = upload(manager)["selected"]
    manager.clear_selection()
    Path(selected["path"]).unlink()
    assert manager.delete(selected["id"])["items"] == []


def test_delete_does_not_follow_replaced_file_link(manager, tmp_path):
    selected = upload(manager)["selected"]
    manager.clear_selection()
    outside = tmp_path / "keep-this-file"
    outside.write_text("preserve")
    path = Path(selected["path"])
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(PackageError, match="symbolic link"):
        manager.delete(selected["id"])
    assert outside.read_text() == "preserve"
    assert manager.db.splunk_package_files()


def test_recovery_cleans_abandoned_uploads_only(manager, tmp_path):
    selected = upload(manager)["selected"]
    partial = manager.upload_dir / ("1" * 32 + ".partial")
    orphaned = manager.upload_dir / ("2" * 32 + ".tgz")
    unknown = manager.upload_dir / "manual.tgz"
    directory = manager.upload_dir / ("3" * 32 + ".partial")
    directory.mkdir()
    outside = tmp_path / "keep"
    outside.write_text("preserve")
    link = manager.upload_dir / ("4" * 32 + ".tgz")
    link.symlink_to(outside)
    for path in (partial, orphaned, unknown):
        path.write_bytes(b"test")
    manager.recover_uploads()
    assert not partial.exists() and not orphaned.exists()
    assert unknown.exists() and directory.is_dir()
    assert link.is_symlink() and outside.read_text() == "preserve"
    assert manager.validate_snapshot(selected).exists()


def test_upload_and_recovery_never_follow_packages_directory_link(manager, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / ("1" * 32 + ".partial")
    target.write_text("preserve")
    manager.upload_dir.symlink_to(outside, target_is_directory=True)
    manager.recover_uploads()
    with pytest.raises(PackageError, match="symbolic link"):
        upload(manager)
    assert list(outside.iterdir()) == [target]
