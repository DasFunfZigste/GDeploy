"""Stopping is durable and cooperative: preserve resources and finish active work safely."""

import copy
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gdeploy.db import Database, DeploymentStopError, DeploymentVisibilityError, MediaStateError
from gdeploy.service import DeploymentError, DeploymentService


def create_job(db, spec, *, status="queued"):
    identifier = str(uuid.uuid4())
    secret_data = {
        "esxi": {"host": "esxi.test", "username": "root", "password": "esxi-private"},
        "vm_credentials": {
            vm["name"]: {"username": "gdeploy", "password": "guest-private", "private_key": "ssh-private",
                         "public_key": "ssh-public", "host_key": "pinned-key", "services": []}
            for vm in spec["vms"]
        },
        "software": {},
    }
    db.create(identifier, spec, secret_data)
    if status != "queued":
        db.update(identifier, status=status)
    return identifier


def finish(db, identifier, status="completed", error=None):
    current = db.get(identifier, private=True)
    return db.finish(identifier, status, vms=current["vms"], resources=current["resources"],
                     secrets=current["secrets"], error=error)


def test_queued_stop_is_immediate_idempotent_private_and_never_claimed(config, spec):
    db = Database(config.data_dir, config.secret_key)
    identifier = create_job(db, spec)
    original = db.get(identifier, private=True)
    service = DeploymentService(db, config)
    stopped = service.request_stop(identifier)
    events = db.events(identifier)
    assert stopped["status"] == stopped["stage"] == "stopped"
    assert service.request_stop(identifier) == stopped
    assert db.events(identifier) == events
    assert db.claim() is None
    service.run(identifier)
    saved = db.get(identifier, private=True)
    assert saved["secrets"] == original["secrets"] and saved["resources"] == original["resources"]
    assert saved["vms"] == original["vms"]
    assert "guest-private" not in json.dumps(stopped) and "ssh-private" not in json.dumps(events)
    db.set_visibility(identifier, True)
    assert db.list() == []
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit WHERE action LIKE '%stopped the queued%'").fetchone()[0] == 1


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted", "cleanup_failed", "reverted", "cleaning"])
def test_stop_never_changes_terminal_or_cleanup_records(config, spec, status):
    db = Database(config.data_dir, config.secret_key)
    identifier = create_job(db, spec, status=status)
    original = db.get(identifier, private=True)
    with pytest.raises(DeploymentStopError):
        db.request_stop(identifier)
    assert db.get(identifier, private=True) == original
    assert db.request_stop(str(uuid.uuid4())) is None


@pytest.mark.parametrize("outcome", ["completed", "failed", "interrupted"])
def test_running_stop_protects_visibility_and_worker_finish_acknowledges(config, spec, outcome):
    db = Database(config.data_dir, config.secret_key)
    identifier = create_job(db, spec, status="running")
    stopped = db.request_stop(identifier)
    assert stopped["status"] == stopped["stage"] == "stopping"
    assert db.request_stop(identifier) == stopped
    with pytest.raises(DeploymentVisibilityError):
        db.set_visibility(identifier, True)
    assert not db.progress_stage(identifier, "installing_software", "Unexpected next stage")
    assert db.get(identifier)["stage"] == "stopping"
    result = finish(db, identifier, outcome, "sanitized operation failure" if outcome == "failed" else None)
    assert result["status"] == result["stage"] == "stopped"
    assert finish(db, identifier, "failed", "stale worker") == result
    assert db.set_visibility(identifier, True)["hidden_at"]
    messages = [event["message"] for event in db.events(identifier)]
    assert sum(message.startswith("Deployment stopped.") for message in messages) == 1
    assert not any(message.startswith("Deployment completed.") for message in messages)
    if outcome == "failed":
        assert any("sanitized operation failure" in message for message in messages)


@pytest.mark.parametrize("race", ["claim", "complete"])
def test_stop_serializes_with_claim_and_completion_across_database_instances(config, spec, race):
    db = Database(config.data_dir, config.secret_key)
    other = Database(config.data_dir, config.secret_key)
    for _ in range(8):
        identifier = create_job(db, spec, status="queued" if race == "claim" else "running")
        barrier = threading.Barrier(2)

        def request():
            barrier.wait(timeout=5)
            try:
                return other.request_stop(identifier)["status"]
            except DeploymentStopError:
                return "conflict"

        def work():
            barrier.wait(timeout=5)
            return db.claim() if race == "claim" else finish(db, identifier)["status"]

        with ThreadPoolExecutor(max_workers=2) as executor:
            stop_future, work_future = executor.submit(request), executor.submit(work)
            stop_result, work_result = stop_future.result(timeout=10), work_future.result(timeout=10)
        status = db.deployment_status(identifier)
        if race == "claim":
            assert (stop_result, work_result, status) in {
                ("stopped", None, "stopped"), ("stopping", identifier, "stopping"),
            }
            if status == "stopping":
                finish(db, identifier)
        else:
            assert (stop_result, work_result, status) in {
                ("conflict", "completed", "completed"), ("stopping", "stopped", "stopped"),
            }
        assert db.claim() is None


def test_stopping_media_and_all_packages_remain_protected_until_acknowledgement(config, spec):
    db = Database(config.data_dir, config.secret_key)
    values = []
    for name in ("installer.iso", "splunk.tgz", "fleet.deb", "dependency.deb"):
        path = config.data_dir / name
        path.write_bytes(b"test installer")
        values.append({"id": name, "name": name, "path": str(path), "source": "upload", "sha256": "a" * 64})
    media, splunk, fleet, dependency = values
    db.set_media_settings(media, uploaded=True)
    db.set_splunk_package_settings(splunk, uploaded=True)
    db.register_fleetmanager_package(fleet)
    db.register_fleetmanager_package(dependency)
    identifier = str(uuid.uuid4())
    db.create(identifier, spec, {"os_media": media, "splunk_package": splunk,
                                "fleetmanager": {"mode": "offline", "package": fleet, "dependencies": [dependency]}})
    db.clear_media_settings()
    db.clear_splunk_package_settings()
    db.update(identifier, status="running")
    db.request_stop(identifier)
    deletions = [(db.delete_media, media), (db.delete_splunk_package, splunk),
                 (db.delete_fleetmanager_package, fleet), (db.delete_fleetmanager_package, dependency)]
    for delete, item in deletions:
        with pytest.raises(MediaStateError, match="pending stop"):
            delete(item["id"], lambda value: pytest.fail("Deleted an active installer"))
    assert db.media_storage_state()["references"] == [media]
    assert db.splunk_package_storage_state()["references"] == [splunk]
    assert db.fleetmanager_storage_state()["references"] == [fleet, dependency]
    finish(db, identifier)
    for delete, item in deletions:
        assert delete(item["id"], lambda value: Path(value["path"]).unlink())


def test_restart_recovers_pending_stop_without_resuming_or_claiming_remote_work_finished(config, spec):
    db = Database(config.data_dir, config.secret_key)
    identifier = create_job(db, spec, status="running")
    resource = {"datastore": "datastore1", "path": f"gdeploy/{identifier}/vm.iso"}
    original = db.get(identifier, private=True)
    original["vms"][0].update(vm_id="vm-1", status="installing_software")
    db.update(identifier, resources=[resource], vms=original["vms"])
    db.request_stop(identifier)
    reopened = Database(config.data_dir, config.secret_key)
    reopened.recover()
    current = reopened.get(identifier, private=True)
    assert current["status"] == "interrupted" and "stop was pending" in current["error"]
    assert "may still be running" in current["error"]
    assert current["resources"] == [resource] and current["secrets"] == original["secrets"]
    assert current["vms"][0]["vm_id"] == "vm-1"
    assert reopened.claim() is None


@pytest.fixture
def worker(config, spec, monkeypatch):
    import gdeploy.service as module

    db = Database(config.data_dir, config.secret_key)
    calls = []
    control = {"callback": lambda step: None, "ip": "192.0.2.10"}

    def mark(step):
        calls.append(step)
        control["callback"](step)

    class ESXi:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            mark("esxi-exit")

        def upload_iso(self, *args):
            mark("upload")

        def create_vm(self, vm, *args):
            mark("create")
            return "vm-" + vm["name"]

        def power_on(self, *args):
            mark("power")

        def guest_ip(self, *args):
            mark("guest-ip")
            return control["ip"]

        def detach_iso(self, *args):
            mark("detach")

        def delete_iso(self, *args):
            mark("delete-iso")

    class Guest:
        host_key = "pinned-key"

        def __init__(self, *args):
            self.check, self.wait = lambda: None, None

        def set_cancellation_hooks(self, check, wait):
            self.check, self.wait = check, wait

        def __enter__(self):
            self.check()
            return self

        def __exit__(self, *args):
            mark("guest-exit")

        def wait_ready(self, timeout, *, log):
            mark("ready")
            if control.get("wait_ready"):
                self.wait(60)
            self.check()

        def install(self, *args, **kwargs):
            self.check()
            mark("install")
            return {"services": [{"name": "Application", "url": "https://192.0.2.10", "username": "admin",
                                  "password": "new-generated-admin-password"}],
                    "elastic": {"service_token": "new-service-token"}}

    def build(source, output, *args, **kwargs):
        output.write_bytes(b"prepared ISO")
        mark("build")

    monkeypatch.setattr(module, "GuestSession", Guest)
    monkeypatch.setattr(module, "build_seed_iso", build)
    service = DeploymentService(db, config, ESXi)
    monkeypatch.setattr(service, "preflight", lambda *args, **kwargs: {"ok": True, "checks": []})
    prepared = copy.deepcopy(spec)
    prepared["vms"].append(dict(prepared["vms"][0], name="lab-kibana", role="kibana"))
    identifier = create_job(db, prepared)
    assert db.claim() == identifier
    return service, db, identifier, calls, control


@pytest.mark.parametrize("step", ["build", "upload", "create", "power", "ready", "detach", "install"])
def test_worker_stops_between_operations_and_preserves_owned_resources(worker, step):
    service, db, identifier, calls, control = worker
    before = db.get(identifier, private=True)

    def request(current):
        if current == step:
            assert service.request_stop(identifier)["status"] == "stopping"
        if current in {"guest-exit", "esxi-exit"} and step in calls:
            assert db.deployment_status(identifier) == "stopping"

    control["callback"] = request
    service.run(identifier)
    result = db.get(identifier, private=True)
    assert result["status"] == result["stage"] == "stopped"
    assert result["error"] is None
    assert result["secrets"]["vm_credentials"][before["vms"][0]["name"]]["password"] == "guest-private"
    assert not (service.config.data_dir / "artifacts" / identifier).exists()
    assert calls[-1] == "esxi-exit"
    assert "destroy" not in calls and "power-off" not in calls
    if step in {"create", "power", "ready", "detach", "install"}:
        assert result["vms"][0]["vm_id"]
    if step in {"upload", "create", "power", "ready", "detach"}:
        assert len(result["resources"]) == 1 and "delete-iso" not in calls
    if step == "create":
        assert "power" not in calls
    if step == "install":
        assert calls.count("install") == 1
        assert result["vms"][1]["status"] == "os_ready"
        assert result["secrets"]["vm_credentials"][result["vms"][0]["name"]]["services"][0]["password"] == "new-generated-admin-password"
        assert result["secrets"]["elastic"]["service_token"] == "new-service-token"
        assert "new-generated-admin-password" not in json.dumps(db.get(identifier))
    else:
        assert "install" not in calls
        assert not result["vms"][1].get("vm_id")


@pytest.mark.parametrize("step", ["build", "upload", "create", "install"])
def test_stop_stays_pending_until_active_work_finishes(worker, step):
    service, db, identifier, calls, control = worker
    entered, release = threading.Event(), threading.Event()

    def block(current):
        if current == step:
            entered.set()
            assert release.wait(5), "Test failed to release active operation"

    control["callback"] = block
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.run, identifier)
        try:
            assert entered.wait(5)
            assert service.request_stop(identifier)["status"] == "stopping"
            assert not future.done()
            with pytest.raises(DeploymentVisibilityError):
                db.set_visibility(identifier, True)
            with pytest.raises(DeploymentError, match="Only failed"):
                service.redeploy(identifier, "Lab")
        finally:
            release.set()
        future.result(timeout=5)
    assert db.deployment_status(identifier) == "stopped"
    assert calls.count(step) == 1


@pytest.mark.parametrize("waiting", ["no-ip", "static-mismatch", "readiness"])
def test_os_waits_wake_promptly_and_explain_network_waits(worker, waiting):
    service, db, identifier, calls, control = worker
    entered = threading.Event()
    if waiting == "no-ip":
        control["ip"] = None
    elif waiting == "static-mismatch":
        current = db.get(identifier, private=True)
        current["vms"][0]["address"] = "192.0.2.99/24"
        db.update(identifier, vms=current["vms"])
    else:
        control["wait_ready"] = True
    target = "ready" if waiting == "readiness" else "guest-ip"
    control["callback"] = lambda step: entered.set() if step == target else None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.run, identifier)
        assert entered.wait(5)
        service.request_stop(identifier)
        future.result(timeout=2)
    assert db.deployment_status(identifier) == "stopped"
    assert "install" not in calls and "detach" not in calls


@pytest.mark.parametrize("reported, expected, phrase", [
    (None, None, "has not reported a guest IP"),
    ("192.0.2.10", "192.0.2.99/24", "requested static IP is 192.0.2.99"),
])
def test_wait_diagnostics_are_actionable_and_not_repeated_each_poll(worker, monkeypatch, reported, expected, phrase):
    service, db, identifier, calls, control = worker
    control["ip"] = reported
    current = db.get(identifier, private=True)
    current["vms"][0]["address"] = expected
    db.update(identifier, vms=current["vms"])
    polls = []

    def tick(job, duration):
        polls.append(duration)
        if len(polls) == 3:
            service.request_stop(job)
            service._check_stop(job)

    monkeypatch.setattr(service, "_wait", tick)
    service.run(identifier)
    messages = [event["message"] for event in db.events(identifier)]
    assert sum(phrase in message for message in messages) == 1
    assert db.deployment_status(identifier) == "stopped"


def test_stop_racing_final_esxi_context_exit_cannot_be_lost(worker):
    service, db, identifier, _, control = worker
    control["callback"] = lambda step: service.request_stop(identifier) if step == "esxi-exit" else None
    service.run(identifier)
    assert db.deployment_status(identifier) == "stopped"
    assert all(vm["status"] == "completed" for vm in db.get(identifier)["vms"])
    assert not any(event["message"].startswith("Deployment completed.") for event in db.events(identifier))


def test_failed_active_operation_after_stop_keeps_error_redacted_and_resources_retained(worker):
    service, db, identifier, calls, control = worker

    def fail(step):
        if step == "upload":
            service.request_stop(identifier)
            raise RuntimeError("Upload failed for esxi-private guest-private")

    control["callback"] = fail
    service.run(identifier)
    saved = db.get(identifier, private=True)
    assert saved["status"] == "stopped" and len(saved["resources"]) == 1
    assert "create" not in calls and "delete-iso" not in calls
    visible = json.dumps(db.get(identifier)) + json.dumps(db.events(identifier))
    assert "esxi-private" not in visible and "guest-private" not in visible
    assert "Upload failed for [redacted] [redacted]" in saved["error"]


def test_pending_stop_keeps_uncreated_names_addresses_and_capacity_reserved(worker, spec, monkeypatch):
    service, db, identifier, _, _ = worker
    current = db.get(identifier, private=True)
    current["vms"][0]["address"] = "192.0.2.99/24"
    db.update(identifier, vms=current["vms"])
    db.request_stop(identifier)

    class Inventory:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def inventory(self):
            return {"host": {"cpu_threads": 32, "memory_gb": 20}, "vms": [],
                    "datastores": [{"name": "datastore1", "free_gb": 1000}],
                    "networks": [{"name": "VM Network"}]}

    monkeypatch.setattr(service, "client", lambda settings: Inventory())
    candidate = copy.deepcopy(spec)
    candidate["vms"][0]["address"] = "192.0.2.99/24"
    result = DeploymentService.preflight(service, candidate, settings={"host": "esxi.test"})
    checks = {item["name"]: item for item in result["checks"]}
    assert not checks["VM names"]["ok"]
    assert not checks["Static address reservations"]["ok"]
    assert not checks["Memory capacity"]["ok"]


def test_stopped_record_allows_explicit_delete_redeploy(worker, monkeypatch):
    service, db, identifier, _, _ = worker
    service.request_stop(identifier)
    service.run(identifier)
    assert db.deployment_status(identifier) == "stopped"
    calls = []

    class Cleanup:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def find_owned_vms(self, owner):
            calls.append(owner)
            return []

    monkeypatch.setattr(service, "client", lambda settings: Cleanup())
    monkeypatch.setattr(service, "enqueue", lambda *args, **kwargs: {"id": "replacement"})
    assert service.redeploy(identifier, "Lab") == {"id": "replacement"}
    assert calls == [identifier] and db.deployment_status(identifier) == "reverted"


def test_stop_api_requires_authentication(client, spec):
    db = client.app.state.db
    identifier = create_job(db, spec)
    assert client.post(f"/api/deployments/{identifier}/stop").status_code == 401
    assert db.deployment_status(identifier) == "queued"


def test_stop_api_validates_csrf_id_and_terminal_conflicts(signed_in, spec):
    db = signed_in.app.state.db
    identifier = create_job(db, spec)
    url = f"/api/deployments/{identifier}/stop"
    csrf = signed_in.headers.pop("X-CSRF-Token")
    assert signed_in.post(url).status_code == 403
    signed_in.headers["X-CSRF-Token"] = csrf
    assert signed_in.post("/api/deployments/invalid/stop").status_code == 422
    assert signed_in.post(f"/api/deployments/{uuid.uuid4()}/stop").status_code == 404
    response = signed_in.post(url)
    assert response.status_code == 200 and response.json()["status"] == "stopped"
    assert response.json()["events"][-1]["level"] == "audit"
    assert "guest-private" not in response.text and "ssh-private" not in response.text
    assert signed_in.post(url).json() == response.json()
    other = create_job(db, spec, status="completed")
    assert signed_in.post(f"/api/deployments/{other}/stop").status_code == 409


def test_stop_api_is_not_blocked_by_slow_enqueue_or_redeploy_lock(signed_in, spec):
    service = signed_in.app.state.service
    identifier = create_job(service.db, spec, status="running")
    with ThreadPoolExecutor(max_workers=1) as executor:
        with service.action_lock:
            future = executor.submit(signed_in.post, f"/api/deployments/{identifier}/stop")
            response = future.result(timeout=2)
    assert response.status_code == 200 and response.json()["status"] == "stopping"
