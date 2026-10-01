import uuid

import pytest

from gdeploy.db import Database
from gdeploy.guest import GuestError, GuestHostKeyError
from gdeploy.service import DeploymentService


@pytest.mark.parametrize("error", [GuestError("cloud-init failed"), GuestHostKeyError("SSH host key changed")])
def test_terminal_guest_errors_are_not_retried(config, spec, monkeypatch, error):
    import gdeploy.service as module

    db = Database(config.data_dir, config.secret_key)
    id_ = str(uuid.uuid4())
    credentials = {"username": "gdeploy", "password": "secret", "private_key": "key"}
    db.create(id_, spec, {"vm_credentials": {spec["vms"][0]["name"]: credentials}})

    class FakeESXi:
        def guest_ip(self, vm_id):
            return "10.0.0.20"

    class FailedGuest:
        host_key = "known-key"

        def __init__(self, *args):
            pass

        def __enter__(self):
            if isinstance(error, GuestHostKeyError):
                raise error
            return self

        def __exit__(self, *args):
            pass

        def wait_ready(self, timeout):
            raise error

    monkeypatch.setattr(module, "GuestSession", FailedGuest)
    service = DeploymentService(db, config)
    monkeypatch.setattr(
        service.stop_event, "wait", lambda *args: pytest.fail("Terminal guest errors must not be retried")
    )
    vm = dict(spec["vms"][0], vm_id="vm-1")
    with pytest.raises(type(error), match=str(error)):
        service._wait_for_guest(FakeESXi(), vm, credentials, id_)


def test_build_diagnostics_are_persisted_redacted_and_survive_failure(config, spec, monkeypatch):
    import gdeploy.service as module

    class FakeESXi:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    db = Database(config.data_dir, config.secret_key)
    service = DeploymentService(db, config, FakeESXi)
    monkeypatch.setattr(service, "preflight", lambda *args, **kwargs: {"ok": True, "checks": []})
    monkeypatch.setattr(module, "generate_ssh_key", lambda: ("generated-private", "generated-public"))

    def fail(source, output, vm, username, password, key, *, authorized_ssh_keys, log):
        assert authorized_ssh_keys == []
        log("Extract /boot/grub/grub.cfg", "info")
        log(f"Test diagnostic: {password} esxi-password-unique", "error")
        raise GuestError("ISO build failed during extraction: xorriso exited with status 5.")

    monkeypatch.setattr(module, "build_seed_iso", fail)
    item = service.enqueue(spec, {"host": "esxi", "username": "root", "password": "esxi-password-unique"})
    credentials = db.get(item["id"], private=True)["secrets"]["vm_credentials"]
    password = credentials[spec["vms"][0]["name"]]["password"]
    db.claim()
    service.run(item["id"])
    reopened = Database(config.data_dir, config.secret_key)
    assert reopened.get(item["id"])["status"] == "failed"
    events = reopened.events(item["id"])
    messages = "\n".join(event["message"] for event in events)
    assert "Extract /boot/grub/grub.cfg" in messages and "status 5" in messages
    assert password not in messages and "esxi-password-unique" not in messages
    assert any(event["level"] == "error" and "[redacted]" in event["message"] for event in events)
