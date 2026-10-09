import copy
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from gdeploy.db import Database, SensorPairingError
from gdeploy.models import DeploymentSpec
from gdeploy.service import DeploymentError, DeploymentService, safe_error
from gdeploy.vmware import VMwareError
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
            return {"management_mac": "00:50:56:20:bb:01", "monitor_mac": "00:50:56:20:bb:02"}

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


def test_sensor_verifies_configured_macs_before_iso_and_uses_snapshotted_private_settings(sensor_job):
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
    assert built["management_mac"] == "00:50:56:20:bb:01" and built["monitor_mac"] == "00:50:56:20:bb:02"
    installed = next(call[1] for call in calls if call[0] == "install")
    assert installed == {**original, "management_mac": built["management_mac"], "monitor_mac": built["monitor_mac"]}
    assert result["vms"][0]["management_mac"] == built["management_mac"]
    assert "sensor_pairing_token" not in result["spec"]
    public = json.dumps(result) + json.dumps(db.events(job["id"]))
    assert f"Verified sensor MAC addresses for {built['name']}: management {built['management_mac']}; monitoring {built['monitor_mac']}" in public
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
    assert result["vms"][0]["monitor_mac"] == "00:50:56:20:bb:02"
    assert not any(call[0] in {"build-iso", "upload-iso", "power-on", "destroy-vm"} for call in calls)


def test_sensor_mac_failure_before_iso_keeps_unused_pairing_token_for_successful_redeployment(sensor_job):
    service, db, spec, calls, hooks, _ = sensor_job
    job = service.enqueue(spec)
    original = db.get(job["id"], private=True)["secrets"]["corelight_sensor"]

    def fail(*args):
        raise VMwareError("ESXi did not return valid generated sensor network addresses.")

    hooks["network-macs"] = fail
    db.claim()
    service.run(job["id"])
    failed = db.get(job["id"], private=True)
    assert failed["status"] == "failed"
    assert failed["vms"][0]["vm_id"] == "vm-sensor"
    assert failed["vms"][0]["status"] == "preparing"
    assert failed["resources"] == []
    assert not any(call[0] in {"build-iso", "upload-iso", "attach-iso", "power-on", "install", "destroy-vm"} for call in calls)
    assert service.sensor_pairing_policy(job["id"])["can_reuse"]
    with db.connect() as connection:
        assert connection.execute("SELECT started FROM sensor_installation_state WHERE deployment_id=?", (job["id"],)).fetchone()[0] == 0

    del hooks["network-macs"]
    # Recovery uses the encrypted original even if Setup is no longer populated.
    service.corelight_sensor.clear()
    calls.clear()
    replacement = service.redeploy(job["id"], job["name"], sensor_pairing_token="")
    assert ("destroy-vm", "vm-sensor") in calls
    assert db.get(job["id"])["status"] == "reverted"
    assert db.get(replacement["id"], private=True)["secrets"]["corelight_sensor"] == original
    assert not db.sensor_pairing_token_used(original["pairing_token"], deployment_id=replacement["id"])

    calls.clear()
    db.claim()
    service.run(replacement["id"])
    completed = db.get(replacement["id"])
    assert completed["status"] == "completed", completed["error"]
    order = [call[0] for call in calls if call[0] != "inventory"]
    assert order == ["create-vm", "network-macs", "build-iso", "upload-iso", "attach-iso", "power-on", "os-ready", "detach-iso", "delete-iso", "install"]
    built = next(call[1] for call in calls if call[0] == "build-iso")
    installed = next(call[1] for call in calls if call[0] == "install")
    assert installed == {**original, "management_mac": built["management_mac"], "monitor_mac": built["monitor_mac"]}
    assert completed["vms"][0]["management_mac"] == built["management_mac"]
    assert completed["vms"][0]["monitor_mac"] == built["monitor_mac"]


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


@pytest.mark.parametrize("token", ["bad token", "bad\ntoken"])
def test_sensor_redeploy_rejects_invalid_token_before_any_cleanup(sensor_job, token):
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


@pytest.mark.parametrize("provided", ["", "unique-pairing-secret"])
def test_early_sensor_replacement_keeps_original_context_and_transfers_only_its_claim(sensor_job, provided):
    service, db, spec, calls, _, _ = sensor_job
    job = service.enqueue(spec)
    original = db.get(job["id"], private=True)["secrets"]["corelight_sensor"]
    db.update(job["id"], status="failed")
    service.corelight_sensor.save(settings(fleet_url="https://another-fleet.test", repository_token="another-repository"))
    service.corelight_sensor.clear()
    assert service.sensor_pairing_policy(job["id"])["can_reuse"]
    replacement = service.redeploy(job["id"], job["name"], sensor_pairing_token=provided)
    assert replacement["parent_id"] == job["id"]
    assert db.get(replacement["id"], private=True)["secrets"]["corelight_sensor"] == original
    assert db.sensor_pairing_token_used(original["pairing_token"], deployment_id=job["id"])
    assert not db.sensor_pairing_token_used(original["pairing_token"], deployment_id=replacement["id"])
    assert not service.sensor_pairing_policy(job["id"])["can_reuse"]
    before = len(calls)
    assert service.redeploy(job["id"], job["name"])["id"] == replacement["id"]
    assert len(calls) == before
    db.update(replacement["id"], status="failed")
    grandchild = service.redeploy(replacement["id"], replacement["name"])
    assert grandchild["parent_id"] == replacement["id"]
    assert not db.sensor_pairing_token_used(original["pairing_token"], deployment_id=grandchild["id"])
    with pytest.raises(SensorPairingError, match="already assigned"):
        db.create("unrelated-job", spec, {"corelight_sensor": original})
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM sensor_pairing_tokens").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM sensor_redeployments").fetchone()[0] == 0
    public = json.dumps(db.list()) + json.dumps(db.events(job["id"])) + json.dumps(service.sensor_pairing_policy(job["id"]))
    for key in ("repository_token", "community_string", "license_key", "pairing_token"):
        assert original[key] not in public


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("vm_status,allowed", [
    ("pending", True), ("preparing", True), ("installing_os", True), ("os_ready", True),
    ("installing_software", False), ("completed", False), ("", False), ("unknown", False),
])
def test_sensor_reuse_requires_reliable_per_sensor_progress(sensor_job, legacy, vm_status, allowed):
    service, db, spec, calls, _, _ = sensor_job
    job = service.enqueue(spec)
    vms = [{**job["vms"][0], "status": vm_status}]
    db.update(job["id"], status="interrupted", stage="installing_software", vms=vms)
    if legacy:
        with db.connect() as connection:
            connection.execute("DELETE FROM sensor_installation_state WHERE deployment_id=?", (job["id"],))
    assert service.sensor_pairing_policy(job["id"])["can_reuse"] is allowed
    if not allowed:
        calls.clear()
        for token in ("", spec["sensor_pairing_token"]):
            with pytest.raises(DeploymentError, match="fresh"):
                service.redeploy(job["id"], job["name"], sensor_pairing_token=token)
        assert calls == []


def test_legacy_boot_order_failure_reuses_token_after_restart(sensor_job):
    service, db, spec, _, hooks, _ = sensor_job
    job = service.enqueue(spec)

    def fail(*args):
        raise RuntimeError("A specified parameter was not correct: configSpec.bootOptions.bootOrder")

    hooks["create-vm"] = fail
    db.claim()
    service.run(job["id"])
    with db.connect() as connection:
        connection.execute("DELETE FROM sensor_installation_state WHERE deployment_id=?", (job["id"],))
    reopened = Database(db.path.parent, service.config.secret_key)
    reopened.recover()
    assert reopened.sensor_pairing_policy(job["id"])["can_reuse"]
    assert service.redeploy(job["id"], job["name"])["status"] == "queued"


def test_sensor_install_attempt_is_durable_before_transfer_and_cannot_be_erased_by_finish(sensor_job):
    service, db, spec, calls, hooks, _ = sensor_job
    job = service.enqueue(spec)
    original = db.get(job["id"], private=True)

    def fail(*args):
        with db.connect() as connection:
            assert connection.execute("SELECT started FROM sensor_installation_state WHERE deployment_id=?", (job["id"],)).fetchone()[0] == 1
        raise RuntimeError("Injected guest upload failure")

    hooks["install"] = fail
    db.claim()
    service.run(job["id"])
    assert not service.sensor_pairing_policy(job["id"])["can_reuse"]
    db.update(job["id"], vms=original["vms"], secrets=original["secrets"])
    reopened = Database(db.path.parent, service.config.secret_key)
    assert not reopened.sensor_pairing_policy(job["id"])["can_reuse"]
    calls.clear()
    with pytest.raises(DeploymentError, match="already started"):
        service.redeploy(job["id"], job["name"])
    assert calls == []


def test_sensor_reuse_is_not_affected_by_another_roles_installing_software_stage(sensor_job):
    service, db, spec, _, _, _ = sensor_job
    spec["vms"].append({**spec["vms"][0], "name": "second-ubuntu", "role": "ubuntu"})
    job = service.enqueue(spec)
    db.update(job["id"], status="failed", stage="installing_software", vms=[
        {**job["vms"][0], "status": "os_ready"}, {**job["vms"][1], "status": "installing_software"},
    ])
    assert service.sensor_pairing_policy(job["id"])["can_reuse"]


@pytest.mark.parametrize("damage", ["snapshot", "license", "claim", "runtime-vm", "services"])
def test_unverifiable_original_sensor_state_fails_before_cleanup(sensor_job, damage):
    service, db, spec, calls, _, _ = sensor_job
    job = service.enqueue(spec)
    value = db.get(job["id"], private=True)
    if damage == "snapshot":
        value["secrets"].pop("corelight_sensor")
    elif damage == "license":
        value["secrets"]["corelight_sensor"].pop("license_key")
    elif damage == "claim":
        with db.connect() as connection:
            connection.execute("DELETE FROM sensor_pairing_tokens WHERE deployment_id=?", (job["id"],))
    elif damage == "runtime-vm":
        value["vms"] = []
    else:
        value["vms"][0]["services"] = [{"name": "Sensor"}]
    db.update(job["id"], status="failed", secrets=value["secrets"], vms=value["vms"])
    calls.clear()
    assert not service.sensor_pairing_policy(job["id"])["can_reuse"]
    with pytest.raises(DeploymentError):
        service.redeploy(job["id"], job["name"])
    assert calls == []


def test_sensor_cleanup_failure_preserves_original_claim_for_retry_and_fresh_override(sensor_job):
    service, db, spec, calls, hooks, _ = sensor_job
    job = service.enqueue(spec)
    db.update(job["id"], status="failed")

    def fail(*args):
        raise RuntimeError("ESXi unavailable")

    hooks["find-owned"] = fail
    with pytest.raises(DeploymentError, match="ESXi unavailable"):
        service.redeploy(job["id"], job["name"])
    pending = db.sensor_redeployment(job["id"])
    assert pending["reuse"] and db.get(job["id"])["status"] == "cleanup_failed"
    assert service.sensor_pairing_policy(job["id"])["can_reuse"]
    assert len(db.list()) == 1
    assert not db.sensor_pairing_token_used(spec["sensor_pairing_token"], deployment_id=job["id"], replacement_parent_id=job["id"])
    service.corelight_sensor.clear()
    calls.clear()
    with pytest.raises(DeploymentError):
        service.redeploy(job["id"], job["name"], sensor_pairing_token="replacement-fresh")
    assert calls == [] and db.sensor_redeployment(job["id"]) == pending
    service.corelight_sensor.save(settings(fleet_url="https://replacement.test"))
    hooks.clear()
    replacement = service.redeploy(job["id"], job["name"], sensor_pairing_token="replacement-fresh")
    snapshot = db.get(replacement["id"], private=True)["secrets"]["corelight_sensor"]
    assert snapshot["fleet_url"] == "https://replacement.test:1443" and snapshot["pairing_token"] == "replacement-fresh"
    assert db.sensor_pairing_token_used(spec["sensor_pairing_token"])
    assert not db.sensor_pairing_token_used(spec["sensor_pairing_token"], deployment_id=job["id"])


def test_sensor_fresh_pending_token_is_reserved_and_settings_survive_restart_and_retry(sensor_job):
    service, db, spec, _, hooks, _ = sensor_job
    job = service.enqueue(spec)
    db.update(job["id"], status="failed")

    def fail(*args):
        raise RuntimeError("ESXi unavailable")

    hooks["find-owned"] = fail
    with pytest.raises(DeploymentError):
        service.redeploy(job["id"], job["name"], sensor_pairing_token="pending-fresh")
    pending = db.sensor_redeployment(job["id"])
    with pytest.raises(SensorPairingError, match="reserved"):
        db.create("unrelated-new-job", spec, {"corelight_sensor": pending["snapshot"]})
    service.corelight_sensor.clear()
    reopened = Database(db.path.parent, service.config.secret_key)
    reopened.recover()
    assert reopened.sensor_pairing_token_used("pending-fresh")
    hooks.clear()
    replacement = service.redeploy(job["id"], job["name"], sensor_pairing_token="pending-fresh")
    assert db.get(replacement["id"], private=True)["secrets"]["corelight_sensor"] == pending["snapshot"]
    assert db.sensor_redeployment(job["id"]) is None


def test_sensor_cleanup_reservation_serializes_duplicate_calls_across_service_instances(sensor_job):
    service, db, spec, calls, hooks, _ = sensor_job
    job = service.enqueue(spec)
    db.update(job["id"], status="failed")
    entered, release = threading.Event(), threading.Event()

    def hold(*args):
        entered.set()
        assert release.wait(timeout=10)

    hooks["find-owned"] = hold
    other = DeploymentService(Database(db.path.parent, service.config.secret_key), service.config, service.client_factory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.redeploy, job["id"], job["name"])
        assert entered.wait(timeout=5)
        try:
            with pytest.raises(DeploymentError):
                other.redeploy(job["id"], job["name"])
        finally:
            release.set()
        replacement = first.result(timeout=10)
    assert sum(call[0] == "find-owned" for call in calls) == 1
    assert len(db.list()) == 2
    assert other.redeploy(job["id"], job["name"])["id"] == replacement["id"]


def test_sensor_replacement_insert_failure_rolls_back_claim_transfer(sensor_job):
    service, db, spec, _, _, _ = sensor_job
    job = service.enqueue(spec)
    original = db.get(job["id"], private=True)["secrets"]
    db.update(job["id"], status="failed")
    plan = db.begin_sensor_redeployment(job["id"], job["name"], original["corelight_sensor"], reuse=True)
    db.create(plan["replacement_id"], {**spec, "vms": []}, {})
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.create(plan["replacement_id"], spec, original, parent_id=job["id"], sensor_replacement=plan)
    assert db.get(job["id"])["status"] == "cleaning"
    assert db.sensor_redeployment(job["id"])["operation_id"] == plan["operation_id"]
    assert not db.sensor_pairing_token_used(spec["sensor_pairing_token"], deployment_id=job["id"], replacement_parent_id=job["id"])


def test_sensor_transactional_event_failure_rolls_back_child_parent_and_claim(sensor_job):
    service, db, spec, _, _, _ = sensor_job
    job = service.enqueue(spec)
    original = db.get(job["id"], private=True)["secrets"]
    db.update(job["id"], status="failed")
    plan = db.begin_sensor_redeployment(job["id"], job["name"], original["corelight_sensor"], reuse=True)
    with db.connect() as connection:
        connection.execute("CREATE TRIGGER fail_queued_event BEFORE INSERT ON events WHEN NEW.message='Deployment queued' "
                           "BEGIN SELECT RAISE(ABORT, 'injected queued event failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected queued event failure"):
        db.create(plan["replacement_id"], spec, original, parent_id=job["id"], sensor_replacement=plan)
    assert db.get(plan["replacement_id"]) is None and db.get(job["id"])["status"] == "cleaning"
    assert db.sensor_redeployment(job["id"])["operation_id"] == plan["operation_id"]
    assert not db.sensor_pairing_token_used(spec["sensor_pairing_token"], deployment_id=job["id"], replacement_parent_id=job["id"])


def test_sensor_stale_attempt_cannot_finish_or_reopen_a_replacement(sensor_job):
    service, db, spec, _, _, _ = sensor_job
    job = service.enqueue(spec)
    original = db.get(job["id"], private=True)["secrets"]
    db.update(job["id"], status="failed")
    old = db.begin_sensor_redeployment(job["id"], job["name"], original["corelight_sensor"], reuse=True)
    db.recover()
    current = db.begin_sensor_redeployment(job["id"], job["name"], original["corelight_sensor"], reuse=True)
    assert not db.fail_sensor_redeployment(job["id"], old["operation_id"], "stale error")
    with pytest.raises(SensorPairingError, match="reservation changed"):
        db.create(old["replacement_id"], spec, original, parent_id=job["id"], sensor_replacement=old)
    db.create(current["replacement_id"], spec, original, parent_id=job["id"], sensor_replacement=current)
    assert not db.fail_sensor_redeployment(job["id"], current["operation_id"], "late error")
    assert db.get(job["id"])["status"] == "reverted"


def test_sensor_postcommit_response_failure_returns_existing_child_without_duplicate_cleanup(sensor_job, monkeypatch):
    service, db, spec, calls, _, _ = sensor_job
    job = service.enqueue(spec)
    db.update(job["id"], status="failed")
    enqueue = service.enqueue

    def commit_then_fail(*args, **kwargs):
        enqueue(*args, **kwargs)
        raise RuntimeError("Injected response failure after commit")

    monkeypatch.setattr(service, "enqueue", commit_then_fail)
    replacement = service.redeploy(job["id"], job["name"])
    assert db.get(job["id"])["status"] == "reverted"
    count = len(calls)
    assert service.redeploy(job["id"], job["name"])["id"] == replacement["id"]
    assert len(calls) == count and len(db.list()) == 2
