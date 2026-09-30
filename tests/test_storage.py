import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from gdeploy.db import Database, MediaStateError
from gdeploy.media import MediaError, MediaManager
from gdeploy.service import DeploymentService


BODY = b"\0" * (16 * 2048) + b"\x01CD001\x01" + b"installer"
CHECKSUM = hashlib.sha256(BODY).hexdigest()


def upload(client, name="installer.iso"):
    response = client.post(
        "/api/settings/media/upload", params={"filename": name, "sha256": CHECKSUM}, content=BODY,
    )
    assert response.status_code == 200, response.text
    return response.json()["selected"]


def registered(manager, token, *, source="upload"):
    manager.upload_dir.mkdir(exist_ok=True)
    path = manager.upload_dir / (token * 32 + ".iso")
    path.write_bytes(BODY)
    value = manager._snapshot(
        path, name=token + ".iso", source=source, sha256=CHECKSUM, media_id=source + "_" + token * 32
    )
    if source == "esxi":
        value["origin"] = {"host": "esxi.test", "datastore": "iso datastore", "path": "folder/original.iso"}
    manager.db.set_media_settings(value, uploaded=True)
    return value


@pytest.fixture
def manager(config):
    return MediaManager(Database(config.data_dir, config.secret_key), config)


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/settings/storage"),
    ("DELETE", "/api/settings/media"),
    ("DELETE", "/api/settings/media/upload_" + "1" * 32),
])
def test_storage_requires_authentication(client, method, path):
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/settings/storage"),
    ("DELETE", "/api/settings/media"),
    ("DELETE", "/api/settings/media/upload_" + "1" * 32),
])
def test_storage_requires_initial_credential_change(signed_in, method, path):
    with signed_in.app.state.db.connect() as connection:
        connection.execute("UPDATE administrator SET must_change_credentials=1")
    response = signed_in.request(method, path)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "credentials_change_required"


@pytest.mark.parametrize("clear_selection", [False, True])
def test_storage_deletion_requires_csrf(signed_in, clear_selection):
    first = upload(signed_in)
    upload(signed_in, "second.iso")
    signed_in.headers.pop("X-CSRF-Token")
    response = signed_in.delete("/api/settings/media" + ("" if clear_selection else "/" + first["id"]))
    assert response.status_code == 403
    assert Path(first["path"]).read_bytes() == BODY


def test_storage_reports_data_filesystem_and_real_managed_workspace_sizes(signed_in, config, monkeypatch):
    first = upload(signed_in)
    second = registered(signed_in.app.state.media, "a", source="esxi")
    artifacts = config.data_dir / "artifacts" / "job"
    artifacts.mkdir(parents=True)
    (artifacts / "installer.iso").write_bytes(b"work" * 30)
    (artifacts / "seed").mkdir()
    (artifacts / "seed" / "grub.cfg").write_bytes(b"grub")
    (config.data_dir / "media" / ("b" * 32 + ".partial")).write_bytes(b"partial")
    secret = config.data_dir / "not-counted-as-media"
    secret.write_bytes(b"private" * 100)
    (artifacts / "linked").symlink_to(secret)
    (artifacts / "directory-link").symlink_to(config.data_dir, target_is_directory=True)
    queried = []

    def usage(path):
        queried.append(path)
        return SimpleNamespace(total=1000, used=500, free=400)

    monkeypatch.setattr("gdeploy.media.shutil.disk_usage", usage)
    response = signed_in.get("/api/settings/storage")
    assert response.status_code == 200
    result = response.json()
    assert queried == [config.data_dir]
    assert result["data_filesystem"] == {
        "label": "GDeploy data filesystem (container-visible)", "path": str(config.data_dir),
        "total_bytes": 1000, "used_bytes": 500, "free_bytes": 400,
    }
    assert result["managed_iso_bytes"] == len(BODY) * 2
    assert result["workspace_bytes"] == 120 + 4 + 7
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id[first["id"]]["can_delete"]
    assert not by_id[first["id"]]["selected"]
    assert by_id[second["id"]]["origin"] == second["origin"]
    assert by_id[second["id"]]["selected"]
    assert not by_id[second["id"]]["can_delete"]
    assert "path" not in by_id[first["id"]]
    assert "not-counted-as-media" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_storage_filesystem_failure_is_actionable(signed_in, monkeypatch):
    def failed(path):
        raise PermissionError("private underlying path")

    monkeypatch.setattr("gdeploy.media.shutil.disk_usage", failed)
    response = signed_in.get("/api/settings/storage")
    assert response.status_code == 507
    assert "mount and permissions" in response.json()["detail"]
    assert "private underlying path" not in response.text


@pytest.mark.parametrize("source", ["upload", "esxi"])
def test_delete_removes_only_registered_local_copy_and_updates_totals(manager, config, source):
    first = registered(manager, "1", source=source)
    second = registered(manager, "2")
    config.ubuntu_iso.write_bytes(BODY)
    original = config.ubuntu_iso.read_bytes()
    unregistered = manager.upload_dir / ("3" * 32 + ".iso")
    unregistered.write_bytes(BODY)
    result = manager.delete(first["id"])
    assert not Path(first["path"]).exists()
    assert Path(second["path"]).read_bytes() == BODY
    assert unregistered.read_bytes() == BODY
    assert config.ubuntu_iso.read_bytes() == original
    assert result["managed_iso_bytes"] == len(BODY)
    assert [item["id"] for item in result["items"]] == [second["id"]]
    reopened = MediaManager(Database(config.data_dir, config.secret_key), config)
    assert reopened.db.media_files() == [second]
    assert reopened.selected() == second
    with manager.db.connect() as connection:
        actions = [row[0] for row in connection.execute("SELECT action FROM audit")]
    assert any("Managed OS ISO deleted: 1.iso" == action for action in actions)


def test_api_delete_returns_updated_storage_and_selected_delete_has_reason(signed_in):
    first = upload(signed_in)
    response = signed_in.delete("/api/settings/media/" + first["id"])
    assert response.status_code == 409
    assert "saved selection" in response.json()["detail"]
    second = upload(signed_in, "second.iso")
    response = signed_in.delete("/api/settings/media/" + first["id"])
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [second["id"]]
    assert signed_in.delete("/api/settings/media/" + first["id"]).status_code == 404


@pytest.mark.parametrize("status", ["queued", "running", "cleaning"])
def test_active_deployment_media_cannot_be_deleted(manager, spec, status):
    first = registered(manager, "1")
    registered(manager, "2")
    manager.db.create("job", spec, {"os_media": first})
    manager.db.update("job", status=status)
    item = next(item for item in manager.storage()["items"] if item["id"] == first["id"])
    assert not item["can_delete"]
    assert "deployment" in item["delete_reason"]
    with pytest.raises(MediaStateError, match="deployment"):
        manager.delete(first["id"])
    assert Path(first["path"]).read_bytes() == BODY


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted", "cleanup_failed", "reverted"])
def test_terminal_deployment_history_does_not_pin_old_media(manager, spec, status):
    first = registered(manager, "1")
    registered(manager, "2")
    manager.db.create("job", spec, {"os_media": first})
    manager.db.update("job", status=status)
    manager.delete(first["id"])
    assert manager.db.get("job", private=True)["secrets"]["os_media"] == first
    assert not Path(first["path"]).exists()


def test_server_mounts_unknown_ids_and_unregistered_files_cannot_be_deleted(manager, config):
    config.ubuntu_iso.write_bytes(BODY)
    registered(manager, "1")
    unregistered = manager.upload_dir / ("2" * 32 + ".iso")
    unregistered.write_bytes(BODY)
    for media_id in (manager._server_id(config.ubuntu_iso), "upload_" + "2" * 32, "../ubuntu.iso", str(config.ubuntu_iso)):
        with pytest.raises(MediaError) as error:
            manager.delete(media_id)
        assert error.value.status_code == 404
    assert unregistered.read_bytes() == BODY
    assert config.ubuntu_iso.read_bytes() == BODY


@pytest.mark.parametrize("unsafe", ["file-symlink", "directory-symlink", "directory", "outside-path"])
def test_unsafe_media_is_never_followed_or_deleted(manager, tmp_path, unsafe):
    first = registered(manager, "1")
    registered(manager, "2")
    external = tmp_path / "outside"
    external.mkdir()
    target = external / Path(first["path"]).name
    target.write_bytes(BODY)
    path = Path(first["path"])
    if unsafe == "file-symlink":
        path.unlink()
        path.symlink_to(target)
    elif unsafe == "directory-symlink":
        manager.upload_dir.rename(manager.upload_dir.with_name("saved-media"))
        manager.upload_dir.symlink_to(external, target_is_directory=True)
    elif unsafe == "directory":
        path.unlink()
        path.mkdir()
    else:
        first["path"] = str(target)
        with manager.db.connect() as connection:
            connection.execute("UPDATE media_files SET value=? WHERE id=?", (manager.db.seal(first), first["id"]))
    item = next(item for item in manager.storage()["items"] if item["id"] == first["id"])
    assert not item["can_delete"]
    assert not item["available"]
    with pytest.raises(MediaError):
        manager.delete(first["id"])
    assert target.read_bytes() == BODY
    assert any(item["id"] == first["id"] for item in manager.db.media_files())


def test_missing_unused_iso_can_remove_stale_registration(manager):
    first = registered(manager, "1")
    registered(manager, "2")
    Path(first["path"]).unlink()
    item = next(item for item in manager.storage()["items"] if item["id"] == first["id"])
    assert item["can_delete"] and not item["available"] and item["size_bytes"] == 0
    manager.delete(first["id"])
    assert all(item["id"] != first["id"] for item in manager.db.media_files())


def test_failed_unlink_preserves_registry_and_selection(manager, monkeypatch):
    first = registered(manager, "1")
    second = registered(manager, "2")

    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("gdeploy.media.os.unlink", denied)
    with pytest.raises(MediaError) as error:
        manager.delete(first["id"])
    assert error.value.status_code == 507
    assert Path(first["path"]).read_bytes() == BODY
    assert manager.selected() == second
    assert len(manager.db.media_files()) == 2


def test_directory_replaced_by_link_during_delete_cannot_redirect_unlink(manager, tmp_path, monkeypatch):
    first = registered(manager, "1")
    registered(manager, "2")
    external = tmp_path / "outside"
    external.mkdir()
    target = external / Path(first["path"]).name
    target.write_bytes(BODY)
    original_open = os.open

    def swapped(path, flags, *args, **kwargs):
        if Path(path) == manager.upload_dir:
            manager.upload_dir.rename(manager.upload_dir.with_name("saved-media"))
            manager.upload_dir.symlink_to(external, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("gdeploy.media.os.open", swapped)
    with pytest.raises(MediaError):
        manager.delete(first["id"])
    assert target.read_bytes() == BODY
    assert len(manager.db.media_files()) == 2


def test_deletion_between_verification_and_selection_cannot_resurrect_media(manager, config, monkeypatch):
    first = registered(manager, "1")
    second = registered(manager, "2")
    other = MediaManager(Database(config.data_dir, config.secret_key), config)
    verified, resume = threading.Event(), threading.Event()
    original = other.validate_snapshot

    def wait_after_verification(value):
        result = original(value)
        verified.set()
        assert resume.wait(5)
        return result

    monkeypatch.setattr(other, "validate_snapshot", wait_after_verification)
    with ThreadPoolExecutor(max_workers=1) as pool:
        selecting = pool.submit(other.select, first["id"], CHECKSUM)
        try:
            assert verified.wait(5)
            manager.delete(first["id"])
        finally:
            resume.set()
        with pytest.raises(MediaStateError, match="removed or changed"):
            selecting.result(timeout=5)
    assert manager.selected() == second
    assert not Path(first["path"]).exists()


def test_deletion_during_enqueue_preflight_cannot_queue_missing_media(manager, config, spec, monkeypatch):
    first = registered(manager, "1")
    service = DeploymentService(Database(config.data_dir, config.secret_key), config)
    entered, resume = threading.Event(), threading.Event()

    def preflight(*args, **kwargs):
        assert kwargs["os_media"]["id"] == first["id"]
        entered.set()
        assert resume.wait(5)
        return {"ok": True, "checks": []}

    monkeypatch.setattr(service, "preflight", preflight)
    monkeypatch.setattr("gdeploy.service.generate_ssh_key", lambda: ("private", "public"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        enqueue = pool.submit(service.enqueue, spec, {"host": "esxi.test"})
        try:
            assert entered.wait(5)
            registered(manager, "2")
            manager.delete(first["id"])
        finally:
            resume.set()
        with pytest.raises(MediaStateError, match="removed or changed"):
            enqueue.result(timeout=5)
    assert manager.db.list() == []


def test_selection_transaction_serializes_with_deletion_across_database_instances(manager, config, monkeypatch):
    first = registered(manager, "1")
    registered(manager, "2")
    other = MediaManager(Database(config.data_dir, config.secret_key), config)
    entered, resume = threading.Event(), threading.Event()
    original = other.db._require_registered_media

    def hold_transaction(connection, value):
        original(connection, value)
        entered.set()
        assert resume.wait(5)

    monkeypatch.setattr(other.db, "_require_registered_media", hold_transaction)
    with ThreadPoolExecutor(max_workers=2) as pool:
        selecting = pool.submit(other.select, first["id"], CHECKSUM)
        try:
            assert entered.wait(5)
            deleting = pool.submit(manager.delete, first["id"])
        finally:
            resume.set()
        assert selecting.result(timeout=5)["selected"]["id"] == first["id"]
        with pytest.raises(MediaStateError, match="saved selection"):
            deleting.result(timeout=5)
    assert Path(first["path"]).read_bytes() == BODY


def test_active_media_protection_is_independent_of_history_limit(manager, spec):
    first = registered(manager, "1")
    registered(manager, "2")
    manager.db.create("old-active", spec, {"os_media": first})
    with manager.db.connect() as connection:
        original = connection.execute("SELECT * FROM deployments WHERE id='old-active'").fetchone()
        for number in range(501):
            newer = dict(original) | {"id": str(number), "status": "completed", "created_at": "9999"}
            connection.execute(
                "INSERT INTO deployments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", tuple(newer.values())
            )
    assert all(item["id"] != "old-active" for item in manager.db.list())
    with pytest.raises(MediaStateError, match="deployment"):
        manager.delete(first["id"])


def test_server_alias_of_managed_file_has_same_deletion_protection(manager, spec):
    first = registered(manager, "1")
    second = registered(manager, "2")
    alias = dict(first, source="server", id=manager._server_id(Path(first["path"])))
    manager.db.set_media_settings(alias)
    assert next(item for item in manager.storage()["items"] if item["id"] == first["id"])["selected"]
    with pytest.raises(MediaStateError, match="saved selection"):
        manager.delete(first["id"])
    manager.db.set_media_settings(second)
    manager.db.create("alias-job", spec, {"os_media": alias})
    with pytest.raises(MediaStateError, match="deployment"):
        manager.delete(first["id"])
    manager.db.update("alias-job", status="completed")
    manager.delete(first["id"])
    with pytest.raises(MediaStateError, match="removed or changed"):
        manager.db.create("stale-alias-job", spec, {"os_media": alias})


def test_environment_media_fallback_has_same_selection_protection(manager, config):
    first = registered(manager, "1")
    with manager.db.connect() as connection:
        connection.execute("DELETE FROM media_settings")
    fallback = MediaManager(manager.db, replace(config, ubuntu_iso=Path(first["path"]), ubuntu_sha256=CHECKSUM))
    item = fallback.storage()["items"][0]
    assert item["selected"] and not item["can_delete"]
    with pytest.raises(MediaStateError, match="saved selection"):
        fallback.delete(first["id"])
    assert Path(first["path"]).read_bytes() == BODY


def test_clear_saved_selection_allows_deleting_the_only_uploaded_iso(signed_in):
    first = upload(signed_in)
    assert signed_in.get("/api/settings/media").json()["has_saved_selection"]
    response = signed_in.delete("/api/settings/media")
    assert response.status_code == 200
    result = response.json()
    assert result["selected"] is None
    assert not result["ready"] and not result["has_saved_selection"]
    assert result["items"][0]["id"] == first["id"]
    assert Path(first["path"]).read_bytes() == BODY
    storage = signed_in.get("/api/settings/storage").json()
    assert storage["items"][0]["can_delete"]
    response = signed_in.delete("/api/settings/media/" + first["id"])
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["managed_iso_bytes"] == 0


def test_clear_selection_preserves_active_job_snapshot_and_deletion_protection(signed_in, spec):
    first = upload(signed_in)
    db = signed_in.app.state.db
    db.create("waiting-job", spec, {"os_media": first})
    assert signed_in.delete("/api/settings/media").status_code == 200
    assert db.get("waiting-job", private=True)["secrets"]["os_media"] == first
    storage = signed_in.get("/api/settings/storage").json()
    assert not storage["items"][0]["selected"]
    assert not storage["items"][0]["can_delete"]
    response = signed_in.delete("/api/settings/media/" + first["id"])
    assert response.status_code == 409
    assert "deployment" in response.json()["detail"]
    assert Path(first["path"]).read_bytes() == BODY


def test_clear_selection_restores_environment_media_fallback(manager, config):
    first = registered(manager, "1")
    config.ubuntu_iso.write_bytes(BODY)
    configured = MediaManager(manager.db, replace(config, ubuntu_sha256=CHECKSUM))
    result = configured.clear_selection()
    assert not result["has_saved_selection"]
    assert result["ready"]
    assert result["selected"]["path"] == str(config.ubuntu_iso)
    assert result["selected"]["source"] == "server"
    assert Path(first["path"]).read_bytes() == BODY
    assert configured.storage()["items"][0]["can_delete"]
    configured.delete(first["id"])
    assert config.ubuntu_iso.read_bytes() == BODY
