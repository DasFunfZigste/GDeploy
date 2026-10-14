import copy
import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pyVmomi import vim

from gdeploy import alma_guest, guest
from gdeploy.config import Config
from gdeploy.db import Database
from gdeploy.main import create_app
from gdeploy.media import MediaError, MediaManager
from gdeploy.os_profiles import ALMA10, UBUNTU
from gdeploy.service import DeploymentError, DeploymentService
from gdeploy.vmware import ESXiClient


TREEINFO = """[general]
family = AlmaLinux
arch = x86_64
[release]
name = AlmaLinux
version = 10.0
[tree]
arch = x86_64
[images-x86_64]
kernel = images/pxeboot/vmlinuz
initrd = images/pxeboot/initrd.img
[stage2]
mainimage = images/install.img
[variant-BaseOS]
repository = BaseOS
[variant-AppStream]
repository = AppStream
"""


def iso_body(marker=b"ALMA"):
    data = bytearray(40 * 2048)
    data[:len(marker)] = marker
    data[16 * 2048:16 * 2048 + 7] = b"\x01CD001\x01"
    return bytes(data)


def write_iso(path, marker=b"ALMA"):
    data = iso_body(marker)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def choose(manager, path, checksum, profile):
    candidate = next(item for item in manager.catalog()["items"] if item["name"] == path.name)
    return manager.select(candidate["id"], checksum, profile)["selected"]


@pytest.fixture
def alma_inspection(monkeypatch):
    """Real profile/parser integration with xorriso's extraction boundary modeled."""
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        source = Path(command[command.index("-indev") + 1])
        assert kwargs["check"] is True
        if "-extract" in command:
            index = command.index("-extract")
            assert command[index + 1] == "/.treeinfo"
            if not source.read_bytes().startswith(b"ALMA"):
                raise subprocess.CalledProcessError(5, command, stderr=b"Cannot find /.treeinfo")
            Path(command[index + 2]).write_text(TREEINFO)
        else:
            assert "-ls" in command
            assert all(path in command for path in alma_guest.DVD_FILES)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(alma_guest.subprocess, "run", run)
    return commands


class InventoryClient:
    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def inventory(self):
        return {
            "host": {"cpu_threads": 32, "memory_gb": 128},
            "vms": [], "datastores": [{"name": "datastore1", "free_gb": 1000}],
            "networks": [{"name": "VM Network"}],
        }


def test_api_advertises_both_profiles_and_keeps_ubuntu_default(signed_in):
    catalog = signed_in.get("/api/settings/media").json()
    assert {profile["id"] for profile in catalog["profiles"]} == {UBUNTU, ALMA10}
    assert catalog["profile"] == UBUNTU
    assert signed_in.get("/api/settings").json()["os_profile"] == UBUNTU


def test_select_alma_server_iso_validates_metadata_and_persists_across_restart(signed_in, config, alma_inspection):
    checksum = write_iso(config.ubuntu_iso)
    catalog = signed_in.get("/api/settings/media").json()
    response = signed_in.put("/api/settings/media", json={
        "media_id": catalog["items"][0]["id"], "sha256": checksum, "profile": ALMA10,
    })
    assert response.status_code == 200
    saved = response.json()
    assert saved["profile"] == saved["selected"]["profile"] == ALMA10
    assert next(item for item in saved["items"] if item["selected"])["profile"] == ALMA10
    assert signed_in.get("/api/settings").json()["os_profile"] == ALMA10
    assert signed_in.app.state.db.media_settings()["profile"] == ALMA10
    assert config.data_dir.joinpath("artifacts").is_dir()
    assert alma_inspection
    with TestClient(create_app(config, start_worker=False)) as restarted:
        restarted.post("/api/login", json={"username": "admin", "password": "test-admin-passphrase"})
        assert restarted.get("/api/settings/media").json() == saved
        assert restarted.get("/api/settings").json()["os_profile"] == ALMA10


def test_server_reselection_without_profile_keeps_verified_alma_selection(config, alma_inspection):
    checksum = write_iso(config.ubuntu_iso)
    db = Database(config.data_dir, config.secret_key)
    manager = MediaManager(db, config)
    original = choose(manager, config.ubuntu_iso, checksum, ALMA10)
    result = manager.select(original["id"], checksum)
    assert result["selected"]["profile"] == ALMA10
    assert result["profile"] == ALMA10


def test_legacy_environment_alma_profile_uses_generic_iso_and_checksum(config, monkeypatch, alma_inspection):
    checksum = write_iso(config.ubuntu_iso)
    monkeypatch.setenv("GDEPLOY_DATA_DIR", str(config.data_dir))
    monkeypatch.setenv("GDEPLOY_SECRET_KEY", config.secret_key)
    monkeypatch.setenv("GDEPLOY_ADMIN_PASSWORD_HASH", config.admin_password_hash)
    monkeypatch.setenv("GDEPLOY_OS_ISO", str(config.ubuntu_iso))
    monkeypatch.setenv("GDEPLOY_OS_SHA256", checksum)
    monkeypatch.setenv("GDEPLOY_OS_PROFILE", ALMA10)
    loaded = Config.from_env()
    assert loaded.os_profile == ALMA10
    assert loaded.ubuntu_iso == config.ubuntu_iso
    manager = MediaManager(Database(loaded.data_dir, loaded.secret_key), loaded)
    assert manager.legacy()["profile"] == ALMA10
    assert manager.validate_snapshot(manager.legacy()) == config.ubuntu_iso
    with pytest.raises(ValueError, match="GDEPLOY_OS_PROFILE"):
        replace(config, os_profile="not-an-installer")


@pytest.mark.parametrize("source", ["upload", "esxi"])
def test_managed_alma_iso_keeps_profile_when_listed_and_reselected(signed_in, config, alma_inspection, source):
    if source == "upload":
        body = iso_body()
        response = signed_in.post("/api/settings/media/upload", params={
            "filename": "AlmaLinux-10.0-x86_64-dvd.iso", "sha256": hashlib.sha256(body).hexdigest(),
            "profile": ALMA10,
        }, content=body, headers={"Content-Type": "application/octet-stream"})
        assert response.status_code == 200
        catalog = response.json()
    else:
        body = iso_body()

        class DatastoreClient:
            def browse_iso_media(self, datastore, folder):
                return {"files": [{"name": "AlmaLinux.iso", "path": "isos/AlmaLinux.iso", "size_bytes": len(body)}]}

            def download_iso(self, datastore, path, destination, **kwargs):
                destination.write_bytes(body)

        catalog = signed_in.app.state.media.import_esxi(
            DatastoreClient(), "esxi.lab", "datastore1", "isos/AlmaLinux.iso", hashlib.sha256(body).hexdigest(), ALMA10,
        )
    selected = catalog["selected"]
    assert selected["profile"] == ALMA10
    assert selected["source"] == source
    assert signed_in.app.state.db.media_files()[0]["profile"] == ALMA10
    result = signed_in.app.state.media.select(selected["id"], selected["sha256"])
    assert result["selected"]["profile"] == ALMA10
    assert next(item for item in result["items"] if item["id"] == selected["id"])["profile"] == ALMA10


def test_rejected_alma_metadata_keeps_previous_selection_and_discards_upload(signed_in, config, alma_inspection):
    checksum = write_iso(config.ubuntu_iso, b"UBUNTU")
    before = choose(signed_in.app.state.media, config.ubuntu_iso, checksum, UBUNTU)
    body = iso_body(b"OTHER")
    response = signed_in.post("/api/settings/media/upload", params={
        "filename": "pretends-to-be-AlmaLinux.iso", "sha256": hashlib.sha256(body).hexdigest(), "profile": ALMA10,
    }, content=body, headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 400
    assert "Could not verify the AlmaLinux" in response.json()["detail"]
    assert signed_in.app.state.db.media_settings() == before
    assert signed_in.app.state.db.media_files() == []
    assert list((config.data_dir / "media").iterdir()) == []


def test_profile_inspection_never_bypasses_publisher_checksum(config, alma_inspection):
    write_iso(config.ubuntu_iso)
    manager = MediaManager(Database(config.data_dir, config.secret_key), config)
    item = manager.catalog()["items"][0]
    with pytest.raises(MediaError, match="SHA-256 does not match"):
        manager.select(item["id"], "0" * 64, ALMA10)
    assert alma_inspection == []


def test_iso_replacement_during_profile_validation_is_rejected(config, monkeypatch):
    checksum = write_iso(config.ubuntu_iso)
    manager = MediaManager(Database(config.data_dir, config.secret_key), config)
    item = manager.catalog()["items"][0]

    def change_source(path, **kwargs):
        path.write_bytes(iso_body(b"replacement") + b"changed size")
        return {"profile": ALMA10}

    monkeypatch.setattr(alma_guest, "validate_install_iso", change_source)
    with pytest.raises(MediaError, match="changed while its installer profile"):
        manager.select(item["id"], checksum, ALMA10)
    assert manager.db.media_settings() is None


@pytest.mark.parametrize("endpoint", ["select", "upload", "esxi"])
def test_api_rejects_unknown_installer_profiles(signed_in, endpoint):
    checksum = "0" * 64
    if endpoint == "select":
        response = signed_in.put("/api/settings/media", json={
            "media_id": "unknown", "sha256": checksum, "profile": "other-linux",
        })
    elif endpoint == "upload":
        response = signed_in.post("/api/settings/media/upload", params={
            "filename": "installer.iso", "sha256": checksum, "profile": "other-linux",
        }, content=iso_body())
    else:
        response = signed_in.post("/api/settings/media/esxi", json={
            "host": "esxi.lab", "datastore": "datastore1", "path": "isos/installer.iso", "sha256": checksum,
            "profile": "other-linux",
        })
    assert response.status_code == 422
    assert signed_in.app.state.db.media_settings() is None


def test_queued_alma_profile_and_media_do_not_follow_later_setup_changes(config, spec, monkeypatch, alma_inspection):
    import gdeploy.service as module

    checksum = write_iso(config.ubuntu_iso)
    second = config.ubuntu_iso.parent / "ubuntu-server.iso"
    other_checksum = write_iso(second, b"UBUNTU")
    db = Database(config.data_dir, config.secret_key)
    service = DeploymentService(db, config, InventoryClient)
    original = choose(service.media, config.ubuntu_iso, checksum, ALMA10)
    monkeypatch.setattr(module.shutil, "which", lambda command: "/usr/bin/xorriso")
    monkeypatch.setattr(module, "generate_ssh_key", lambda: ("private", "public"))
    spec["os_profile"] = ALMA10
    job = service.enqueue(spec, {"host": "esxi.lab", "username": "root", "password": "secret"})
    assert db.get(job["id"], private=True)["secrets"]["os_media"] == original
    choose(service.media, second, other_checksum, UBUNTU)
    observed = []

    def build(source, target, vm, *args, **kwargs):
        observed.append((source, copy.deepcopy(vm)))
        raise DeploymentError("Stop after immutable OS profile selection")

    monkeypatch.setattr(module, "build_seed_iso", build)
    db.claim()
    service.run(job["id"])
    assert len(observed) == 1
    assert observed[0][0] == config.ubuntu_iso
    assert observed[0][1]["os_profile"] == ALMA10
    assert db.get(job["id"], private=True)["secrets"]["os_media"]["profile"] == ALMA10
    assert service.media.selected()["profile"] == UBUNTU
    assert "Stop after immutable OS profile" in db.get(job["id"])["error"]


def test_missing_requested_profile_media_fails_before_queuing_or_generating_credentials(config, spec, monkeypatch, alma_inspection):
    import gdeploy.service as module

    alma_iso = config.ubuntu_iso.parent / "alma-dvd.iso"
    checksum = write_iso(alma_iso)
    db = Database(config.data_dir, config.secret_key)
    service = DeploymentService(db, config, InventoryClient)
    choose(service.media, alma_iso, checksum, ALMA10)
    monkeypatch.setattr(module.shutil, "which", lambda command: "/usr/bin/xorriso")
    monkeypatch.setattr(module, "generate_ssh_key", lambda: pytest.fail("Missing requested OS media must not generate credentials"))
    spec["os_profile"] = UBUNTU
    checks = service.preflight(spec, {"host": "esxi.lab", "username": "root", "password": "secret"})
    assert not checks["ok"]
    assert any(check["name"] == "OS ISO" and not check["ok"] for check in checks["checks"])
    with pytest.raises(DeploymentError, match="Select and verify an ISO for Ubuntu"):
        service.enqueue(spec, {"host": "esxi.lab", "username": "root", "password": "secret"})
    assert db.list() == []


def test_alma_dispatch_passes_credentials_and_authorized_keys_without_reinterpreting_profile(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(alma_guest, "build_install_iso", lambda *args, **kwargs: captured.append((args, kwargs)))
    spec = {"name": "alma-vm", "os_profile": ALMA10, "role": "os_only", "ip_mode": "dhcp"}
    def callback(message, level):
        pass
    guest.build_seed_iso(tmp_path / "source.iso", tmp_path / "output.iso", spec, "gdeploy", "password", "public",
                         authorized_ssh_keys=["additional"], log=callback)
    assert captured[0][0][2] is spec
    assert captured[0][0][3:] == ("gdeploy", "password", "public")
    assert captured[0][1] == {"authorized_ssh_keys": ["additional"], "log": callback}
    with pytest.raises(guest.GuestError, match="Unknown OS installer"):
        guest.build_seed_iso(tmp_path / "source.iso", tmp_path / "output.iso", {"os_profile": "unknown"},
                             "gdeploy", "password", "public")


@pytest.mark.parametrize("profile, guest_id", [(UBUNTU, "ubuntu64Guest"), (ALMA10, "rhel9_64Guest")])
def test_vmware_uses_esxi8_rhel_family_identifier_for_alma_with_efi_vmxnet3(profile, guest_id, monkeypatch):
    class NetworkStub:
        def InvokeAccessor(self, obj, info):
            assert info.name == "name"
            return "VM Network"

    client = ESXiClient("esxi.lab", "root", "secret")
    client._si = NS()
    client._content = NS(fileManager=MagicMock())
    client._host = NS(
        datastore=[NS(name="datastore1", summary=NS(accessible=True, maintenanceMode="normal"))],
        network=[vim.Network("network-1", NetworkStub())], parent=NS(resourcePool=NS()),
    )
    client._dc = NS(name="ha-datacenter", vmFolder=MagicMock())
    client._dc.vmFolder.CreateVM_Task.return_value = NS(info=NS(state="success", result=NS(_moId="created")))
    monkeypatch.setattr(client, "_objects", lambda kind: [])
    owner = "067a159a-4055-4bfe-b5ed-3b34644b6ebc"
    spec = {"name": "lab-os", "cpu": 2, "ram_gb": 4, "disk_gb": 40,
            "datastore": "datastore1", "network": "VM Network", "os_profile": profile}
    assert client.create_vm(spec, f"[datastore1] gdeploy/{owner}/os.iso", owner) == "created"
    result = client._dc.vmFolder.CreateVM_Task.call_args.kwargs["config"]
    assert result.guestId == guest_id
    assert result.firmware == "efi"
    assert any(isinstance(change.device, vim.vm.device.VirtualVmxnet3) for change in result.deviceChange)
    assert isinstance(result.bootOptions.bootOrder[0], vim.vm.BootOptions.BootableDiskDevice)
