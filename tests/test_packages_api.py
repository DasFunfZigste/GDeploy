import hashlib
import io
import tarfile

import pytest
from fastapi.testclient import TestClient

from gdeploy.main import create_app


def package_body():
    output = io.BytesIO()
    header = bytearray(20)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = (62).to_bytes(2, "little")
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, body in (("splunk/bin/splunk", b"launcher"), ("splunk/bin/splunkd", bytes(header))):
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    return output.getvalue()


ENDPOINTS = [
    ("GET", "/api/settings/splunk-package"),
    ("PUT", "/api/settings/splunk-package"),
    ("DELETE", "/api/settings/splunk-package"),
    ("DELETE", "/api/settings/splunk-package/upload_" + "0" * 32),
    ("POST", "/api/settings/splunk-package/upload?filename=splunk.tgz&sha256=" + "0" * 64),
]


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_package_endpoints_require_authentication(client, method, path):
    response = client.request(method, path, json={"package_id": "arbitrary", "sha256": "0" * 64})
    assert response.status_code == 401


@pytest.mark.parametrize("method,path", [(method, path) for method, path in ENDPOINTS if method != "GET"])
def test_package_writes_require_csrf(signed_in, method, path):
    signed_in.headers.pop("X-CSRF-Token")
    response = signed_in.request(method, path, json={"package_id": "arbitrary", "sha256": "0" * 64})
    assert response.status_code == 403
    assert signed_in.app.state.db.splunk_package_settings() is None


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_package_endpoints_require_changed_initial_credentials(signed_in, method, path):
    with signed_in.app.state.db.connect() as connection:
        connection.execute("UPDATE administrator SET must_change_credentials=1")
    response = signed_in.request(method, path, json={"package_id": "arbitrary", "sha256": "0" * 64})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "credentials_change_required"


def test_server_picker_updates_readiness_without_touching_os_media(signed_in, config):
    body = package_body()
    path = config.splunk_package.with_name("splunk-10.0.0-build-linux-amd64.tgz")
    path.write_bytes(body)
    catalog = signed_in.get("/api/settings/splunk-package").json()
    assert not signed_in.get("/api/settings").json()["splunk_configured"]
    response = signed_in.put("/api/settings/splunk-package", json={
        "package_id": catalog["items"][0]["id"], "sha256": hashlib.sha256(body).hexdigest(),
    })
    assert response.status_code == 200
    assert response.json()["ready"]
    assert response.json()["selected"]["name"] == path.name
    assert signed_in.get("/api/settings").json()["splunk_configured"]
    assert signed_in.app.state.db.media_settings() is None
    assert not signed_in.get("/api/settings").json()["iso_configured"]


def test_uploaded_package_is_ready_after_restart_and_can_be_managed(signed_in, config):
    body = package_body()
    response = signed_in.post("/api/settings/splunk-package/upload", params={
        "filename": "splunk-10.0.0-build-linux-amd64.tgz", "sha256": hashlib.sha256(body).hexdigest(),
    }, content=body, headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 200
    original = response.json()
    package_id = original["selected"]["id"]
    assert original["ready"]
    assert not original["items"][0]["can_delete"]
    with TestClient(create_app(config, start_worker=False)) as restarted:
        login = restarted.post("/api/login", json={"username": "admin", "password": "test-admin-passphrase"})
        restarted.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert restarted.get("/api/settings/splunk-package").json() == original
        assert restarted.get("/api/settings").json()["splunk_configured"]
        assert restarted.delete(f"/api/settings/splunk-package/{package_id}").status_code == 409
        cleared = restarted.delete("/api/settings/splunk-package").json()
        assert not cleared["ready"]
        assert cleared["items"][0]["can_delete"]
        removed = restarted.delete(f"/api/settings/splunk-package/{package_id}")
        assert removed.status_code == 200
        assert removed.json()["items"] == []


def test_wrong_checksum_returns_actionable_error_without_saving_upload(signed_in, config):
    response = signed_in.post("/api/settings/splunk-package/upload", params={
        "filename": "splunk.tgz", "sha256": "0" * 64,
    }, content=package_body(), headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 400
    assert "publisher checksum" in response.json()["detail"]
    assert signed_in.app.state.db.splunk_package_settings() is None
    assert list((config.data_dir / "packages").iterdir()) == []


def test_picker_never_accepts_filesystem_path(signed_in):
    response = signed_in.put("/api/settings/splunk-package", json={"package_id": "/etc/passwd", "sha256": "0" * 64})
    assert response.status_code == 404
    assert signed_in.app.state.db.splunk_package_settings() is None


def test_delete_rejects_server_mounted_packages(signed_in, config):
    config.splunk_package.write_bytes(package_body())
    item = signed_in.get("/api/settings/splunk-package").json()["items"][0]
    assert not item["can_delete"]
    response = signed_in.delete(f"/api/settings/splunk-package/{item['id']}")
    assert response.status_code == 404
    assert config.splunk_package.exists()


def test_startup_removes_abandoned_partial_upload(signed_in, config):
    directory = config.data_dir / "packages"
    directory.mkdir()
    partial = directory / ("1" * 32 + ".partial")
    partial.write_bytes(b"interrupted upload")
    with TestClient(create_app(config, start_worker=False)):
        assert not partial.exists()
