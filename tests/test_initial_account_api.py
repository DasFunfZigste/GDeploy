from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from gdeploy.config import hash_password
from gdeploy.main import COOKIE, create_app


NEW_ACCOUNT = {
    "username": "lab-operator",
    "password": "a-new-administrator-passphrase",
    "password_confirm": "a-new-administrator-passphrase",
}


@pytest.fixture
def default_config(config):
    return replace(config, admin_password_hash=hash_password("admin"))


@pytest.fixture
def default_client(default_config):
    with TestClient(create_app(default_config, start_worker=False)) as client:
        yield client


def sign_in_default(client):
    response = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert response.status_code == 200
    session = response.json()
    assert session["must_change_credentials"] is True
    client.headers["X-CSRF-Token"] = session["csrf_token"]
    return session


def test_setup_requires_authenticated_session(default_client):
    assert default_client.post("/api/account/setup", json=NEW_ACCOUNT).status_code == 401


def test_default_login_stays_restricted_on_session_refresh(default_client):
    original = sign_in_default(default_client)
    assert default_client.get("/api/session").json() == original
    assert default_client.get("/api/health").json() == {"status": "ok"}
    assert default_client.post("/api/logout").status_code == 200
    assert default_client.get("/api/session").json() == {"authenticated": False}


def test_every_business_endpoint_is_blocked_before_setup(default_client, spec):
    sign_in_default(default_client)
    identifier = str(uuid4())
    requests = [
        ("GET", "/api/settings", None),
        ("GET", "/api/settings/deployment-defaults", None),
        ("PUT", "/api/settings/deployment-defaults", {"host": "esxi.invalid", "default_network": "Servers"}),
        ("DELETE", "/api/settings/deployment-defaults", None),
        ("GET", "/api/settings/ssh-keys", None),
        ("PUT", "/api/settings/ssh-keys", {"public_keys": []}),
        ("PUT", "/api/settings", {"host": "esxi.invalid", "username": "root", "password": "test-only"}),
        ("GET", "/api/inventory", None),
        ("GET", "/api/deployments", None),
        ("GET", f"/api/deployments/{identifier}", None),
        ("GET", "/api/deployments?include_hidden=true", None),
        ("PATCH", f"/api/deployments/{identifier}/visibility", {"hidden": True}),
        ("POST", "/api/preflight", spec),
        ("POST", "/api/deployments", spec),
        ("POST", f"/api/deployments/{identifier}/credentials", None),
        ("POST", f"/api/deployments/{identifier}/stop", None),
        ("POST", f"/api/deployments/{identifier}/redeploy", {"confirm_name": "test"}),
    ]
    for method, path, payload in requests:
        response = default_client.request(method, path, json=payload)
        assert response.status_code == 403, path
        assert response.json()["detail"]["code"] == "credentials_change_required", path
    assert default_client.app.state.db.settings() is None
    assert default_client.app.state.db.list() == []


def test_setup_requires_csrf_and_rejects_cross_origin_login(default_client):
    response = default_client.post(
        "/api/login", json={"username": "admin", "password": "admin"}, headers={"Origin": "https://other.invalid"}
    )
    assert response.status_code == 403
    sign_in_default(default_client)
    default_client.headers.pop("X-CSRF-Token")
    assert default_client.post("/api/account/setup", json=NEW_ACCOUNT).status_code == 403
    assert default_client.post(
        "/api/account/setup", json=NEW_ACCOUNT, headers={"X-CSRF-Token": "incorrect"}
    ).status_code == 403
    assert default_client.app.state.db.administrator()["must_change_credentials"] is True


@pytest.mark.parametrize("changes", [
    {"username": "admin"},
    {"username": "AdMiN"},
    {"username": "ab"},
    {"username": "spaces forbidden"},
    {"username": "-invalid"},
    {"username": "x" * 101},
    {"password": "admin", "password_confirm": "admin"},
    {"password": " " * 12, "password_confirm": " " * 12},
    {"password_confirm": "mismatched-private-password"},
    {"password": "x" * 1025, "password_confirm": "x" * 1025},
    {"unexpected": "private-field"},
])
def test_setup_rejects_invalid_credentials_without_echoing_passwords(default_client, changes):
    sign_in_default(default_client)
    payload = {**NEW_ACCOUNT, **changes}
    response = default_client.post("/api/account/setup", json=payload)
    assert response.status_code == 422
    for name in ("password", "password_confirm"):
        if len(payload[name].strip()) >= 12:
            assert payload[name] not in response.text
    assert default_client.app.state.db.administrator()["must_change_credentials"] is True


def test_setup_invalidates_all_sessions_and_old_login_preserving_encrypted_data(default_client, default_config):
    sign_in_default(default_client)
    db = default_client.app.state.db
    other_token, other_csrf = db.new_session("admin", 8)
    db.set_settings({"host": "saved-esxi.invalid", "username": "root", "password": "existing-esxi-secret"})
    with db.connect() as connection:
        encrypted_before = connection.execute("SELECT value FROM settings").fetchone()[0]
    response = default_client.post("/api/account/setup", json=NEW_ACCOUNT)
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert default_client.get("/api/session").json() == {"authenticated": False}
    assert db.session(other_token) is None
    assert default_client.get(
        "/api/settings", headers={"Cookie": f"{COOKIE}={other_token}", "X-CSRF-Token": other_csrf}
    ).status_code == 401
    assert default_client.post("/api/login", json={"username": "admin", "password": "admin"}).status_code == 401
    login = default_client.post("/api/login", json={k: NEW_ACCOUNT[k] for k in ("username", "password")})
    assert login.status_code == 200 and login.json()["must_change_credentials"] is False
    default_client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    assert default_client.get("/api/settings").json()["host"] == "saved-esxi.invalid"
    assert db.settings()["password"] == "existing-esxi-secret"
    with db.connect() as connection:
        assert connection.execute("SELECT value FROM settings").fetchone()[0] == encrypted_before
        audit = str(connection.execute("SELECT action FROM audit").fetchall())
    assert NEW_ACCOUNT["password"] not in audit
    assert default_client.app.state.config.secret_key == default_config.secret_key
    assert default_client.post("/api/account/setup", json=NEW_ACCOUNT).status_code == 409


def test_new_credentials_survive_restart_with_original_bootstrap_config(default_config):
    with TestClient(create_app(default_config, start_worker=False)) as client:
        sign_in_default(client)
        assert client.post("/api/account/setup", json=NEW_ACCOUNT).status_code == 200
    with TestClient(create_app(default_config, start_worker=False)) as restarted:
        assert restarted.post("/api/login", json={"username": "admin", "password": "admin"}).status_code == 401
        response = restarted.post("/api/login", json={k: NEW_ACCOUNT[k] for k in ("username", "password")})
        assert response.status_code == 200
        assert response.json()["must_change_credentials"] is False
        assert restarted.get("/api/deployments").status_code == 200


def test_existing_custom_account_is_not_reset_or_forced_through_setup(signed_in):
    assert signed_in.get("/api/session").json()["must_change_credentials"] is False
    assert signed_in.post("/api/account/setup", json=NEW_ACCOUNT).status_code == 409
    assert signed_in.get("/api/deployments").status_code == 200
    assert signed_in.app.state.db.administrator()["username"] == "admin"
