import hashlib
from dataclasses import replace

import pytest

from gdeploy.db import Database
from gdeploy.service import DeploymentError, DeploymentService


def iso_file(path, marker):
    content = bytearray(40 * 2048)
    content[:len(marker)] = marker
    content[16 * 2048:16 * 2048 + 7] = b"\x01CD001\x01"
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def choose(service, path, checksum):
    item = next(item for item in service.media.catalog()["items"] if item["name"] == path.name)
    return service.media.select(item["id"], checksum)["selected"]


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
            "vms": [],
            "datastores": [{"name": "datastore1", "free_gb": 1000}],
            "networks": [{"name": "VM Network"}],
        }


@pytest.mark.parametrize("legacy_record", [False, True])
def test_queued_job_keeps_media_after_setup_changes(config, spec, monkeypatch, legacy_record):
    import gdeploy.service as module

    first = config.ubuntu_iso
    first_checksum = iso_file(first, b"first")
    second = first.parent / "second.iso"
    second_checksum = iso_file(second, b"second")
    config = replace(config, ubuntu_sha256=first_checksum)
    db = Database(config.data_dir, config.secret_key)
    service = DeploymentService(db, config, InventoryClient)
    monkeypatch.setattr(module, "generate_ssh_key", lambda: ("private", "public"))
    monkeypatch.setattr(module.shutil, "which", lambda command: "/usr/bin/xorriso")
    item = service.enqueue(spec, {"host": "esxi.lab", "username": "root", "password": "secret"})
    captured = db.get(item["id"], private=True)["secrets"]
    assert captured["os_media"]["sha256"] == first_checksum
    if legacy_record:
        captured.pop("os_media")
        db.update(item["id"], secrets=captured)
    choose(service, second, second_checksum)
    sources = []

    def capture_source(source, *args, **kwargs):
        sources.append(source)
        raise DeploymentError("Stop after source selection, before any VM creation")

    monkeypatch.setattr(module, "build_seed_iso", capture_source)
    db.claim()
    service.run(item["id"])
    assert sources == [first]
    assert "Stop after source selection" in db.get(item["id"])["error"]
    assert service.media.selected()["sha256"] == second_checksum


def test_missing_media_cannot_queue_a_deployment(config, spec, monkeypatch):
    db = Database(config.data_dir, config.secret_key)
    service = DeploymentService(db, config, InventoryClient)
    with pytest.raises(DeploymentError, match="Select and verify an OS ISO in Setup"):
        service.enqueue(spec, {"host": "esxi.lab"})
    assert db.list() == []


def test_changed_media_stops_queued_job_before_vm_creation(config, spec, monkeypatch):
    import gdeploy.service as module

    checksum = iso_file(config.ubuntu_iso, b"verified")
    db = Database(config.data_dir, config.secret_key)
    service = DeploymentService(db, config, InventoryClient)
    choose(service, config.ubuntu_iso, checksum)
    monkeypatch.setattr(module, "generate_ssh_key", lambda: ("private", "public"))
    monkeypatch.setattr(module.shutil, "which", lambda command: "/usr/bin/xorriso")
    item = service.enqueue(spec, {"host": "esxi.lab"})
    iso_file(config.ubuntu_iso, b"replaced")
    monkeypatch.setattr(module, "build_seed_iso", lambda *args: pytest.fail("Changed media must fail preflight"))
    db.claim()
    service.run(item["id"])
    result = db.get(item["id"])
    assert result["status"] == "failed"
    assert "SHA-256" in result["error"]
    assert all(not vm.get("vm_id") for vm in result["vms"])
