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
