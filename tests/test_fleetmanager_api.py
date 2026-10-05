import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from gdeploy.main import create_app
from test_fleetmanager import fake_metadata, license_pem, package_bytes


ENDPOINT = "/api/settings/fleetmanager"
ENDPOINTS = [
    ("GET", ENDPOINT), ("PUT", ENDPOINT), ("DELETE", ENDPOINT),
    ("POST", ENDPOINT + "/packages/upload?filename=fleet.deb"),
    ("DELETE", ENDPOINT + "/packages/upload_" + "0" * 32),
]


@pytest.fixture(autouse=True)
def metadata(monkeypatch):
    monkeypatch.setattr("gdeploy.fleetmanager._deb_metadata", fake_metadata)


def payload():
    return {"mode": "online", "community_string": "private-community-value", "repository_token": "private-repository-token", "license_pem": license_pem(), "license_name": "customer-fleet.pem"}


def upload(client, package="corelight-fleet", architecture="amd64", sha256=None):
    params = {"filename": package + ".deb"}
    if sha256 is not None:
        params["sha256"] = sha256
    return client.post(ENDPOINT + "/packages/upload", params=params, content=package_bytes(package, architecture))


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_fleetmanager_routes_require_authentication(client, method, path):
    assert client.request(method, path, json={"mode": "online"}).status_code == 401


@pytest.mark.parametrize("method,path", [item for item in ENDPOINTS if item[0] != "GET"])
def test_fleetmanager_writes_require_csrf(signed_in, method, path):
    signed_in.headers.pop("X-CSRF-Token")
    assert signed_in.request(method, path, json={"mode": "online"}).status_code == 403
    assert signed_in.app.state.db.fleetmanager_settings() is None


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_fleetmanager_routes_require_initial_account_setup(signed_in, method, path):
    with signed_in.app.state.db.connect() as connection:
        connection.execute("UPDATE administrator SET must_change_credentials=1")
    response = signed_in.request(method, path, json={"mode": "online"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "credentials_change_required"


def test_online_saved_settings_redacted_restart_and_blank_secret_retention(signed_in, config):
    assert not signed_in.get("/api/settings").json()["fleetmanager_configured"]
    original = payload()
    response = signed_in.put(ENDPOINT, json=original)
    assert response.status_code == 200 and response.json()["ready"]
    data = response.json()
    assert data["license"]["name"] == "customer-fleet.pem"
    for field in ["community_string", "repository_token", "license_pem"]:
        assert original[field] not in response.text
    assert signed_in.get("/api/settings").json()["fleetmanager_configured"]
    response = signed_in.put(ENDPOINT, json={"mode": "online", "community_string": "", "repository_token": "", "license_pem": ""})
    assert response.json() == data
    with TestClient(create_app(config, start_worker=False)) as restarted:
        login = restarted.post("/api/login", json={"username": "admin", "password": "test-admin-passphrase"})
        restarted.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert restarted.get(ENDPOINT).json() == data
        assert restarted.delete(ENDPOINT).status_code == 200
        assert not restarted.get(ENDPOINT).json()["ready"]
        assert not restarted.get("/api/settings").json()["fleetmanager_configured"]


def test_offline_upload_selection_and_protected_management(signed_in):
    response = upload(signed_in)
    assert response.status_code == 200
    first = response.json()
    main = first["uploaded_package_id"]
    assert not first["ready"] and first["package_id"] is None
    dependency = upload(signed_in, "libexample", "all").json()["uploaded_package_id"]
    values = {**payload(), "mode": "offline", "repository_token": "", "package_id": main, "dependency_ids": [dependency]}
    saved = signed_in.put(ENDPOINT, json=values)
    assert saved.status_code == 200 and saved.json()["ready"]
    assert signed_in.get("/api/settings").json()["fleetmanager_mode"] == "offline"
    assert not saved.json()["repository_token_configured"]
    for item in [main, dependency]:
        assert signed_in.delete(ENDPOINT + "/packages/" + item).status_code == 409
    signed_in.delete(ENDPOINT)
    for item in [main, dependency]:
        deleted = signed_in.delete(ENDPOINT + "/packages/" + item)
        assert deleted.status_code == 200
    assert deleted.json()["packages"] == []


def test_invalid_secret_values_and_pydantic_errors_do_not_echo_input(signed_in):
    original = signed_in.put(ENDPOINT, json=payload()).json()
    for changes, status in [
        ({"community_string": "private'bad-community"}, 400),
        ({"repository_token": "private token"}, 400),
        ({"license_pem": "private-bad-pem"}, 400),
        ({"repository_token": "private" * 700}, 422),
        ({"license_pem": "private" * 10000}, 422),
        ({"dependency_ids": "private-wrong-type"}, 422),
    ]:
        response = signed_in.put(ENDPOINT, json={"mode": "online", **changes})
        assert response.status_code == status
        for value in changes.values():
            assert value not in response.text
        assert signed_in.get(ENDPOINT).json() == original


def test_optional_checksum_and_mounted_selection(signed_in, config):
    checksum = hashlib.sha256(package_bytes()).hexdigest()
    assert upload(signed_in, sha256=checksum).status_code == 200
    before = signed_in.get(ENDPOINT).json()
    assert upload(signed_in, sha256="0" * 64).status_code == 400
    assert signed_in.get(ENDPOINT).json() == before
    path = config.ubuntu_iso.with_name("fleet.deb")
    path.write_bytes(package_bytes())
    item = next(item for item in signed_in.get(ENDPOINT).json()["packages"] if item["source"] == "server")
    response = signed_in.put(ENDPOINT, json={**payload(), "mode": "offline", "package_id": item["id"]})
    assert response.status_code == 200 and response.json()["ready"]
    assert signed_in.delete(ENDPOINT + "/packages/" + item["id"]).status_code == 404
    assert path.exists()
    assert signed_in.put(ENDPOINT, json={"mode": "offline", "package_id": "/etc/passwd"}).status_code == 404


def test_settings_summary_never_scans_server_packages(signed_in, monkeypatch):
    monkeypatch.setattr(signed_in.app.state.fleetmanager, "_files", lambda: pytest.fail("summary scanned packages"))
    assert signed_in.get("/api/settings").status_code == 200


def test_startup_recovers_partial_uploads_but_retains_registered_files(signed_in, config):
    upload(signed_in)
    directory = config.data_dir / "fleetmanager-packages"
    partial = directory / ("1" * 32 + ".partial")
    partial.write_bytes(b"interrupted")
    with TestClient(create_app(config, start_worker=False)) as restarted:
        assert not partial.exists()
        assert len(restarted.app.state.db.fleetmanager_files()) == 1
    with signed_in.app.state.db.connect() as connection:
        audit = json.dumps([tuple(row) for row in connection.execute("SELECT * FROM audit")])
    assert "PRIVATE KEY" not in audit and "private-community" not in audit
