import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from gdeploy.corelight_sensor import CorelightSensorError, CorelightSensorManager, fleet_url
from gdeploy.db import Database, SensorPairingError
from gdeploy.main import create_app
from gdeploy.models import DeploymentSpec


ENDPOINT = "/api/settings/corelight-sensor"


def settings(**changes):
    return {
        "repository_token": "sensor-repository-secret", "community_string": "sensor-community-secret",
        "license_key": "sensor-license-secret", "fleet_url": "https://fleet.example.test",
        "server_sslname": "fleet.example.test", **changes,
    }


def sensor_spec(spec, **changes):
    return {
        **spec, "sensor_pairing_token": "unique-pairing-secret",
        "vms": [{**spec["vms"][0], "name": "lab-sensor", "role": "corelight_sensor", "cpu": 4,
                 "ram_gb": 16, "disk_gb": 600, "monitor_network": "Capture", "dhcp_reserved": True, **changes}],
    }


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_sensor_settings_require_authentication_and_initial_account_setup(client, signed_in, method):
    signed_in.cookies.clear()
    assert client.request(method, ENDPOINT, json={}).status_code == 401
    login = signed_in.post("/api/login", json={"username": "admin", "password": "test-admin-passphrase"})
    signed_in.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    with signed_in.app.state.db.connect() as connection:
        connection.execute("UPDATE administrator SET must_change_credentials=1")
    assert signed_in.request(method, ENDPOINT, json={}).status_code == 403


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_sensor_settings_writes_require_csrf(signed_in, method):
    signed_in.headers.pop("X-CSRF-Token")
    assert signed_in.request(method, ENDPOINT, json=settings()).status_code == 403
    assert signed_in.app.state.corelight_sensor.selected() is None


def test_sensor_defaults_encrypted_masked_retained_and_reopened(signed_in, config):
    original = settings(api_network="192.0.2.3/24")
    response = signed_in.put(ENDPOINT, json=original)
    assert response.status_code == 200
    result = response.json()
    assert result["configured"] and result["ready"]
    assert result["fleet_url"] == "https://fleet.example.test:1443"
    assert result["api_network"] == "192.0.2.0/24"
    assert signed_in.get("/api/settings").json()["corelight_sensor_configured"]
    db = signed_in.app.state.db
    for key in ("repository_token", "community_string", "license_key"):
        assert result[key + "_configured"] and original[key] not in response.text
        assert original[key].encode() not in db.path.read_bytes()
    assert signed_in.put(ENDPOINT, json={"repository_token": "", "community_string": "", "license_key": ""}).json() == result
    with TestClient(create_app(config, start_worker=False)) as reopened:
        assert reopened.app.state.corelight_sensor.catalog() == result
        assert reopened.app.state.corelight_sensor.selected()["license_key"] == original["license_key"]
    assert signed_in.delete(ENDPOINT).status_code == 200
    assert not signed_in.get(ENDPOINT).json()["configured"]
    assert not signed_in.get("/api/settings").json()["corelight_sensor_configured"]


@pytest.mark.parametrize("changes", [
    {"repository_token": "bad secret"}, {"repository_token": "bad:secret"}, {"repository_token": "bad\nsecret"},
    {"community_string": "bad\nsecret"}, {"license_key": "bad\nlicense"},
    {"fleet_url": "http://fleet.test"}, {"fleet_url": "https://secret:password@fleet.test:1443"},
    {"fleet_url": "https://fleet.test:0"}, {"fleet_url": "https://fleet.test/?token=private"},
    {"fleet_url": "https://fleet.test/#private"}, {"server_sslname": "https://fleet.test"},
    {"server_sslname": "bad\nname"}, {"api_network": "::/0"}, {"api_network": "not-a-network"},
    {"pairing_token": "must-not-save"}, {"licensing_mode": "fleet"}, {"mode": "offline"},
    {"repository_token": "x" * 4097}, {"license_key": "x" * 65537}, {"repository_token": None},
])
def test_invalid_sensor_defaults_never_echo_secret_or_replace_saved_settings(signed_in, changes):
    before = signed_in.put(ENDPOINT, json=settings()).json()
    response = signed_in.put(ENDPOINT, json=changes)
    assert response.status_code in {400, 422}
    for key in ("repository_token", "community_string", "license_key", "pairing_token"):
        value = changes.get(key)
        if isinstance(value, str):
            assert value not in response.text
    assert signed_in.get(ENDPOINT).json() == before


@pytest.mark.parametrize("missing", ["repository_token", "community_string", "license_key", "fleet_url", "server_sslname"])
def test_required_sensor_configuration_cannot_be_omitted_on_first_save(signed_in, missing):
    values = settings()
    del values[missing]
    assert signed_in.put(ENDPOINT, json=values).status_code == 400
    assert signed_in.app.state.corelight_sensor.selected() is None


@pytest.mark.parametrize("url,expected", [
    ("https://fleet.test", "https://fleet.test:1443"),
    ("https://fleet.test:1443/", "https://fleet.test:1443"),
    ("https://192.0.2.2:8443/fleet", "https://192.0.2.2:8443/fleet"),
    ("https://[2001:db8::1]/", "https://[2001:db8::1]:1443"),
])
def test_pairing_url_keeps_explicit_port_and_path(url, expected):
    assert fleet_url(url) == expected


@pytest.mark.parametrize("changes", [
    {"cpu": 3}, {"ram_gb": 15}, {"disk_gb": 549}, {"monitor_network": None},
    {"monitor_network": ""}, {"dhcp_reserved": False}, {"dhcp_reserved": "true"},
])
def test_sensor_vm_requires_capacity_two_interfaces_and_reserved_address(spec, changes):
    with pytest.raises(ValueError):
        DeploymentSpec(**sensor_spec(spec, **changes))


def test_sensor_vm_allows_same_port_group_and_static_management(spec):
    value = sensor_spec(spec, monitor_network="VM Network")
    assert DeploymentSpec(**value).vms[0].monitor_network == "VM Network"
    value = sensor_spec(spec, ip_mode="static", address="192.0.2.10/24", gateway="192.0.2.1", dns=["192.0.2.53"], dhcp_reserved=False)
    assert DeploymentSpec(**value).vms[0].address == "192.0.2.10/24"


@pytest.mark.parametrize("token", [None, 1, [], "bad secret", "bad\nsecret", "é", "x" * 4097])
def test_pairing_request_validation_does_not_echo_token(signed_in, spec, token):
    response = signed_in.post("/api/preflight", json={**sensor_spec(spec), "sensor_pairing_token": token})
    assert response.status_code == 422
    if isinstance(token, str):
        assert token not in response.text


def test_sensor_pairing_token_never_becomes_a_global_default(config):
    db = Database(config.data_dir, config.secret_key)
    manager = CorelightSensorManager(db)
    with pytest.raises(CorelightSensorError, match="fresh pairing token"):
        manager.save({**settings(), "pairing_token": "private"})
    assert manager.selected() is None


def test_pairing_token_claim_is_atomic_persistent_and_keeps_public_specs_clean(config, spec):
    db = Database(config.data_dir, config.secret_key)
    manager = CorelightSensorManager(db)
    manager.save(settings())
    requested = sensor_spec(spec)
    snapshot = manager.snapshot(requested["sensor_pairing_token"])

    def enqueue(identity):
        try:
            db.create(identity, requested, {"corelight_sensor": snapshot})
            return identity
        except SensorPairingError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue, ["sensor-one", "sensor-two"]))
    created = [value for value in results if value]
    assert len(created) == 1 and len(db.list()) == 1
    job = db.get(created[0])
    assert "sensor_pairing_token" not in job["spec"]
    assert snapshot["pairing_token"] not in json.dumps(job)
    assert snapshot["pairing_token"].encode() not in db.path.read_bytes()
    assert db.get(created[0], private=True)["secrets"]["corelight_sensor"] == snapshot
    db.update(created[0], status="completed")
    db.set_visibility(created[0], True)
    reopened = Database(config.data_dir, config.secret_key)
    assert reopened.sensor_pairing_token_used(snapshot["pairing_token"])
    assert not reopened.sensor_pairing_token_used(snapshot["pairing_token"], deployment_id=created[0])
    assert not reopened.sensor_pairing_token_used("new-unused-token")
    before = copy.deepcopy(db.get(created[0], private=True))
    with pytest.raises(CorelightSensorError, match="already assigned"):
        manager.require_unused_token(snapshot["pairing_token"])
    assert db.get(created[0], private=True) == before
