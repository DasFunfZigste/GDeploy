import asyncio
import hashlib
import io
import json
import os
import shutil
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.requests import ClientDisconnect

from gdeploy.db import Database, PackageStateError
from gdeploy.fleetmanager import FleetManager, FleetManagerError, _deb_metadata, validate_license


def license_pem(*, key=None, starts=-1, ends=30, mismatched=False):
    key = key or ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Fleet Manager")])
    current = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(current + timedelta(days=starts)).not_valid_after(current + timedelta(days=ends))
        .sign(key, hashes.SHA256())
    )
    if mismatched:
        key = ec.generate_private_key(ec.SECP256R1())
    return (
        certificate.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    ).decode()


def package_bytes(package="corelight-fleet", architecture="amd64", version="29.2.2-1"):
    return json.dumps({"package": package, "version": version, "architecture": architecture}).encode()


def fake_metadata(path, descriptor):
    data = os.pread(descriptor, 16384, 0)
    try:
        value = json.loads(data)
        if value["architecture"] not in {"amd64", "all"}:
            raise ValueError("architecture")
        return value
    except (ValueError, KeyError) as exc:
        raise FleetManagerError("Choose a valid Debian .deb package for amd64 or all architectures.") from exc


class StreamRequest:
    def __init__(self, body, *, length=None, error=None):
        self.headers = {} if length is None else {"content-length": str(length)}
        self.body, self.error = body, error

    async def stream(self):
        for part in self.body:
            yield part
        if self.error:
            raise self.error


@pytest.fixture
def manager(config, monkeypatch):
    monkeypatch.setattr("gdeploy.fleetmanager._deb_metadata", fake_metadata)
    return FleetManager(Database(config.data_dir, config.secret_key), config)


def upload(manager, body=None, *, checksum=None, filename="corelight-fleet_29.2.2-1_amd64.deb"):
    return asyncio.run(manager.upload(StreamRequest([body or package_bytes()]), filename, checksum))


def settings(**kwargs):
    return {"mode": "online", "community_string": "test-community", "repository_token": "repo-token", "license_pem": license_pem(), **kwargs}


def offline(manager):
    main = upload(manager)["uploaded_package_id"]
    dependency = upload(manager, package_bytes("libexample", "all"), filename="libexample.deb")["uploaded_package_id"]
    manager.save(settings(mode="offline", repository_token="", package_id=main, dependency_ids=[dependency]))
    return manager.selected()


def test_online_settings_are_encrypted_redacted_and_persisted(manager, config):
    payload = settings()
    catalog = manager.save(payload)
    assert catalog["ready"] and catalog["mode"] == "online"
    assert catalog["community_string_configured"] and catalog["repository_token_configured"]
    assert catalog["license"]["sha256"] == hashlib.sha256(payload["license_pem"].encode()).hexdigest()
    for secret in [payload["community_string"], payload["repository_token"], payload["license_pem"]]:
        assert secret not in json.dumps(catalog)
        assert secret.encode() not in manager.db.path.read_bytes()
    assert not list(config.data_dir.glob("*.pem"))
    reopened = FleetManager(Database(config.data_dir, config.secret_key), config)
    assert reopened.validate_snapshot(reopened.selected()) == manager.selected()
    assert reopened.catalog()["license"] == catalog["license"]
    manager.save({"mode": "online", "community_string": "", "repository_token": "", "license_pem": ""})
    assert manager.selected()["license_pem"] == payload["license_pem"]
    assert manager.selected()["community_string"] == payload["community_string"]
    assert not manager.clear()["ready"]
    assert manager.selected() is None


def test_upload_registers_without_selecting_and_optional_hash_is_computed(manager):
    body = package_bytes()
    result = upload(manager, body)
    item = result["packages"][0]
    assert result["uploaded_package_id"] == item["id"]
    assert not result["ready"] and result["package_id"] is None and manager.selected() is None
    assert item["sha256"] == hashlib.sha256(body).hexdigest()
    assert item["package"] == "corelight-fleet" and item["architecture"] == "amd64"
    assert item["can_delete"] and item["available"]
    assert manager.upload_dir.joinpath(item["id"].removeprefix("upload_") + ".deb").stat().st_mode & 0o777 == 0o600
    with manager.db.connect() as connection:
        encrypted = connection.execute("SELECT value FROM fleetmanager_files").fetchone()[0]
    assert item["name"] not in encrypted and item["sha256"] not in encrypted


def test_offline_package_dependency_selection_and_clear_preserves_files(manager):
    saved = offline(manager)
    assert manager.catalog()["ready"] and manager.catalog()["mode"] == "offline"
    assert manager.catalog()["dependency_ids"] == [saved["dependencies"][0]["id"]]
    assert manager.validate_snapshot(saved) == saved
    assert not any(item["can_delete"] for item in manager.catalog()["packages"])
    assert manager.save({"mode": "offline"})["ready"]
    assert manager.selected() == saved
    assert not manager.clear()["ready"]
    assert all(item["can_delete"] for item in manager.catalog()["packages"])
    assert manager.validate_snapshot(saved) == saved


@pytest.mark.parametrize("changes,error", [
    ({"community_string": ""}, "community"),
    ({"community_string": "secret'with-quote"}, "without quotes"),
    ({"community_string": "secret\ncontrol"}, "printable"),
    ({"repository_token": ""}, "repository token"),
    ({"repository_token": "secret token"}, "whitespace"),
    ({"repository_token": "secret:token"}, "colons"),
    ({"license_pem": "invalid-secret-license"}, "certificate"),
    ({"mode": "offline"}, "corelight-fleet"),
])
def test_settings_validation_does_not_persist_or_echo_secrets(manager, changes, error):
    with pytest.raises(FleetManagerError, match=error) as caught:
        manager.save(settings(**changes))
    assert manager.selected() is None
    for value in changes.values():
        if value.startswith("secret") or value == "invalid-secret-license":
            assert value not in str(caught.value)


@pytest.mark.parametrize("kwargs", [{"ends": -1, "starts": -10}, {"starts": 1}, {"mismatched": True}])
def test_expired_future_or_mismatched_license_rejected(manager, kwargs):
    original = manager.save(settings())
    with pytest.raises(FleetManagerError):
        manager.save({"mode": "online", "license_pem": license_pem(**kwargs)})
    assert manager.catalog()["license"] == original["license"]


def test_license_requires_private_key_and_bounded_pem():
    valid = license_pem()
    assert validate_license(valid)["name"] == "corelight-fleetd.pem"
    with pytest.raises(FleetManagerError, match="matching"):
        validate_license(valid.split("-----BEGIN PRIVATE KEY-----")[0])
    with pytest.raises(FleetManagerError, match="64 KiB"):
        validate_license("x" * 65537)
    with pytest.raises(FleetManagerError, match="filename"):
        validate_license(valid, "../secret.pem")


def test_offline_requires_main_and_disallows_dependency_conflicts(manager):
    main = upload(manager)["uploaded_package_id"]
    first = upload(manager, package_bytes("libexample"), filename="first.deb")["uploaded_package_id"]
    second = upload(manager, package_bytes("libexample", version="2.0"), filename="second.deb")["uploaded_package_id"]
    universal_main = upload(manager, package_bytes(architecture="all"), filename="wrong.deb")["uploaded_package_id"]
    for fields in [
        {"package_id": first}, {"package_id": universal_main},
        {"package_id": main, "dependency_ids": [main]},
        {"package_id": main, "dependency_ids": [first, first]},
        {"package_id": main, "dependency_ids": [first, second]},
    ]:
        with pytest.raises(FleetManagerError):
            manager.save(settings(mode="offline", **fields))
    assert manager.selected() is None


def test_server_catalog_metadata_and_validation_are_cached_but_selection_rehashes(manager, config, monkeypatch):
    path = config.ubuntu_iso.with_name("corelight-fleet.deb")
    path.write_bytes(package_bytes())
    catalog = manager.catalog()
    item = catalog["packages"][0]
    assert item["source"] == "server" and not item["can_delete"]
    with monkeypatch.context() as context:
        context.setattr("gdeploy.fleetmanager._deb_metadata", lambda *args: pytest.fail("unnecessary metadata parse"))
        assert manager.catalog()["packages"] == catalog["packages"]
        assert manager.catalog(include_packages=False)["packages"] == []
    manager.save(settings(mode="offline", package_id=item["id"]))
    before = path.stat()
    path.write_bytes(b"x" * before.st_size)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(FleetManagerError, match="SHA-256"):
        manager.validate_snapshot(manager.selected())
    with pytest.raises(FleetManagerError, match="Only Debian packages uploaded"):
        manager.delete(item["id"])


def test_missing_or_changed_package_fails_readiness(manager):
    saved = offline(manager)
    path = Path(saved["package"]["path"])
    path.write_bytes(b"changed")
    assert not manager.catalog()["ready"]
    with pytest.raises(FleetManagerError, match="SHA-256"):
        manager.validate_snapshot(saved)
    path.unlink()
    assert not manager.catalog()["ready"]
    with pytest.raises(FleetManagerError, match="missing or unreadable"):
        manager.validate_snapshot(saved)


def test_invalid_uploads_leave_configuration_and_storage_unchanged(manager, monkeypatch):
    before = manager.save(settings())
    for body, filename, digest in [
        (b"invalid", "broken.deb", None),
        (package_bytes(), "../bad.deb", None),
        (package_bytes(), "bad.rpm", None),
        (package_bytes(), "valid.deb", "0" * 64),
        (package_bytes(), "valid.deb", "invalid-checksum"),
        (package_bytes(architecture="arm64"), "arm.deb", None),
    ]:
        with pytest.raises(FleetManagerError):
            upload(manager, body, filename=filename, checksum=digest)
    assert manager.catalog() == before
    assert not list(manager.upload_dir.glob("*"))
    monkeypatch.setattr("gdeploy.fleetmanager.MAX_UPLOAD_BYTES", 1)
    with pytest.raises(FleetManagerError, match="4 GiB"):
        upload(manager)
    assert not list(manager.upload_dir.glob("*"))


def test_empty_incomplete_disconnected_and_low_space_uploads_are_removed(manager, monkeypatch):
    for request in [StreamRequest([]), StreamRequest([package_bytes()], length=999), StreamRequest([b"part"], error=ClientDisconnect())]:
        with pytest.raises(FleetManagerError):
            asyncio.run(manager.upload(request, "test.deb"))
    assert not list(manager.upload_dir.glob("*"))
    monkeypatch.setattr("gdeploy.fleetmanager.shutil.disk_usage", lambda path: type("Disk", (), {"free": 0})())
    with pytest.raises(FleetManagerError, match="space"):
        upload(manager)
    assert not manager.db.fleetmanager_files()


@pytest.mark.parametrize("status", ["queued", "running", "cleaning"])
def test_main_and_dependency_protected_by_active_job_after_clear(manager, spec, status):
    saved = offline(manager)
    manager.db.create("job", spec, {"fleetmanager": saved})
    manager.db.update("job", status=status)
    manager.clear()
    for item in manager.db.fleetmanager_packages(saved):
        with pytest.raises(PackageStateError, match="queued, running, or cleaning"):
            manager.delete(item["id"])
    assert not any(item["can_delete"] for item in manager.catalog()["packages"])
    assert manager.db.get("job", private=True)["secrets"]["fleetmanager"] == saved


def test_deletion_protection_serializes_with_save_and_job_creation(manager, spec):
    saved = offline(manager)
    with pytest.raises(PackageStateError, match="saved Fleet Manager"):
        manager.delete(saved["package"]["id"])
    manager.clear()
    manager.delete(saved["package"]["id"])
    Path(saved["package"]["path"]).write_bytes(package_bytes())
    with pytest.raises(PackageStateError, match="removed or changed"):
        manager.db.create("job", spec, {"fleetmanager": saved})
    with pytest.raises(PackageStateError, match="removed or changed"):
        manager.db.set_fleetmanager_settings(saved)
    assert manager.db.get("job") is None and manager.selected() is None


def test_failed_job_history_keeps_snapshot_after_package_deleted(manager, spec):
    saved = offline(manager)
    manager.db.create("job", spec, {"fleetmanager": saved})
    manager.db.update("job", status="failed")
    manager.clear()
    for item in manager.db.fleetmanager_packages(saved):
        manager.delete(item["id"])
    assert manager.catalog()["packages"] == []
    assert manager.db.get("job", private=True)["secrets"]["fleetmanager"] == saved


def test_symlink_packages_directory_and_source_are_never_followed(manager, tmp_path, config):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "keep.deb"
    target.write_bytes(package_bytes())
    config.ubuntu_iso.with_name("link.deb").symlink_to(target)
    assert manager.catalog()["packages"] == []
    manager.upload_dir.symlink_to(outside, target_is_directory=True)
    manager.recover_uploads()
    with pytest.raises(FleetManagerError, match="symbolic link"):
        upload(manager)
    assert list(outside.iterdir()) == [target]


def test_delete_rejects_replaced_symlink_and_allows_missing_registration(manager, tmp_path):
    result = upload(manager)
    item = manager.db.fleetmanager_files()[0]
    path = Path(item["path"])
    target = tmp_path / "keep.deb"
    target.write_bytes(b"preserve")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(FleetManagerError, match="symbolic link"):
        manager.delete(item["id"])
    assert target.read_bytes() == b"preserve"
    path.unlink()
    assert manager.delete(result["uploaded_package_id"])["packages"] == []


def test_recovery_cleans_only_own_unregistered_regular_uploads(manager):
    upload(manager)
    for name in ["1" * 32 + ".partial", "2" * 32 + ".deb", "manual.deb"]:
        (manager.upload_dir / name).write_bytes(b"pending")
    manager.recover_uploads()
    assert not (manager.upload_dir / ("1" * 32 + ".partial")).exists()
    assert not (manager.upload_dir / ("2" * 32 + ".deb")).exists()
    assert (manager.upload_dir / "manual.deb").exists()
    assert Path(manager.db.fleetmanager_files()[0]["path"]).exists()


def build_deb(package="corelight-fleet", architecture="amd64"):
    def tar_body(name, data):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        return output.getvalue()

    fields = f"Package: {package}\nVersion: 29.2.2-1\nArchitecture: {architecture}\nMaintainer: Test <test@example.com>\nDescription: Synthetic validation package\n".encode()
    parts = [("debian-binary", b"2.0\n"), ("control.tar.gz", tar_body("./control", fields)), ("data.tar.gz", tar_body("./test", b"fixture"))]
    archive = b"!<arch>\n"
    for name, data in parts:
        header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
        archive += header + data + (b"\n" if len(data) % 2 else b"")
    return archive


@pytest.mark.skipif(not shutil.which("dpkg-deb"), reason="Real Debian control validation runs on Linux CI")
def test_real_dpkg_deb_reads_actual_control_and_rejects_wrong_architecture(config):
    manager = FleetManager(Database(config.data_dir, config.secret_key), config)
    path = config.ubuntu_iso.with_name("real.deb")
    path.write_bytes(build_deb())
    metadata = manager._inspect_package(path)
    assert metadata["package"] == "corelight-fleet" and metadata["architecture"] == "amd64"
    path.write_bytes(build_deb(architecture="arm64"))
    with pytest.raises(FleetManagerError, match="valid Debian"):
        manager._inspect_package(path)
    path.write_bytes(b"not a deb")
    with pytest.raises(FleetManagerError, match="valid Debian"):
        manager._inspect_package(path)


def test_dpkg_failure_does_not_echo_process_output(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise FileNotFoundError("sensitive process details")
    monkeypatch.setattr("gdeploy.fleetmanager.subprocess.run", fail)
    with pytest.raises(FleetManagerError, match="dpkg-deb") as caught:
        _deb_metadata(tmp_path / "test.deb", 0)
    assert "sensitive" not in str(caught.value)


def test_switch_online_ignores_missing_previous_offline_packages(manager):
    saved = offline(manager)
    for item in manager.db.fleetmanager_packages(saved):
        Path(item["path"]).unlink()
    catalog = manager.save({"mode": "online", "repository_token": "new-token"})
    assert catalog["ready"] and catalog["package_id"] is None and catalog["dependency_ids"] == []
    selected = manager.selected()
    assert selected["package"] is None and selected["dependencies"] == []
    assert selected["license_pem"] == saved["license_pem"]
    assert manager.validate_snapshot(selected) == selected
    assert all(item["can_delete"] for item in catalog["packages"])


@pytest.mark.parametrize("key_first", [False, True])
def test_license_supports_either_pem_block_order(key_first):
    pem = license_pem()
    certificate, private_key = pem.split("-----BEGIN PRIVATE KEY-----", 1)
    private_key = "-----BEGIN PRIVATE KEY-----" + private_key
    reordered = private_key + certificate if key_first else certificate + private_key
    assert validate_license(reordered)["sha256"] == hashlib.sha256(reordered.encode()).hexdigest()


def test_license_unsupported_key_algorithm_is_sanitized(monkeypatch):
    from cryptography.exceptions import UnsupportedAlgorithm

    def unsupported(*args, **kwargs):
        raise UnsupportedAlgorithm("sensitive key details")
    monkeypatch.setattr("gdeploy.fleetmanager.serialization.load_pem_private_key", unsupported)
    with pytest.raises(FleetManagerError, match="matching unencrypted private key") as caught:
        validate_license(license_pem())
    assert "sensitive" not in str(caught.value)


def test_license_requires_first_certificate_to_match_key_and_preserves_prior_choice(manager):
    original = manager.save(settings())
    leaf = license_pem()
    unrelated = license_pem().split("-----BEGIN PRIVATE KEY-----", 1)[0]
    with pytest.raises(FleetManagerError, match="first certificate.*identity leaf matching"):
        manager.save({"mode": "online", "license_pem": unrelated + leaf})
    assert manager.catalog()["license"] == original["license"]
    # A following chain certificate is valid input; key/cert file block order is independent.
    assert validate_license(leaf + unrelated)["sha256"] == hashlib.sha256((leaf + unrelated).encode()).hexdigest()
