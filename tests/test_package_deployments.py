"""Splunk package choices must reach the worker without changing queued jobs."""

import hashlib
import io
import os
import tarfile
from dataclasses import replace

import pytest

from gdeploy.db import Database
from gdeploy.service import DeploymentError, DeploymentService


def package_file(path, marker=b"first"):
    with tarfile.open(path, "w:gz") as archive:
        elf = b"\x7fELF\x02\x01" + b"\x00" * 12 + (62).to_bytes(2, "little")
        for name, body in (("splunk/bin/splunk", marker), ("splunk/bin/splunkd", elf)):
            item = tarfile.TarInfo(name)
            item.size = len(body)
            archive.addfile(item, io.BytesIO(body))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def package_job(config, spec, monkeypatch):
    import gdeploy.service as module

    source = bytearray(40 * 2048)
    source[16 * 2048:16 * 2048 + 7] = b"\x01CD001\x01"
    config.ubuntu_iso.write_bytes(source)
    checksum = package_file(config.splunk_package)
    config = replace(config, ubuntu_sha256=hashlib.sha256(source).hexdigest(), splunk_sha256=checksum)
    spec = dict(spec, splunk_license_accepted=True, vms=[dict(spec["vms"][0], role="splunk", name="lab-splunk")])
    db = Database(config.data_dir, config.secret_key)
    calls = []

    class ESXi:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def inventory(self):
            return {
                "host": {"cpu_threads": 32, "memory_gb": 128}, "vms": [],
                "datastores": [{"name": "datastore1", "free_gb": 1000}],
                "networks": [{"name": "VM Network"}],
            }

        def upload_iso(self, *args):
            calls.append("upload-iso")

        def create_vm(self, *args):
            calls.append("create-vm")
            return "vm-splunk"

        def power_on(self, *args):
            calls.append("power-on")

        def detach_iso(self, *args):
            calls.append("detach-iso")

        def delete_iso(self, *args):
            calls.append("delete-iso")

    class Guest:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def install(self, role, secrets, *, splunk_package, splunk_sha256, **kwargs):
            assert role == "splunk"
            observed = hashlib.sha256(splunk_package.read_bytes()).hexdigest()
            assert splunk_sha256 == observed
            calls.append(("install", splunk_package, observed))
            return {"services": []}

    monkeypatch.setattr(module, "GuestSession", Guest)
    monkeypatch.setattr(module, "generate_ssh_key", lambda: ("private", "public"))
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/xorriso")
    monkeypatch.setattr(module, "build_seed_iso", lambda source, target, *a, **kw: target.write_bytes(b"iso"))
    service = DeploymentService(db, config, ESXi)

    def wait_ready(esxi, vm, credential, identifier):
        calls.append("os-ready")
        vm["ip"] = "192.0.2.25"
        credential["host_key"] = "pinned-key"

    monkeypatch.setattr(service, "_wait_for_guest", wait_ready)
    db.set_settings({"host": "esxi.lab", "username": "root", "password": "test-secret"})
    return service, db, spec, config, calls


def choose(service, path, checksum):
    candidate = next(item for item in service.packages.catalog()["items"] if item["name"] == path.name)
    return service.packages.select(candidate["id"], checksum)["selected"]


def test_missing_package_has_a_setup_link_and_cannot_queue(package_job):
    service, db, spec, config, calls = package_job
    config.splunk_package.unlink()
    result = service.preflight(spec)
    failed = next(check for check in result["checks"] if check["name"] == "Splunk Linux x86_64 package")
    assert not failed["ok"]
    assert failed["action"] == {"label": "Configure Splunk package", "href": "#settings/packages"}
    assert "Software packages" in failed["message"]
    with pytest.raises(DeploymentError, match="Software packages"):
        service.enqueue(spec)
    assert db.list() == [] and calls == []


def test_other_roles_do_not_require_a_splunk_package(package_job):
    service, _, spec, config, _ = package_job
    config.splunk_package.unlink()
    spec["vms"][0]["role"] = "ubuntu"
    result = service.preflight(spec)
    assert result["ok"]
    assert all("Splunk" not in check["name"] for check in result["checks"])


@pytest.mark.parametrize("mode", ["sha256", "sha512", "legacy"])
def test_queued_package_reaches_install_even_after_setup_changes(package_job, mode):
    service, db, spec, config, calls = package_job
    legacy = mode == "legacy"
    first = config.splunk_package if legacy else config.splunk_package.with_name("splunk-10.1-linux-amd64.tgz")
    first_checksum = config.splunk_sha256 if legacy else package_file(first)
    if mode == "sha512":
        publisher_checksum = hashlib.sha512(first.read_bytes()).hexdigest()
        service.packages.select(service.packages._server_id(first), sha512=publisher_checksum)
    elif not legacy:
        choose(service, first, first_checksum)
    item = service.enqueue(spec)
    secret_data = db.get(item["id"], private=True)["secrets"]
    assert secret_data["splunk_package"]["path"] == str(first)
    assert secret_data["splunk_package"]["sha256"] == first_checksum
    if mode == "sha512":
        assert secret_data["splunk_package"]["sha512"] == publisher_checksum
    if legacy:
        secret_data.pop("splunk_package")
        db.update(item["id"], secrets=secret_data)
    second = config.splunk_package.with_name("splunk-future-linux-amd64.tgz")
    second_checksum = package_file(second, b"second")
    choose(service, second, second_checksum)
    db.claim()
    service.run(item["id"])
    assert db.get(item["id"])["status"] == "completed"
    assert calls == ["upload-iso", "create-vm", "power-on", "os-ready", "detach-iso", "delete-iso", ("install", first, first_checksum)]
    assert service.packages.selected()["path"] == str(second)
    assert "splunk_package" not in db.get(item["id"])


@pytest.mark.parametrize("when", ["before_creation", "after_os_ready"])
def test_changed_package_stops_before_install_and_preserves_created_vm(package_job, monkeypatch, when):
    service, db, spec, config, calls = package_job
    item = service.enqueue(spec)

    def change_package():
        # Even metadata-preserving replacement must be detected by hashing.
        old = config.splunk_package.stat()
        body = bytearray(config.splunk_package.read_bytes())
        body[-8] ^= 1
        config.splunk_package.write_bytes(body)
        os.utime(config.splunk_package, ns=(old.st_atime_ns, old.st_mtime_ns))

    if when == "before_creation":
        change_package()
    else:
        original = service._wait_for_guest

        def change_after_boot(*args):
            original(*args)
            change_package()

        monkeypatch.setattr(service, "_wait_for_guest", change_after_boot)
    db.claim()
    service.run(item["id"])
    result = db.get(item["id"])
    assert result["status"] == "failed"
    assert "SHA-256" in result["error"]
    assert not any(isinstance(call, tuple) and call[0] == "install" for call in calls)
    assert ("create-vm" in calls) == (when == "after_os_ready")
    if when == "after_os_ready":
        assert result["vms"][0]["vm_id"] == "vm-splunk"
        assert result["vms"][0]["status"] == "os_ready"


def test_invalid_tar_is_rejected_during_preflight(package_job):
    service, _, spec, config, calls = package_job
    config.splunk_package.write_bytes(b"not a tgz")
    service.packages.config = replace(config, splunk_sha256=hashlib.sha256(b"not a tgz").hexdigest())
    result = service.preflight(spec)
    assert not result["ok"]
    assert calls == []
    assert next(check for check in result["checks"] if "Splunk" in check["name"])["action"]["href"] == "#settings/packages"
