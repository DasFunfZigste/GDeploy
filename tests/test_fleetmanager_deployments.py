"""Fleet configuration is private, frozen per job, and checked before VM changes."""

import copy
import hashlib
import json
from dataclasses import replace

import pytest

from gdeploy.db import Database
from gdeploy.fleetmanager import FleetManagerError
from gdeploy.guest import _autoinstall_data
from gdeploy.models import DeploymentSpec, VMSpec
from gdeploy.service import DeploymentError, DeploymentService, safe_error


@pytest.fixture
def fleet_job(config, spec, monkeypatch):
    import gdeploy.service as module

    source = bytearray(40 * 2048)
    source[16 * 2048:16 * 2048 + 7] = b"\x01CD001\x01"
    config.ubuntu_iso.write_bytes(source)
    config = replace(config, ubuntu_sha256=hashlib.sha256(source).hexdigest())
    spec = dict(spec, vms=[dict(spec["vms"][0], role="fleetmanager", name="lab-fleet", disk_gb=80)])
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
            return "vm-fleet"

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

        def install(self, role, secrets, *, fleetmanager, **kwargs):
            assert role == "fleetmanager"
            calls.append(("install", copy.deepcopy(fleetmanager)))
            return {"services": [{
                "name": "FleetManager", "url": "https://192.0.2.25", "username": "admin",
                "password": "fleet-generated-temporary-password", "password_change_required": True,
                "community_string": fleetmanager["community_string"],
            }]}

    def build(source, output, *args, **kwargs):
        calls.append(("iso-offline", kwargs.get("offline", False)))
        output.write_bytes(b"iso")

    monkeypatch.setattr(module, "GuestSession", Guest)
    monkeypatch.setattr(module, "generate_ssh_key", lambda: ("private", "public"))
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/xorriso")
    monkeypatch.setattr(module, "build_seed_iso", build)
    service = DeploymentService(db, config, ESXi)
    saved = {
        "mode": "online", "community_string": "fleet-shared-secret", "repository_token": "repo-token-secret",
        "license_name": "customer-fleet.pem", "license_pem": "private-license-body",
        "license_sha256": "a" * 64, "package": None, "dependencies": [],
    }
    monkeypatch.setattr(service.fleetmanager, "selected", lambda: copy.deepcopy(saved))

    def validate(snapshot):
        if not snapshot:
            raise FleetManagerError("Save FleetManager configuration first.")
        return snapshot

    monkeypatch.setattr(service.fleetmanager, "validate_snapshot", validate)

    def ready(esxi, vm, credential, identifier):
        calls.append("os-ready")
        vm["ip"] = "192.0.2.25"
        credential["host_key"] = "pinned-key"

    monkeypatch.setattr(service, "_wait_for_guest", ready)
    db.set_settings({"host": "esxi.lab", "username": "root", "password": "esxi-secret"})
    return service, db, spec, saved, calls


def test_missing_fleet_setup_links_to_actionable_setup_before_queue(fleet_job, monkeypatch):
    service, db, spec, _, calls = fleet_job
    monkeypatch.setattr(service.fleetmanager, "selected", lambda: None)
    result = service.preflight(spec)
    failed = next(check for check in result["checks"] if check["name"] == "FleetManager configuration")
    assert not failed["ok"]
    assert failed["action"] == {"label": "Configure FleetManager", "href": "#settings/packages/fleetmanager"}
    with pytest.raises(DeploymentError, match="FleetManager"):
        service.enqueue(spec)
    assert db.list() == [] and calls == []


@pytest.mark.parametrize("mode", ["online", "offline"])
def test_queued_fleet_secrets_and_mode_survive_setup_changes(fleet_job, mode):
    service, db, spec, saved, calls = fleet_job
    saved["mode"] = mode
    original = copy.deepcopy(saved)
    job = service.enqueue(spec)
    serialized = json.dumps(job)
    for secret in (saved["community_string"], saved["repository_token"], saved["license_pem"]):
        assert secret not in serialized
    saved.update(mode="online" if mode == "offline" else "offline", community_string="new-secret",
                 repository_token="new-token", license_pem="new-license")
    db.claim()
    service.run(job["id"])
    completed = db.get(job["id"])
    assert completed["status"] == "completed"
    assert ("iso-offline", mode == "offline") in calls
    assert ("install", original) in calls
    assert completed["vms"][0]["services"] == [{"name": "FleetManager", "url": "https://192.0.2.25"}]
    private = service.credentials(job["id"])["vms"][0]["services"][0]
    assert private["password"] == "fleet-generated-temporary-password"
    assert private["password_change_required"] is True
    assert private["community_string"] == original["community_string"]
    assert "fleet-generated-temporary-password" not in json.dumps(db.get(job["id"]))


@pytest.mark.parametrize("when", ["before_vm", "after_os"])
def test_stale_package_or_license_stops_safely(fleet_job, monkeypatch, when):
    service, db, spec, _, calls = fleet_job
    job = service.enqueue(spec)

    def reject(snapshot):
        raise FleetManagerError("FleetManager package changed or license expired.")

    if when == "before_vm":
        monkeypatch.setattr(service.fleetmanager, "validate_snapshot", reject)
    else:
        ready = service._wait_for_guest

        def after_ready(*args):
            ready(*args)
            monkeypatch.setattr(service.fleetmanager, "validate_snapshot", reject)

        monkeypatch.setattr(service, "_wait_for_guest", after_ready)
    db.claim()
    service.run(job["id"])
    result = db.get(job["id"])
    assert result["status"] == "failed"
    assert "FleetManager" in result["error"]
    assert not any(isinstance(call, tuple) and call[0] == "install" for call in calls)
    assert ("create-vm" in calls) == (when == "after_os")
    if when == "after_os":
        assert result["vms"][0]["vm_id"] == "vm-fleet"
        assert result["vms"][0]["status"] == "os_ready"


def test_other_roles_do_not_require_fleet_configuration(fleet_job, monkeypatch):
    service, _, spec, _, _ = fleet_job
    spec["vms"][0]["role"] = "ubuntu"
    monkeypatch.setattr(service.fleetmanager, "selected", lambda: pytest.fail("Unrelated role reads Fleet secrets"))
    assert service.preflight(spec)["ok"]
    assert service.enqueue(spec)["status"] == "queued"


def test_fleet_role_has_its_own_resources_and_supports_five_vms(spec):
    vms = [dict(spec["vms"][0], name="lab-" + role, role=role, disk_gb=80)
           for role in ("ubuntu", "splunk", "elasticsearch", "kibana", "fleetmanager")]
    result = DeploymentSpec(**dict(spec, vms=vms, splunk_license_accepted=True))
    assert len(result.vms) == 5
    for changes in ({"cpu": 1}, {"ram_gb": 4}, {"disk_gb": 50}):
        with pytest.raises(ValueError):
            VMSpec(**dict(vms[-1], **changes))


def test_fleet_secrets_are_redacted_from_errors():
    values = {"community_string": "community-private", "repository_token": "repository-private",
              "license_pem": "license-private"}
    error = safe_error("problem " + " ".join(values.values()), {"fleetmanager": values})
    assert "problem" in error and "[redacted]" in error
    assert all(value not in error for value in values.values())


def test_offline_iso_uses_no_repository_candidates_and_keeps_guest_access(spec):
    vm = dict(spec["vms"][0], role="fleetmanager")
    install = _autoinstall_data(vm, "gdeploy", "test-password", "ssh-rsa TEST", offline=True)["autoinstall"]
    assert install["apt"] == {"mirror-selection": {"primary": []}, "fallback": "offline-install", "geoip": False}
    assert install["source"] == {"id": "ubuntu-server", "search_drivers": False}
    assert install["network"]["ethernets"]["gdeploy"]["dhcp4"]
    assert {"openssh-server", "open-vm-tools", "python3", "ca-certificates"}.issubset(install["packages"])
    assert install["ssh"]["authorized-keys"] == ["ssh-rsa TEST"]
    assert install["storage"]["layout"]["name"] == "direct"
    online = _autoinstall_data(vm, "gdeploy", "test-password", "ssh-rsa TEST")["autoinstall"]
    assert "apt" not in online
