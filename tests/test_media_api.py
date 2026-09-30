import hashlib

import pytest
from fastapi.testclient import TestClient

from gdeploy.main import create_app


def iso_body():
    return b"\0" * (16 * 2048) + b"\x01CD001\x01" + b"test installer"


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/settings/media"),
    ("PUT", "/api/settings/media"),
    ("POST", "/api/settings/media/upload?filename=installer.iso&sha256=" + "0" * 64),
])
def test_media_requires_authentication(client, method, path):
    response = client.request(method, path, json={"media_id": "arbitrary", "sha256": "0" * 64})
    assert response.status_code == 401


@pytest.mark.parametrize("method,path", [
    ("PUT", "/api/settings/media"),
    ("POST", "/api/settings/media/upload?filename=installer.iso&sha256=" + "0" * 64),
])
def test_media_mutations_require_csrf(signed_in, method, path):
    signed_in.headers.pop("X-CSRF-Token")
    response = signed_in.request(method, path, json={"media_id": "arbitrary", "sha256": "0" * 64})
    assert response.status_code == 403
    assert signed_in.app.state.db.media_settings() is None


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/settings/media"),
    ("PUT", "/api/settings/media"),
    ("POST", "/api/settings/media/upload?filename=installer.iso&sha256=" + "0" * 64),
])
def test_media_requires_completed_initial_credentials(signed_in, method, path):
    with signed_in.app.state.db.connect() as connection:
        connection.execute("UPDATE administrator SET must_change_credentials=1")
    response = signed_in.request(method, path, json={"media_id": "arbitrary", "sha256": "0" * 64})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "credentials_change_required"


def test_server_picker_selects_media_and_settings_report_readiness(signed_in, config):
    config.ubuntu_iso.write_bytes(iso_body())
    checksum = hashlib.sha256(iso_body()).hexdigest()
    catalog = signed_in.get("/api/settings/media").json()
    assert not signed_in.get("/api/settings").json()["iso_configured"]
    response = signed_in.put("/api/settings/media", json={"media_id": catalog["items"][0]["id"], "sha256": checksum})
    assert response.status_code == 200
    assert response.json()["ready"]
    assert signed_in.get("/api/settings").json()["iso_configured"]
    assert signed_in.app.state.db.settings() is None


def test_raw_upload_is_checked_selected_and_available_after_restart(signed_in, config):
    body = iso_body()
    response = signed_in.post(
        "/api/settings/media/upload", params={"filename": "New installer.iso", "sha256": hashlib.sha256(body).hexdigest()},
        content=body, headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200
    assert response.json()["ready"]
    assert response.json()["selected"]["name"] == "New installer.iso"
    with TestClient(create_app(config, start_worker=False)) as restarted:
        restarted.post("/api/login", json={"username": "admin", "password": "test-admin-passphrase"})
        assert restarted.get("/api/settings/media").json() == response.json()
        assert restarted.get("/api/settings").json()["iso_configured"]


def test_wrong_upload_checksum_has_clear_error_and_no_persisted_file(signed_in, config):
    response = signed_in.post(
        "/api/settings/media/upload", params={"filename": "installer.iso", "sha256": "0" * 64},
        content=iso_body(), headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 400
    assert "does not match" in response.json()["detail"]
    assert signed_in.app.state.db.media_settings() is None
    assert list((config.data_dir / "media").iterdir()) == []


def test_picker_rejects_filesystem_paths(signed_in):
    response = signed_in.put("/api/settings/media", json={"media_id": "/etc/passwd", "sha256": "0" * 64})
    assert response.status_code in (404, 422)
    assert signed_in.app.state.db.media_settings() is None
