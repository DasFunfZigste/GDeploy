import uuid

import pytest
from fastapi.testclient import TestClient

from gdeploy.main import create_app

PATH = "/api/settings/deployment-defaults"
HOST = "esxi.example.test"
CONNECTION = {"host": HOST, "username": "root", "password": "private-host-password", "verify_tls": True}


@pytest.fixture
def inventory(signed_in, monkeypatch):
    signed_in.app.state.db.set_settings(CONNECTION)
    data = {"networks": [{"name": "Servers"}, {"name": "Management"}]}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def inventory(self):
            return data

    monkeypatch.setattr(signed_in.app.state.service, "client", lambda settings: Client())
    return data


def save(client, network="Servers", host=HOST):
    return client.put(PATH, json={"host": host, "default_network": network})


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_default_network_requires_authentication(client, method):
    response = client.request(method, PATH, json={"host": HOST, "default_network": "Servers"} if method == "PUT" else None)
    assert response.status_code == 401
    assert client.app.state.db.deployment_defaults() is None


def test_empty_defaults_can_be_read_without_host_or_inventory(signed_in, monkeypatch):
    monkeypatch.setattr(signed_in.app.state.service, "client", lambda *args: pytest.fail("Reading defaults must not query ESXi"))
    expected = {"host": None, "default_network": None, "applies_to_host": False}
    assert signed_in.get(PATH).json() == expected
    assert signed_in.get("/api/settings").json()["deployment_defaults"] == expected
    assert save(signed_in).status_code == 400


def test_default_is_host_bound_persistent_and_never_changes_existing_specs(signed_in, inventory, config, spec):
    db = signed_in.app.state.db
    identifier = str(uuid.uuid4())
    db.create(identifier, spec, {"esxi": CONNECTION, "private_key": "retained-key"})
    before = db.get(identifier, private=True)
    expected = {"host": HOST, "default_network": "Servers", "applies_to_host": True}
    response = save(signed_in)
    assert response.status_code == 200 and response.json() == expected
    assert response.headers["Cache-Control"] == "no-store"
    assert "private-host-password" not in response.text
    assert signed_in.get("/api/settings").json()["deployment_defaults"] == expected
    with db.connect() as connection:
        encoded = connection.execute("SELECT value FROM deployment_defaults").fetchone()[0]
    assert HOST not in encoded and "Servers" not in encoded
    with TestClient(create_app(config, start_worker=False)) as restarted:
        restarted.post("/api/login", json={"username": "admin", "password": "test-admin-passphrase"})
        assert restarted.get(PATH).json() == expected
    assert save(signed_in, "Management").status_code == 200
    assert db.get(identifier, private=True) == before
    assert signed_in.delete(PATH).json() == {"host": HOST, "default_network": None, "applies_to_host": False}
    assert signed_in.delete(PATH).status_code == 200
    assert db.get(identifier, private=True) == before


def test_host_change_does_not_apply_old_default_and_stale_form_cannot_replace_it(signed_in, inventory):
    assert save(signed_in).status_code == 200
    response = signed_in.put("/api/settings", json=dict(CONNECTION, host="other.example.test"))
    assert response.status_code == 200
    expected = {"host": HOST, "default_network": "Servers", "applies_to_host": False}
    assert signed_in.get(PATH).json() == expected
    assert save(signed_in, "Management").status_code == 409
    assert signed_in.get(PATH).json() == expected
    assert signed_in.put("/api/settings", json=CONNECTION).status_code == 200
    assert signed_in.get(PATH).json()["applies_to_host"] is True
    assert save(signed_in, "Management", host="ESXI.EXAMPLE.TEST.").status_code == 200


@pytest.mark.parametrize("networks", [[], [{"name": "Management"}], [{"name": "Servers"}, {"name": "Servers"}]])
def test_unavailable_or_ambiguous_group_rejected_without_replacing_saved_choice(signed_in, inventory, networks):
    assert save(signed_in, "Management").status_code == 200
    inventory["networks"] = networks
    response = save(signed_in)
    assert response.status_code == 400
    assert signed_in.get(PATH).json()["default_network"] == "Management"


def test_inventory_failure_is_redacted_and_preserves_previous_default(signed_in, inventory, monkeypatch):
    assert save(signed_in).status_code == 200

    def broken(settings):
        raise RuntimeError("private-host-password: unavailable")

    monkeypatch.setattr(signed_in.app.state.service, "client", broken)
    response = save(signed_in, "Management")
    assert response.status_code == 400
    assert "private-host-password" not in response.text
    assert signed_in.get(PATH).json()["default_network"] == "Servers"


def test_host_changed_during_inventory_is_rejected(signed_in, inventory, monkeypatch):
    db = signed_in.app.state.db
    original = signed_in.app.state.service.client

    def changed(settings):
        db.set_settings(dict(CONNECTION, host="new.example.test"))
        return original(settings)

    monkeypatch.setattr(signed_in.app.state.service, "client", changed)
    assert save(signed_in).status_code == 409
    assert db.deployment_defaults() is None


@pytest.mark.parametrize("network", ["", " ", " Servers", "Servers\n", "server\tgroup", "x" * 129, None, 123, []])
def test_invalid_values_never_replace_defaults(signed_in, inventory, network):
    assert save(signed_in).status_code == 200
    assert save(signed_in, network).status_code == 422
    assert signed_in.get(PATH).json()["default_network"] == "Servers"


def test_mutations_require_csrf(signed_in, inventory):
    assert save(signed_in).status_code == 200
    signed_in.headers.pop("X-CSRF-Token")
    assert save(signed_in, "Management").status_code == 403
    assert signed_in.delete(PATH).status_code == 403
    assert signed_in.get(PATH).json()["default_network"] == "Servers"


def test_default_never_silently_fills_an_incomplete_api_deployment(signed_in, inventory, spec):
    assert save(signed_in).status_code == 200
    del spec["vms"][0]["network"]
    assert signed_in.post("/api/deployments", json=spec).status_code == 422
    assert signed_in.app.state.db.list() == []
