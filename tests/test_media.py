import asyncio
import hashlib
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest
from starlette.requests import ClientDisconnect

from gdeploy.config import Config
from gdeploy.db import Database
from gdeploy.media import FREE_SPACE_RESERVE, MAX_UPLOAD_BYTES, MediaError, MediaManager


def iso_bytes(payload=b"test installer"):
    return b"\0" * (16 * 2048) + b"\x01CD001\x01" + payload


def write_iso(path, payload=b"test installer"):
    contents = iso_bytes(payload)
    path.write_bytes(contents)
    return hashlib.sha256(contents).hexdigest()


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


@pytest.fixture
def manager(config):
    return MediaManager(Database(config.data_dir, config.secret_key), config)


def test_server_selection_is_checked_persistent_and_separate_from_esxi(manager, config):
    expected = write_iso(config.ubuntu_iso)
    manager.db.set_settings({"host": "esxi.invalid", "password": "preserve-esxi-secret"})
    catalog = manager.catalog()
    assert not catalog["ready"]
    assert catalog["items"][0]["name"] == "ubuntu.iso"
    selection = manager.select(catalog["items"][0]["id"], expected.upper())
    assert selection["ready"]
    reopened = MediaManager(Database(config.data_dir, config.secret_key), config)
    assert reopened.selected()["sha256"] == expected
    assert reopened.validate_snapshot(reopened.selected()) == config.ubuntu_iso
    assert reopened.db.settings()["password"] == "preserve-esxi-secret"


def test_legacy_fallback_does_not_override_saved_selection(manager, config):
    first_checksum = write_iso(config.ubuntu_iso)
    manager = MediaManager(manager.db, replace(config, ubuntu_sha256=first_checksum))
    legacy = manager.legacy()
    assert legacy == manager.selected()
    second = config.ubuntu_iso.with_name("new.iso")
    checksum = write_iso(second, b"another installer")
    item = next(item for item in manager.catalog()["items"] if item["name"] == "new.iso")
    manager.select(item["id"], checksum)
    assert manager.selected()["name"] == "new.iso"
    assert manager.legacy() == legacy
    assert manager.validate_snapshot(legacy) == config.ubuntu_iso


@pytest.mark.parametrize("checksum", ["", "a" * 63, "g" * 64, "../../etc/passwd", None])
def test_selection_requires_complete_publisher_checksum(manager, config, checksum):
    write_iso(config.ubuntu_iso)
    media_id = manager.catalog()["items"][0]["id"]
    with pytest.raises(MediaError, match="publisher"):
        manager.select(media_id, checksum)
    assert manager.db.media_settings() is None


def test_checksum_mismatch_preserves_previous_selection(manager, config):
    checksum = write_iso(config.ubuntu_iso)
    media_id = manager.catalog()["items"][0]["id"]
    manager.select(media_id, checksum)
    selected = manager.selected()
    with pytest.raises(MediaError, match="does not match"):
        manager.select(media_id, "0" * 64)
    assert manager.selected() == selected


def test_iso_extension_alone_does_not_approve_file(manager, config):
    config.ubuntu_iso.write_bytes(b"not an ISO file")
    checksum = hashlib.sha256(config.ubuntu_iso.read_bytes()).hexdigest()
    with pytest.raises(MediaError, match="not a valid ISO"):
        manager.select(manager.catalog()["items"][0]["id"], checksum)


def test_deleted_and_changed_media_are_not_ready(manager, config):
    checksum = write_iso(config.ubuntu_iso)
    manager.select(manager.catalog()["items"][0]["id"], checksum)
    config.ubuntu_iso.write_bytes(iso_bytes(b"changed bytes"))
    assert not manager.catalog()["ready"]
    config.ubuntu_iso.unlink()
    assert manager.selected() is not None
    assert not manager.catalog()["ready"]
    with pytest.raises(MediaError, match="missing or unreadable"):
        manager.validate_snapshot(manager.selected())


def test_full_validation_detects_tampering_even_with_identical_stat_metadata(manager, config):
    checksum = write_iso(config.ubuntu_iso, b"original")
    manager.select(manager.catalog()["items"][0]["id"], checksum)
    before = config.ubuntu_iso.stat()
    config.ubuntu_iso.write_bytes(iso_bytes(b"tampered"))
    os.utime(config.ubuntu_iso, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert manager.catalog()["ready"]  # The inexpensive status is not an integrity approval.
    with pytest.raises(MediaError, match="does not match"):
        manager.validate_snapshot(manager.selected())


def test_catalog_does_not_follow_symlinks_or_accept_arbitrary_ids(manager, config, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "private.iso"
    checksum = write_iso(target)
    config.ubuntu_iso.symlink_to(target)
    assert manager.catalog()["items"] == []
    assert manager.selected() is None
    for identifier in (str(target), "../../outside/private.iso", manager._server_id(target)):
        with pytest.raises(MediaError) as exc:
            manager.select(identifier, checksum)
        assert exc.value.status_code == 404


def test_queued_snapshot_cannot_point_outside_managed_roots(manager, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "private.iso"
    checksum = write_iso(target)
    snapshot = manager._snapshot(target, sha256=checksum)
    with pytest.raises(MediaError, match="outside the managed"):
        manager.validate_snapshot(snapshot)


def test_symlink_swapped_after_selection_fails_validation(manager, config, tmp_path):
    checksum = write_iso(config.ubuntu_iso)
    manager.select(manager.catalog()["items"][0]["id"], checksum)
    target = tmp_path / "replacement.iso"
    write_iso(target)
    config.ubuntu_iso.unlink()
    config.ubuntu_iso.symlink_to(target)
    with pytest.raises(MediaError, match="symbolic link"):
        manager.validate_snapshot(manager.selected())


def test_chunked_upload_is_selected_and_survives_restart(manager, config):
    body = iso_bytes()
    request = StreamRequest([body[:20], body[20:32000], body[32000:]])
    checksum = hashlib.sha256(body).hexdigest()
    result = asyncio.run(manager.upload(request, "Installer image.iso", checksum))
    assert result["ready"]
    assert result["selected"]["name"] == "Installer image.iso"
    assert result["selected"]["source"] == "upload"
    assert result["items"][0]["sha256"] == checksum
    path = manager.validate_snapshot(result["selected"])
    assert path.parent == config.data_dir / "media"
    assert path.name != "Installer image.iso"
    assert path.stat().st_mode & 0o077 == 0
    reopened = MediaManager(Database(config.data_dir, config.secret_key), config)
    assert reopened.catalog() == result
    assert not list(path.parent.glob("*.partial"))


def test_reupload_uses_new_immutable_path_and_old_snapshot_still_works(manager):
    body = iso_bytes()
    checksum = hashlib.sha256(body).hexdigest()
    first = asyncio.run(manager.upload(StreamRequest([body]), "installer.iso", checksum))["selected"]
    second = asyncio.run(manager.upload(StreamRequest([body]), "installer.iso", checksum))["selected"]
    assert first["path"] != second["path"]
    assert manager.validate_snapshot(first).read_bytes() == body
    assert len(manager.catalog()["items"]) == 2


def test_startup_recovery_removes_only_abandoned_managed_upload_files(manager, tmp_path):
    body = iso_bytes()
    checksum = hashlib.sha256(body).hexdigest()
    first = asyncio.run(manager.upload(StreamRequest([body]), "first.iso", checksum))["selected"]
    second = asyncio.run(manager.upload(StreamRequest([body]), "second.iso", checksum))["selected"]
    partial = manager.upload_dir / ("1" * 32 + ".partial")
    orphaned = manager.upload_dir / ("2" * 32 + ".iso")
    unknown = manager.upload_dir / "manual-install.iso"
    unknown_partial = manager.upload_dir / "manual.partial"
    directory = manager.upload_dir / ("3" * 32 + ".partial")
    directory.mkdir()
    target = tmp_path / "keep-this-file"
    target.write_bytes(body)
    link = manager.upload_dir / ("4" * 32 + ".iso")
    link.symlink_to(target)
    for path in (partial, orphaned, unknown, unknown_partial):
        path.write_bytes(body)
    manager.recover_uploads()
    assert not partial.exists()
    assert not orphaned.exists()
    assert unknown.read_bytes() == body
    assert unknown_partial.read_bytes() == body
    assert directory.is_dir()
    assert link.is_symlink()
    assert target.read_bytes() == body
    assert manager.validate_snapshot(first).exists()
    assert manager.validate_snapshot(second).exists()
    assert len(manager.db.media_files()) == 2


def test_startup_recovery_never_follows_media_directory_link(manager, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / ("1" * 32 + ".partial")
    target.write_text("preserve")
    manager.upload_dir.symlink_to(outside, target_is_directory=True)
    manager.recover_uploads()
    assert target.read_text() == "preserve"


@pytest.mark.parametrize("filename", ["../installer.iso", "/installer.iso", "folder\\installer.iso", "bad\n.iso", "bad.img", ".iso"])
def test_upload_rejects_unsafe_filenames_before_writing(manager, filename):
    with pytest.raises(MediaError, match="filename"):
        asyncio.run(manager.upload(StreamRequest([iso_bytes()]), filename, "0" * 64))
    assert not manager.upload_dir.exists()


def test_bad_upload_preserves_selection_and_removes_partial_files(manager, config):
    checksum = write_iso(config.ubuntu_iso)
    manager.select(manager.catalog()["items"][0]["id"], checksum)
    before = manager.selected()
    with pytest.raises(MediaError, match="does not match"):
        asyncio.run(manager.upload(StreamRequest([iso_bytes()]), "installer.iso", "0" * 64))
    assert manager.selected() == before
    assert list(manager.upload_dir.iterdir()) == []
    assert manager.db.media_files() == []


@pytest.mark.parametrize("error", [ClientDisconnect(), asyncio.CancelledError(), RuntimeError("unexpected stop")])
def test_interrupted_uploads_remove_partial_files(manager, error):
    body = iso_bytes()
    request = StreamRequest([body[:20]], error=error)
    expected_error = MediaError if isinstance(error, ClientDisconnect) else type(error)
    with pytest.raises(expected_error):
        asyncio.run(manager.upload(request, "installer.iso", hashlib.sha256(body).hexdigest()))
    assert list(manager.upload_dir.iterdir()) == []
    assert manager.db.media_files() == []
    assert manager.db.media_settings() is None


@pytest.mark.parametrize("length", [10, 100000])
def test_upload_rejects_incomplete_or_misdeclared_size(manager, length):
    body = iso_bytes()
    with pytest.raises(MediaError, match="Content-Length|incomplete"):
        asyncio.run(manager.upload(StreamRequest([body], length=length), "installer.iso", hashlib.sha256(body).hexdigest()))
    assert list(manager.upload_dir.iterdir()) == []


def test_oversized_upload_rejected_before_body_is_read(manager):
    request = StreamRequest([], length=MAX_UPLOAD_BYTES + 1, error=AssertionError("Body should not be read"))
    with pytest.raises(MediaError) as exc:
        asyncio.run(manager.upload(request, "installer.iso", "0" * 64))
    assert exc.value.status_code == 413
    assert not manager.upload_dir.exists()


def test_chunked_upload_limit_cannot_be_bypassed_without_content_length(manager, monkeypatch):
    monkeypatch.setattr("gdeploy.media.MAX_UPLOAD_BYTES", 20)
    with pytest.raises(MediaError) as exc:
        asyncio.run(manager.upload(StreamRequest([b"x" * 15, b"x" * 15]), "installer.iso", "0" * 64))
    assert exc.value.status_code == 413
    assert list(manager.upload_dir.iterdir()) == []


def test_disk_space_checked_before_upload(manager, monkeypatch):
    monkeypatch.setattr("gdeploy.media.shutil.disk_usage", lambda _: SimpleNamespace(free=FREE_SPACE_RESERVE))
    with pytest.raises(MediaError) as exc:
        asyncio.run(manager.upload(StreamRequest([], length=100), "installer.iso", "0" * 64))
    assert exc.value.status_code == 507
    assert list(manager.upload_dir.iterdir()) == []


def test_chunked_upload_rechecks_available_disk_space(manager, monkeypatch):
    readings = iter([FREE_SPACE_RESERVE + 100, FREE_SPACE_RESERVE])
    monkeypatch.setattr("gdeploy.media.shutil.disk_usage", lambda _: SimpleNamespace(free=next(readings)))
    with pytest.raises(MediaError) as exc:
        asyncio.run(manager.upload(StreamRequest([b"data"]), "installer.iso", "0" * 64))
    assert exc.value.status_code == 507
    assert list(manager.upload_dir.iterdir()) == []


def test_upload_directory_symlink_is_rejected(manager, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    manager.upload_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(MediaError, match="symbolic link"):
        asyncio.run(manager.upload(StreamRequest([]), "installer.iso", "0" * 64))
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("use_generic", [False, True])
def test_environment_aliases_preserve_legacy_configuration(config, monkeypatch, use_generic):
    monkeypatch.setenv("GDEPLOY_DATA_DIR", str(config.data_dir))
    monkeypatch.setenv("GDEPLOY_SECRET_KEY", config.secret_key)
    monkeypatch.setenv("GDEPLOY_ADMIN_PASSWORD_HASH", config.admin_password_hash)
    monkeypatch.setenv("GDEPLOY_UBUNTU_ISO", "/media/old.iso")
    monkeypatch.setenv("GDEPLOY_UBUNTU_SHA256", "a" * 64)
    if use_generic:
        monkeypatch.setenv("GDEPLOY_OS_ISO", "/media/new.iso")
        monkeypatch.setenv("GDEPLOY_OS_SHA256", "B" * 64)
    else:
        monkeypatch.delenv("GDEPLOY_OS_ISO", raising=False)
        monkeypatch.delenv("GDEPLOY_OS_SHA256", raising=False)
    loaded = Config.from_env()
    assert str(loaded.ubuntu_iso) == ("/media/new.iso" if use_generic else "/media/old.iso")
    assert loaded.ubuntu_sha256 == ("b" * 64 if use_generic else "a" * 64)
