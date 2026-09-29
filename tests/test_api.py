import uuid


def test_login_required_and_health_public(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/deployments").status_code == 401
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/session").json() == {"authenticated": False}


def test_session_and_csrf_logout(signed_in):
    assert signed_in.get("/api/session").json()["authenticated"]
    token = signed_in.headers.pop("X-CSRF-Token")
    assert signed_in.post("/api/logout").status_code == 403
    signed_in.headers["X-CSRF-Token"] = token
    assert signed_in.post("/api/logout").status_code == 200
    assert signed_in.get("/api/deployments").status_code == 401


def test_rate_limit_and_origin(client):
    payload = {"username": "admin", "password": "incorrect"}
    assert client.post("/api/login", json=payload, headers={"Origin": "https://evil.invalid"}).status_code == 403
    for _ in range(5):
        assert client.post("/api/login", json=payload).status_code == 401
    assert client.post("/api/login", json=payload).status_code == 429


def test_non_ascii_login_fails_cleanly(client):
    assert client.post("/api/login", json={"username": "é", "password": "incorrect"}).status_code == 401


def test_secure_cookie_and_headers(client):
    response = client.post("/api/login", json={"username": "admin", "password": "test-admin-passphrase"})
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_settings_password_encrypted_and_never_returned(signed_in):
    payload = {"host": "esxi.example.test", "username": "root", "password": "esxi-very-secret", "verify_tls": True}
    assert signed_in.put("/api/settings", json=payload).status_code == 200
    assert "esxi-very-secret" not in signed_in.get("/api/settings").text
    db = signed_in.app.state.db
    with db.connect() as c:
        raw = c.execute("SELECT value FROM settings").fetchone()[0]
    assert "esxi-very-secret" not in raw
    assert db.settings()["password"] == "esxi-very-secret"
    payload["password"] = ""
    assert signed_in.put("/api/settings", json=payload).status_code == 200
    payload["host"] = "different.example.test"
    assert signed_in.put("/api/settings", json=payload).status_code == 400


def test_validation_does_not_echo_secret_input(signed_in):
    response = signed_in.put(
        "/api/settings", json={"host": "bad/url", "username": "root", "password": "never-echo-this"}
    )
    assert response.status_code == 422
    assert "never-echo-this" not in response.text


def test_esxi_password_preserves_whitespace(signed_in):
    assert (
        signed_in.put(
            "/api/settings", json={"host": "esxi.local", "username": "root", "password": " spaces matter "}
        ).status_code
        == 200
    )
    assert signed_in.app.state.db.settings()["password"] == " spaces matter "


def test_credentials_are_explicit_audited_and_excluded_from_history(signed_in, spec):
    db = signed_in.app.state.db
    deployment_id = str(uuid.uuid4())
    secrets = {
        "vm_credentials": {
            "lab-elasticsearch": {
                "username": "gdeploy",
                "password": "unique-guest-secret",
                "private_key": "PRIVATE-KEY",
                "services": [],
            }
        }
    }
    db.create(deployment_id, spec, secrets)
    for path in ("/api/deployments", f"/api/deployments/{deployment_id}"):
        response = signed_in.get(path)
        assert "unique-guest-secret" not in response.text
        assert "PRIVATE-KEY" not in response.text
    response = signed_in.post(f"/api/deployments/{deployment_id}/credentials")
    assert response.json()["vms"][0]["password"] == "unique-guest-secret"
    assert "PRIVATE-KEY" not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert db.events(deployment_id)[-1]["level"] == "audit"


def test_creation_rechecks_preflight(signed_in, spec, monkeypatch):
    service = signed_in.app.state.service
    calls = []
    monkeypatch.setattr(
        service,
        "preflight",
        lambda *args, **kwargs: (
            calls.append(args)
            or {"ok": False, "checks": [{"name": "Capacity", "ok": False, "message": "Insufficient RAM"}]}
        ),
    )
    response = signed_in.post("/api/deployments", json=spec)
    assert response.status_code == 400
    assert "Insufficient RAM" in response.text and len(calls) == 1
    assert service.db.list() == []


def test_unknown_deployment_is_404(signed_in):
    assert signed_in.get(f"/api/deployments/{uuid.uuid4()}").status_code == 404


def test_corrupt_and_missing_config_fail_closed(config):
    from dataclasses import replace
    import pytest

    with pytest.raises(ValueError):
        replace(config, secret_key="not-a-key")
    with pytest.raises(ValueError):
        replace(config, admin_password_hash="plaintext")
