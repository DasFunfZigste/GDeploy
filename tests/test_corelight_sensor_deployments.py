import copy
import json
from dataclasses import replace

import pytest

from gdeploy.db import Database
from gdeploy.models import DeploymentSpec
from gdeploy.service import DeploymentError, DeploymentService, safe_error
from test_corelight_sensor import sensor_spec, settings
from test_media_deployments import iso_file


@pytest.fixture
def sensor_job(config, spec, monkeypatch):
    import gdeploy.service as module

    checksum = iso_file(config.ubuntu_iso, b"Ubuntu 24.04 minimal")
    config = replace(config, ubuntu_sha256=checksum)
    spec = DeploymentSpec(**sensor_spec(spec)).model_dump()
    db = Database(config.data_dir, config.secret_key)
    calls, hooks = [], {}
    inventory = {
        "host": {"cpu_threads": 32, "cpu_cores": 16, "cpu_mhz": 2500, "memory_gb": 128,
                 "free_cpu_reservation_mhz": 40000, "free_memory_reservation_gb": 128},
        "vms": [], "datastores": [{"name": "datastore1", "free_gb": 5000}],
        "networks": [{"name": "VM Network"}, {"name": "Capture"}],
    }

    def record(name, *args):
        calls.append((name, *copy.deepcopy(args)))
        if name in hooks:
            hooks[name](*args)

    class ESXi:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def inventory(self):
            record("inventory")
            return inventory

        def create_vm(self, vm, iso_path, identifier):
            record("create-vm", vm, iso_path)
            return "vm-sensor"

        def network_macs(self, vm_id, identifier):
            record("network-macs", vm_id)
            return {"management_mac": "00:50:56:aa:bb:01", "monitor_mac": "00:50:56:aa:bb:02"}

        def upload_iso(self, datastore, remote, local):
            record("upload-iso", datastore, remote)

        def attach_iso(self, vm_id, remote, identifier):
            record("attach-iso", vm_id, remote)

        def power_on(self, vm_id, identifier):
            record("power-on", vm_id)

        def detach_iso(self, vm_id, identifier):
            record("detach-iso", vm_id)

        def delete_iso(self, datastore, remote, identifier):
            record("delete-iso", datastore, remote)

        def find_owned_vms(self, identifier):
            record("find-owned", identifier)
            return []

        def destroy_vm(self, vm_id, identifier):
            record("destroy-vm", vm_id)

    class Guest:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def install(self, role, secrets, *, corelight_sensor, **kwargs):
            assert role == "corelight_sensor"
            record("install", corelight_sensor)
            return {"services": [{"name": "Corelight Software Sensor", "url": "https://192.0.2.25",
                                  "username": "admin", "password": corelight_sensor["community_string"]}]}

    def build(source, output, vm, *args, **kwargs):
        record("build-iso", vm)
        output.write_bytes(b"sensor ISO")

    def ready(esxi, vm, credential, identifier):
        record("os-ready")
        vm["ip"] = "192.0.2.25"
        credential["host_key"] = "pinned-key"

    monkeypatch.setattr(module, "GuestSession", Guest)
    monkeypatch.setattr(module, "generate_ssh_key", lambda: ("private", "public"))
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/xorriso")
    monkeypatch.setattr(module, "build_seed_iso", build)
    service = DeploymentService(db, config, ESXi)
    monkeypatch.setattr(service, "_wait_for_guest", ready)
    db.set_settings({"host": "esxi.lab", "username": "root", "password": "esxi-secret"})
    service.corelight_sensor.save(settings())
    return service, db, spec, calls, hooks, inventory


def test_sensor_uses_generated_macs_before_iso_and_snapshotted_private_settings(sensor_job):
    service, db, spec, calls, _, _ = sensor_job
    job = service.enqueue(spec)
    original = db.get(job["id"], private=True)["secrets"]["corelight_sensor"]
    service.corelight_sensor.save(settings(repository_token="changed-token", license_key="changed-license", fleet_url="https://changed.test"))
    service.corelight_sensor.clear()
    db.claim()
    service.run(job["id"])
    result = db.get(job["id"])
    assert result["status"] == "completed", result["error"]
    order = [call[0] for call in calls if call[0] != "inventory"]
    assert order == ["create-vm", "network-macs", "build-iso", "upload-iso", "attach-iso", "power-on", "os-ready", "detach-iso", "delete-iso", "install"]
    created = next(call for call in calls if call[0] == "create-vm")
    assert created[2] is None
    built = next(call[1] for call in calls if call[0] == "build-iso")
    assert built["management_mac"] == "00:50:56:aa:bb:01" and built["monitor_mac"] == "00:50:56:aa:bb:02"
    installed = next(call[1] for call in calls if call[0] == "install")
    assert installed == {**original, "management_mac": built["management_mac"], "monitor_mac": built["monitor_mac"]}
    assert result["vms"][0]["management_mac"] == built["management_mac"]
    assert "sensor_pairing_token" not in result["spec"]
    public = json.dumps(result) + json.dumps(db.events(job["id"]))
    for key in ("repository_token", "community_string", "license_key", "pairing_token"):
        assert original[key] not in public
    assert service.credentials(job["id"])["vms"][0]["services"][0]["password"] == original["community_string"]


@pytest.mark.parametrize("failure", ["network-macs", "build-iso", "upload-iso", "attach-iso", "install"])
def test_sensor_failures_keep_created_vm_and_redact_configuration(sensor_job, failure):
    service, db, spec, calls, hooks, _ = sensor_job
    job = service.enqueue(spec)
    snapshot = db.get(job["id"], private=True)["secrets"]["corelight_sensor"]

    def fail(*args):
        raise RuntimeError("Injected failure: " + json.dumps(snapshot))

    hooks[failure] = fail
    db.claim()
    service.run(job["id"])
    result = db.get(job["id"], private=True)
    assert result["status"] == "failed" and result["vms"][0]["vm_id"] == "vm-sensor"
    assert result["secrets"]["corelight_sensor"] == snapshot
    assert not any(call[0] == "destroy-vm" for call in calls)
    assert bool(result["resources"]) == (failure in {"upload-iso", "attach-iso"})
    for key in ("repository_token", "community_string", "license_key", "pairing_token"):
        assert snapshot[key] not in result["error"]
        assert snapshot[key] not in json.dumps(db.events(job["id"]))


def test_sensor_stop_after_vm_creation_keeps_mac_mapping_without_provisioning_media(sensor_job):
    service, db, spec, calls, hooks, _ = sensor_job
    job = service.enqueue(spec)
    hooks["network-macs"] = lambda *args: service.request_stop(job["id"])
    db.claim()
    service.run(job["id"])
    result = db.get(job["id"], private=True)
    assert result["status"] == "stopped" and result["vms"][0]["vm_id"] == "vm-sensor"
    assert result["vms"][0]["monitor_mac"] == "00:50:56:aa:bb:02"
    assert not any(call[0] in {"build-iso", "upload-iso", "power-on", "destroy-vm"} for call in calls)


@pytest.mark.parametrize("missing", ["settings", "pairing", "license"])
def test_missing_sensor_inputs_fail_before_queue_without_secret_echo(sensor_job, missing):
    service, db, spec, calls, _, _ = sensor_job
    if missing == "settings":
        service.corelight_sensor.clear()
    elif missing == "pairing":
        spec.pop("sensor_pairing_token")
    else:
        saved = service.corelight_sensor.selected()
        saved.pop("license_key")
        db.set_corelight_sensor_settings(saved)
    result = service.preflight(spec)
    check = next(item for item in result["checks"] if item["name"] == "Corelight Software Sensor configuration")
    assert not check["ok"] and check["action"]["href"] == "#settings/packages/corelight-sensor"
    with pytest.raises(DeploymentError):
        service.enqueue(spec)
    assert not db.list() and all(call[0] == "inventory" for call in calls)


@pytest.mark.parametrize("field,value", [("cpu_cores", 2), ("free_cpu_reservation_mhz", 9999), ("free_memory_reservation_gb", 15), ("cpu_mhz", None)])
def test_sensor_preflight_requires_dedicated_host_reservation_capacity(sensor_job, field, value):
    service, db, spec, calls, _, inventory = sensor_job
    inventory["host"][field] = value
    assert not service.preflight(spec)["ok"]
    with pytest.raises(DeploymentError, match="[Cc]apacity"):
        service.enqueue(spec)
    assert db.list() == [] and all(call[0] == "inventory" for call in calls)


def test_sensor_preflight_rejects_missing_capture_portgroup_and_dhcp_reservation(sensor_job):
    service, _, spec, _, _, _ = sensor_job
    spec["vms"][0].update(monitor_network="Missing", dhcp_reserved=False)
    failed = [item["name"] for item in service.preflight(spec)["checks"] if not item["ok"]]
    assert "lab-sensor monitoring network" in failed and "lab-sensor management address" in failed


def test_sensor_preflight_accounts_for_queued_reservations(sensor_job):
    service, _, spec, _, _, inventory = sensor_job
    service.enqueue(spec)
    next_spec = copy.deepcopy(spec)
    next_spec["sensor_pairing_token"] = "new-pairing-token"
    next_spec["vms"][0]["name"] = "second-sensor"
    inventory["host"]["free_cpu_reservation_mhz"] = 15000
    failed = [item["name"] for item in service.preflight(next_spec)["checks"] if not item["ok"]]
    assert "Sensor CPU reservation capacity" in failed


def test_queued_sensor_requires_its_snapshot_before_contacting_esxi(sensor_job, monkeypatch):
    service, db, spec, calls, _, _ = sensor_job
    job = service.enqueue(spec)
    value = db.get(job["id"], private=True)
    value["secrets"].pop("corelight_sensor")
    db.update(job["id"], secrets=value["secrets"])
    calls.clear()
    monkeypatch.setattr(service, "client", lambda *args: pytest.fail("Invalid sensor job contacted ESXi"))
    db.claim()
    service.run(job["id"])
    assert db.get(job["id"])["status"] == "failed" and calls == []


@pytest.mark.parametrize("token", ["", "unique-pairing-secret", "bad token"])
def test_sensor_redeploy_requires_fresh_token_before_any_cleanup(sensor_job, token):
    service, db, spec, calls, _, _ = sensor_job
    job = service.enqueue(spec)
    db.update(job["id"], status="failed")
    before = db.get(job["id"], private=True)
    calls.clear()
    with pytest.raises(DeploymentError):
        service.redeploy(job["id"], job["name"], sensor_pairing_token=token)
    assert calls == [] and db.get(job["id"], private=True) == before


def test_sensor_redeploy_uses_new_token_and_validates_required_license_before_cleanup(sensor_job):
    service, db, spec, calls, _, _ = sensor_job
    job = service.enqueue(spec)
    original = db.get(job["id"], private=True)
    db.update(job["id"], status="failed", vms=[{**original["vms"][0], "vm_id": "existing-vm"}])
    saved = service.corelight_sensor.selected()
    service.corelight_sensor.clear()
    calls.clear()
    with pytest.raises(DeploymentError):
        service.redeploy(job["id"], job["name"], sensor_pairing_token="new-pairing-token")
    assert calls == []
    service.corelight_sensor.save(saved)
    replacement = service.redeploy(job["id"], job["name"], sensor_pairing_token="new-pairing-token")
    assert ("destroy-vm", "existing-vm") in calls
    assert db.get(job["id"])["status"] == "reverted"
    assert db.get(job["id"], private=True)["secrets"] == original["secrets"]
    new = db.get(replacement["id"], private=True)
    assert new["secrets"]["corelight_sensor"]["pairing_token"] == "new-pairing-token"
    assert "sensor_pairing_token" not in replacement["spec"]


def test_sensor_redeploy_keeps_prevalidated_settings_when_setup_changes_during_cleanup(sensor_job):
    service, db, spec, _, hooks, _ = sensor_job
    job = service.enqueue(spec)
    original = service.corelight_sensor.selected()
    db.update(job["id"], status="failed")
    hooks["find-owned"] = lambda *args: service.corelight_sensor.clear()
    replacement = service.redeploy(job["id"], job["name"], sensor_pairing_token="new-pairing-token")
    assert db.get(replacement["id"], private=True)["secrets"]["corelight_sensor"] == {**original, "pairing_token": "new-pairing-token"}


def test_sensor_redeploy_error_redacts_new_inputs_as_well_as_old_snapshot(sensor_job, monkeypatch):
    service, db, spec, _, _, _ = sensor_job
    job = service.enqueue(spec)
    db.update(job["id"], status="failed")
    service.corelight_sensor.save(settings(license_key="replacement-license", repository_token="replacement-repository"))

    def fail(*args, **kwargs):
        raise RuntimeError("new-pairing-token replacement-license replacement-repository")

    monkeypatch.setattr(service, "enqueue", fail)
    with pytest.raises(DeploymentError, match=r"\[redacted\]"):
        service.redeploy(job["id"], job["name"], sensor_pairing_token="new-pairing-token")
    public = json.dumps(db.get(job["id"])) + json.dumps(db.events(job["id"]))
    assert all(secret not in public for secret in ("new-pairing-token", "replacement-license", "replacement-repository"))


def test_non_sensor_deployments_do_not_read_sensor_settings(sensor_job, monkeypatch):
    service, _, spec, _, _, _ = sensor_job
    spec.pop("sensor_pairing_token")
    spec["vms"][0]["role"] = "ubuntu"
    spec["vms"][0].pop("monitor_network")
    monkeypatch.setattr(service.corelight_sensor, "selected", lambda: pytest.fail("Unrelated role read sensor settings"))
    assert service.preflight(spec)["ok"] and service.enqueue(spec)["status"] == "queued"


def test_sensor_license_and_token_errors_are_redacted():
    snapshot = {"license_key": "private-license", "pairing_token": "private-pairing", "repository_token": "private-repository", "community_string": "private-community"}
    result = safe_error(" ".join(snapshot.values()), {"corelight_sensor": snapshot})
    assert result == "[redacted] [redacted] [redacted] [redacted]"
