import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from gdeploy.main import create_app
from gdeploy.vmware import VMwareError


BODY = b"\0" * (16 * 2048) + b"\x01CD001\x01" + b"existing datastore installer"
CHECKSUM = hashlib.sha256(BODY).hexdigest()
HOST = "esxi.example.test"
PAYLOAD = {"host": HOST, "datastore": "ISO datastore", "path": "OS images/Server installer.iso", "sha256": CHECKSUM}


class DatastoreClient:
    def __init__(self):
        self.downloads = []
        self.body = BODY

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def inventory(self):
        return {"datastores": [{"name": "ISO datastore", "free_gb": 100, "capacity_gb": 200}]}

    def browse_iso_media(self, datastore, folder=""):
        assert datastore == "ISO datastore"
        if folder == "":
            folders, files, parent = [{"name": "OS images", "path": "OS images"}], [], None
        elif folder == "OS images":
            folders, files, parent = [], [{"name": "Server installer.iso", "path": PAYLOAD["path"], "size_bytes": len(self.body)}], ""
        else:
            raise VMwareError("The selected datastore folder is unavailable.")
        return {"datastore": datastore, "folder": folder, "parent": parent, "folders": folders, "files": files}

    def download_iso(self, datastore, path, destination, *, max_bytes, expected_size):
        assert (datastore, path) == (PAYLOAD["datastore"], PAYLOAD["path"])
        assert expected_size == len(self.body) <= max_bytes
        self.downloads.append(path)
        with destination.open("xb") as output:
            output.write(self.body)
        return len(self.body)


@pytest.fixture
def esxi(signed_in, monkeypatch):
    saved = {"host": HOST, "username": "root", "password": "esxi-secret-password"}
    signed_in.app.state.db.set_settings(saved)
    fake = DatastoreClient()
    factory = MagicMock(return_value=fake)
    monkeypatch.setattr(signed_in.app.state.service, "client_factory", factory)
    fake.factory = factory
    return fake


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_esxi_media_requires_sign_in(client, method):
    assert client.request(method, "/api/settings/media/esxi", json=PAYLOAD).status_code == 401


def test_esxi_import_requires_csrf(signed_in, esxi):
    signed_in.headers.pop("X-CSRF-Token")
    assert signed_in.post("/api/settings/media/esxi", json=PAYLOAD).status_code == 403
    esxi.factory.assert_not_called()


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_esxi_media_requires_initial_credential_replacement(signed_in, esxi, method):
    with signed_in.app.state.db.connect() as connection:
        connection.execute("UPDATE administrator SET must_change_credentials=1")
    response = signed_in.request(method, "/api/settings/media/esxi", json=PAYLOAD)
    assert response.status_code == 403
    esxi.factory.assert_not_called()


def test_saved_host_required_and_local_catalog_remains_independent(signed_in, monkeypatch):
    factory = MagicMock(side_effect=AssertionError("No ESXi calls without a saved host"))
    monkeypatch.setattr(signed_in.app.state.service, "client_factory", factory)
    assert signed_in.get("/api/settings/media").status_code == 200
    for method in ("GET", "POST"):
        response = signed_in.request(method, "/api/settings/media/esxi", json=PAYLOAD)
        assert response.status_code == 400
        assert "Save the ESXi connection" in response.json()["detail"]
    factory.assert_not_called()


def test_browser_uses_saved_connection_and_approved_certificate(signed_in, esxi):
    signed_in.app.state.db.trust_esxi_certificate(f"https://{HOST}:443", "approved-certificate", "fingerprint")
    root = signed_in.get("/api/settings/media/esxi").json()
    assert root["host"] == HOST
    assert root["folder"] == "" and root["parent"] is None
    assert root["folders"][0]["path"] == "OS images"
    nested = signed_in.get("/api/settings/media/esxi", params={"datastore": "ISO datastore", "folder": "OS images"})
    assert nested.status_code == 200
    assert nested.json()["files"][0]["path"] == PAYLOAD["path"]
    assert "esxi-secret-password" not in nested.text
    assert esxi.factory.call_args.kwargs["verify_tls"] is True
    assert esxi.factory.call_args.kwargs["trusted_certificate"] == "approved-certificate"
    assert esxi.downloads == []


def test_empty_host_datastores_produce_empty_browser(signed_in, esxi, monkeypatch):
    monkeypatch.setattr(esxi, "inventory", lambda: {"datastores": []})
    response = signed_in.get("/api/settings/media/esxi")
    assert response.status_code == 200
    assert response.json()["datastore"] == ""
    assert response.json()["files"] == []


def test_host_changed_after_browsing_is_rejected_before_contact(signed_in, esxi):
    response = signed_in.post("/api/settings/media/esxi", json={**PAYLOAD, "host": "old-esxi.example.test"})
    assert response.status_code == 409
    assert "host changed" in response.json()["detail"]
    esxi.factory.assert_not_called()


def test_import_copies_and_verifies_then_survives_restart_and_reselection(signed_in, config, esxi):
    response = signed_in.post("/api/settings/media/esxi", json=PAYLOAD)
    assert response.status_code == 200, response.text
    result = response.json()
    selected = result["selected"]
    assert result["ready"] and selected["source"] == "esxi"
    assert selected["origin"] == {key: PAYLOAD[key] for key in ("host", "datastore", "path")}
    assert selected["path"] != PAYLOAD["path"]
    assert Path(selected["path"]).read_bytes() == BODY
    assert esxi.body == BODY and esxi.downloads == [PAYLOAD["path"]]
    assert signed_in.get("/api/settings").json()["iso_configured"]
    with TestClient(create_app(config, start_worker=False)) as restarted:
        session = restarted.post("/api/login", json={"username": "admin", "password": "test-admin-passphrase"}).json()
        restarted.headers["X-CSRF-Token"] = session["csrf_token"]
        catalog = restarted.get("/api/settings/media").json()
        assert catalog == result
        # Reselect the cached copy without any contact with ESXi.
        chosen = restarted.put("/api/settings/media", json={"media_id": selected["id"], "sha256": CHECKSUM})
        assert chosen.status_code == 200
        assert chosen.json()["selected"]["origin"] == selected["origin"]
        snapshot = restarted.app.state.media.selected()
        assert restarted.app.state.service.media.validate_snapshot(snapshot).read_bytes() == BODY


@pytest.mark.parametrize("failure", ["checksum", "format", "missing", "oversized", "download", "storage"])
def test_failed_import_preserves_previous_selection_and_files(signed_in, config, esxi, monkeypatch, failure):
    original = signed_in.post("/api/settings/media/esxi", json=PAYLOAD).json()["selected"]
    existing_files = set((config.data_dir / "media").iterdir())
    payload = dict(PAYLOAD)
    if failure == "checksum":
        payload["sha256"] = "0" * 64
    elif failure == "format":
        esxi.body = b"This is not an ISO"
        payload["sha256"] = hashlib.sha256(esxi.body).hexdigest()
    elif failure == "missing":
        payload["path"] = "OS images/deleted.iso"
    elif failure == "oversized":
        from gdeploy import media
        monkeypatch.setattr(media, "MAX_UPLOAD_BYTES", 10)
    elif failure == "download":
        monkeypatch.setattr(esxi, "download_iso", MagicMock(side_effect=VMwareError("Download failed: TLS verification failed")))
    elif failure == "storage":
        monkeypatch.setattr(signed_in.app.state.media, "_prepare_upload", MagicMock(side_effect=OSError("disk failure")))
    response = signed_in.post("/api/settings/media/esxi", json=payload)
    assert response.status_code in (400, 404, 413, 507)
    assert "esxi-secret-password" not in response.text
    assert signed_in.app.state.media.selected() == original
    assert set((config.data_dir / "media").iterdir()) == existing_files
    assert Path(original["path"]).read_bytes() == BODY


def test_browse_permissions_error_is_reported(signed_in, esxi, monkeypatch):
    monkeypatch.setattr(esxi, "browse_iso_media", MagicMock(side_effect=VMwareError("Datastore browse permission denied")))
    response = signed_in.get("/api/settings/media/esxi")
    assert response.status_code == 400
    assert "permission denied" in response.json()["detail"]
