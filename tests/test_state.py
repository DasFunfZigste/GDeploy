import uuid

import pytest

from gdeploy.db import Database
from gdeploy.models import DeploymentSpec, VMSpec
from gdeploy.service import DeploymentError, DeploymentService, safe_error


def test_topology_and_network_validation(spec):
    DeploymentSpec(**spec)
    bad = dict(spec, vms=[dict(spec["vms"][0], role="kibana")])
    with pytest.raises(ValueError, match="requires a separate"):
        DeploymentSpec(**bad)
    with pytest.raises(ValueError, match="license"):
        DeploymentSpec(**dict(spec, vms=[dict(spec["vms"][0], role="splunk")]))
    with pytest.raises(ValueError):
        VMSpec(**dict(spec["vms"][0], ip_mode="static", address="10.0.0.0/24", gateway="10.0.0.1", dns=["1.1.1.1"]))
    with pytest.raises(ValueError, match="Gateway"):
        VMSpec(**dict(spec["vms"][0], ip_mode="static", address="10.0.0.20/24", gateway="10.1.0.1", dns=["1.1.1.1"]))


def test_recovery_keeps_secrets_and_does_not_repeat_running_jobs(config, spec):
    db = Database(config.data_dir, config.secret_key)
    ids = [str(uuid.uuid4()) for _ in range(3)]
    for id_ in ids:
        db.create(id_, spec, {"password": "persisted-secret"})
    assert db.claim() == ids[0]
    db.update(ids[1], status="cleaning")
    db.recover()
    assert db.get(ids[0])["status"] == "interrupted"
    assert db.get(ids[1])["status"] == "cleanup_failed"
    assert db.claim() == ids[2]
    assert db.get(ids[0], private=True)["secrets"]["password"] == "persisted-secret"


def test_cleanup_failure_never_queues_replacement(config, spec, monkeypatch):
    db = Database(config.data_dir, config.secret_key)
    id_ = str(uuid.uuid4())
    db.create(id_, spec, {"esxi": {"host": "esxi", "username": "root", "password": "secret"}})
    db.update(id_, status="failed")

    class FakeESXi:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def find_owned_vms(self, owner_id):
            return [{"vm_id": "vm-1", "name": "lab-elasticsearch"}]

        def destroy_vm(self, vm_id, owner_id):
            raise RuntimeError("Ownership mismatch")

    service = DeploymentService(db, config, FakeESXi)
    monkeypatch.setattr(
        service, "enqueue", lambda *args, **kwargs: pytest.fail("Must not enqueue after cleanup failure")
    )
    with pytest.raises(DeploymentError, match="Ownership mismatch"):
        service.redeploy(id_, "Lab")
    assert db.get(id_)["status"] == "cleanup_failed"
    assert len(db.list()) == 1


def test_typed_confirmation_precedes_any_mutation(config, spec):
    db = Database(config.data_dir, config.secret_key)
    id_ = str(uuid.uuid4())
    db.create(id_, spec, {})
    db.update(id_, status="failed")
    service = DeploymentService(db, config)
    with pytest.raises(DeploymentError, match="exact deployment name"):
        service.redeploy(id_, "wrong")
    assert db.get(id_)["status"] == "failed"


def test_safe_errors_remove_known_credentials():
    data = {
        "esxi": {"password": "esxi-password"},
        "software": {"elastic_password": "elastic-password"},
        "elastic": {"service_token": "service-token-value"},
    }
    text = safe_error("esxi-password elastic-password service-token-value", data)
    assert text == "[redacted] [redacted] [redacted]"


def test_workspace_failure_is_recorded_without_killing_worker(config, spec):
    db = Database(config.data_dir, config.secret_key)
    id_ = str(uuid.uuid4())
    db.create(id_, spec, {"esxi": {}, "vm_credentials": {}, "software": {}})
    db.claim()
    (config.data_dir / "artifacts").write_text("an unexpected file")
    DeploymentService(db, config).run(id_)
    assert db.get(id_)["status"] == "failed"
    assert "directory" in db.get(id_)["error"].lower()


def test_retained_address_is_rejected_even_with_new_name(config, spec, monkeypatch):
    db = Database(config.data_dir, config.secret_key)
    id_ = str(uuid.uuid4())
    db.create(id_, spec, {})
    vm = dict(spec["vms"][0], vm_id="vm-12", ip="10.0.0.20", address="10.0.0.20/24")
    db.update(id_, status="completed", vms=[vm])

    class FakeESXi:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def inventory(self):
            return {
                "host": {"cpu_threads": 32, "memory_gb": 128, "free_memory_gb": 120},
                "vms": [{"name": vm["name"]}],
                "datastores": [{"name": "datastore1", "free_gb": 1000}],
                "networks": [{"name": "VM Network"}],
            }

    service = DeploymentService(db, config, FakeESXi)
    candidate = dict(spec, vms=[dict(vm, name="other-elasticsearch")])
    checks = service.preflight(candidate, settings={"host": "esxi"})["checks"]
    assert next(c for c in checks if c["name"] == "Static address reservations")["ok"] is False


def test_job_ownership_persistence_success_and_cleanup(config, spec, monkeypatch):
    import gdeploy.service as module

    db = Database(config.data_dir, config.secret_key)
    created = []
    calls = []

    class FakeESXi:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def upload_iso(self, datastore, remote, local):
            id_ = remote.split("/")[1]
            assert db.get(id_, private=True)["resources"] == [{"datastore": datastore, "path": remote}]
            assert local.read_bytes() == b"test-iso"
            calls.append("upload")

        def create_vm(self, vm, iso, owner):
            created.append({"vm_id": "vm-123", "name": vm["name"], "owner": owner})
            calls.append("create")
            return "vm-123"

        def power_on(self, id_, owner):
            assert db.get(owner)["vms"][0]["vm_id"] == id_
            calls.append("power")

        def guest_ip(self, id_):
            return "10.0.0.20"

        def detach_iso(self, id_, owner):
            calls.append("detach")

        def delete_iso(self, datastore, remote, owner):
            calls.append("delete-iso")

        def find_owned_vms(self, owner):
            return [v for v in created if v["owner"] == owner]

        def destroy_vm(self, vm_id, owner):
            assert any(v["owner"] == owner for v in created)
            created.clear()
            calls.append("destroy")

    class FakeGuest:
        host_key = "ssh-rsa expected-key"

        def __init__(self, ip, username, key, password, known=None):
            if known:
                assert known == self.host_key

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def wait_ready(self, timeout):
            calls.append("ready")

        def install(self, role, secrets, **kwargs):
            calls.append("install")
            return {
                "services": [
                    {
                        "name": "Elasticsearch",
                        "url": "https://10.0.0.20:9200",
                        "username": "elastic",
                        "password": secrets["elastic_password"],
                    }
                ],
                "elastic": {"ip": "10.0.0.20", "service_token": "test-token", "ca_pem": "test-ca", "version": "9.1.0"},
            }

    monkeypatch.setattr(module, "GuestSession", FakeGuest)
    monkeypatch.setattr(module, "generate_ssh_key", lambda: ("test-private", "test-public"))
    monkeypatch.setattr(
        module, "build_seed_iso", lambda source, destination, *args, **kwargs: destination.write_bytes(b"test-iso")
    )
    service = DeploymentService(db, config, FakeESXi)
    preflight_calls = []
    monkeypatch.setattr(
        service, "preflight", lambda *args, **kwargs: preflight_calls.append(kwargs) or {"ok": True, "checks": []}
    )
    item = service.enqueue(spec, {"host": "esxi", "username": "root", "password": "secret"})
    db.claim()
    service.run(item["id"])
    finished = db.get(item["id"])
    assert finished["status"] == "completed"
    assert calls == ["upload", "create", "power", "ready", "detach", "delete-iso", "install"]
    assert "password" not in str(finished)
    old_password = service.credentials(item["id"])["vms"][0]["password"]
    db.update(item["id"], status="failed")
    replacement = service.redeploy(item["id"], "Lab")
    assert calls[-1] == "destroy"
    assert db.get(item["id"])["status"] == "reverted"
    assert replacement["status"] == "queued" and replacement["parent_id"] == item["id"]
    assert service.credentials(replacement["id"])["vms"][0]["password"] != old_password
    assert preflight_calls[-1]["exclude_id"] == item["id"]
