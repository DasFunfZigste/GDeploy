import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gdeploy.db import Database, MediaStateError
from gdeploy.guest import GuestError
from gdeploy.main import create_app
from gdeploy.media import MediaError, MediaManager
from gdeploy.os_profiles import ALMA10, UBUNTU


def iso_body(profile):
    return b"\0" * (16 * 2048) + b"\x01CD001\x01" + profile.encode()


@pytest.fixture
def inspect_alma(monkeypatch):
    calls = []

    def inspect(path, **kwargs):
        calls.append(Path(path))
        if not Path(path).read_bytes().endswith(ALMA10.encode()):
            raise GuestError("Not an AlmaLinux full DVD.")
        return {"profile": ALMA10, "version": "10.0", "arch": "x86_64"}

    monkeypatch.setattr("gdeploy.alma_guest.validate_install_iso", inspect)
    return calls


def upload(client, profile, name=None):
    body = iso_body(profile)
    response = client.post("/api/settings/media/upload", params={
        "filename": name or profile + ".iso", "sha256": hashlib.sha256(body).hexdigest(), "profile": profile,
    }, content=body)
    assert response.status_code == 200, response.text
    return response.json()["selected"]


def test_each_profile_retains_verified_media_and_scoped_catalog_after_restart(signed_in, config, inspect_alma):
    ubuntu = upload(signed_in, UBUNTU)
    alma = upload(signed_in, ALMA10)
    manager = signed_in.app.state.media
    assert manager.selected() == alma
    assert manager.selected(UBUNTU) == ubuntu
    assert manager.selected(ALMA10) == alma
    assert manager.validate_snapshot(ubuntu).read_bytes() == iso_body(UBUNTU)
    assert manager.validate_snapshot(alma).read_bytes() == iso_body(ALMA10)
    for profile, selected in ((UBUNTU, ubuntu), (ALMA10, alma)):
        catalog = signed_in.get("/api/settings/media", params={"profile": profile}).json()
        assert catalog["selected"] == selected
        assert catalog["profile"] == profile
        assert catalog["ready"] and catalog["has_saved_selection"]
        assert all(item["ready"] and item["has_saved_selection"] for item in catalog["profile_selections"].values())
    with TestClient(create_app(config, start_worker=False)) as restarted:
        restarted.post("/api/login", json={"username": "admin", "password": "test-admin-passphrase"})
        for profile, selected in ((UBUNTU, ubuntu), (ALMA10, alma)):
            assert restarted.get("/api/settings/media", params={"profile": profile}).json()["selected"] == selected
        assert restarted.get("/api/settings/media").json()["selected"] == alma


def test_public_profile_metadata_has_readiness_without_local_paths(signed_in, config, inspect_alma):
    ubuntu = upload(signed_in, UBUNTU)
    alma = upload(signed_in, ALMA10)
    settings = signed_in.get("/api/settings").json()
    profiles = settings["os_media_profiles"]
    assert profiles == signed_in.get("/api/settings/media").json()["profile_selections"]
    assert profiles[UBUNTU]["selected"]["id"] == ubuntu["id"]
    assert profiles[ALMA10]["selected"]["id"] == alma["id"]
    assert str(config.data_dir) not in json.dumps(profiles)
    assert all("path" not in value["selected"] for value in profiles.values())
    Path(ubuntu["path"]).unlink()
    profiles = signed_in.get("/api/settings").json()["os_media_profiles"]
    assert not profiles[UBUNTU]["ready"] and profiles[UBUNTU]["has_saved_selection"]
    assert profiles[ALMA10]["ready"]


def test_unsaved_files_are_not_automatically_adopted_for_a_requested_profile(signed_in, config):
    config.ubuntu_iso.write_bytes(iso_body(UBUNTU))
    config.ubuntu_iso.with_name("AlmaLinux-10-x86_64-dvd.iso").write_bytes(iso_body(ALMA10))
    manager = signed_in.app.state.media
    assert manager.selected(ALMA10) is None
    catalog = signed_in.get("/api/settings/media", params={"profile": ALMA10}).json()
    assert catalog["selected"] is None
    assert catalog["profile"] == ALMA10 and not catalog["ready"] and not catalog["has_saved_selection"]
    assert catalog["profile_selections"][ALMA10] == {"selected": None, "ready": False, "has_saved_selection": False}


def test_clear_profile_keeps_other_default_and_all_files(signed_in, inspect_alma):
    ubuntu = upload(signed_in, UBUNTU)
    alma = upload(signed_in, ALMA10)
    response = signed_in.delete("/api/settings/media", params={"profile": UBUNTU})
    assert response.status_code == 200
    result = response.json()
    assert result["profile"] == UBUNTU and result["selected"] is None
    assert not result["ready"] and not result["has_saved_selection"]
    assert result["profile_selections"][ALMA10]["ready"]
    assert signed_in.app.state.media.selected() == alma
    assert Path(ubuntu["path"]).exists() and Path(alma["path"]).exists()
    assert signed_in.delete("/api/settings/media/" + ubuntu["id"]).status_code == 200
    assert signed_in.delete("/api/settings/media/" + alma["id"]).status_code == 409
    assert signed_in.delete("/api/settings/media", params={"profile": UBUNTU}).status_code == 200
    assert signed_in.app.state.media.selected(ALMA10) == alma


def test_no_profile_clear_preserves_legacy_current_behavior_without_clearing_other_os(signed_in, inspect_alma):
    ubuntu = upload(signed_in, UBUNTU)
    alma = upload(signed_in, ALMA10)
    response = signed_in.delete("/api/settings/media")
    assert response.status_code == 200
    assert response.json()["selected"] is None
    assert not response.json()["has_saved_selection"]
    assert signed_in.app.state.media.selected(ALMA10) is None
    assert signed_in.app.state.media.selected(UBUNTU) == ubuntu
    assert Path(alma["path"]).exists()


def test_all_saved_os_defaults_protected_from_deletion_including_legacy_current_mismatch(signed_in, inspect_alma):
    ubuntu = upload(signed_in, UBUNTU)
    alma = upload(signed_in, ALMA10)
    manager = signed_in.app.state.media
    storage = manager.storage()
    assert all(item["selected"] and not item["can_delete"] for item in storage["items"])
    for selected in (ubuntu, alma):
        with pytest.raises(MediaStateError, match="saved selection"):
            manager.delete(selected["id"])
        assert Path(selected["path"]).exists()
    replacement = upload(signed_in, UBUNTU, "replacement-ubuntu.iso")
    assert manager.selected(UBUNTU) == replacement
    assert signed_in.delete("/api/settings/media/" + ubuntu["id"]).status_code == 200
    assert signed_in.delete("/api/settings/media/" + alma["id"]).status_code == 409


def test_clear_profile_preserves_active_job_media_and_its_deletion_protection(signed_in, spec, inspect_alma):
    ubuntu = upload(signed_in, UBUNTU)
    alma = upload(signed_in, ALMA10)
    db = signed_in.app.state.db
    db.create("ubuntu-waiting", spec, {"os_media": ubuntu})
    signed_in.delete("/api/settings/media", params={"profile": UBUNTU})
    assert db.get("ubuntu-waiting", private=True)["secrets"]["os_media"] == ubuntu
    assert signed_in.app.state.media.selected(ALMA10) == alma
    response = signed_in.delete("/api/settings/media/" + ubuntu["id"])
    assert response.status_code == 409 and "deployment" in response.json()["detail"]


def test_legacy_worker_singleton_changes_cannot_remove_profile_file_deletion_protection(signed_in, inspect_alma):
    original = upload(signed_in, UBUNTU)
    other = upload(signed_in, ALMA10)
    db = signed_in.app.state.db
    legacy_current = dict(other, profile=UBUNTU)
    with db.connect() as connection:
        connection.execute("UPDATE media_settings SET value=? WHERE id=1", (db.seal(legacy_current),))
    rows = signed_in.app.state.media.storage()["items"]
    assert all(row["selected"] and not row["can_delete"] for row in rows)
    assert signed_in.delete("/api/settings/media/" + original["id"]).status_code == 409
    assert Path(original["path"]).exists()


@pytest.mark.parametrize("restart_before_switch", [False, True])
def test_legacy_newer_pointer_is_preserved_when_switching_profiles(signed_in, config, inspect_alma, restart_before_switch):
    old = upload(signed_in, UBUNTU, "ubuntu-old.iso")
    newer = upload(signed_in, UBUNTU, "ubuntu-new.iso")
    db = signed_in.app.state.db
    # A v0.16.0 worker updates only the approved singleton, leaving the new
    # profile row at the earlier value. That row must not resurrect on switch.
    with db.connect() as connection:
        connection.execute("UPDATE media_profile_settings SET value=? WHERE profile=?", (db.seal(old), UBUNTU))
    manager = signed_in.app.state.media
    if restart_before_switch:
        manager = MediaManager(Database(config.data_dir, config.secret_key), config)
    alma_path = config.ubuntu_iso.with_name("alma.iso")
    alma_path.write_bytes(iso_body(ALMA10))
    checksum = hashlib.sha256(iso_body(ALMA10)).hexdigest()
    manager.select(manager._server_id(alma_path), checksum, ALMA10)
    assert manager.selected(UBUNTU) == newer
    assert manager.db.media_profile_settings()[UBUNTU] == newer
    with pytest.raises(MediaStateError, match="saved selection"):
        manager.delete(newer["id"])
    assert Path(newer["path"]).exists()
    manager.delete(old["id"])
    assert not Path(old["path"]).exists()


def test_bad_checksum_or_wrong_profile_does_not_replace_either_saved_default(signed_in, inspect_alma):
    ubuntu = upload(signed_in, UBUNTU)
    alma = upload(signed_in, ALMA10)
    before = signed_in.app.state.db.media_profile_settings()
    wrong_hash = signed_in.put("/api/settings/media", json={
        "media_id": ubuntu["id"], "sha256": "0" * 64, "profile": ALMA10,
    })
    assert wrong_hash.status_code == 400 and "SHA-256" in wrong_hash.json()["detail"]
    wrong_profile = signed_in.put("/api/settings/media", json={
        "media_id": ubuntu["id"], "sha256": ubuntu["sha256"], "profile": ALMA10,
    })
    assert wrong_profile.status_code == 400 and "AlmaLinux" in wrong_profile.json()["detail"]
    assert signed_in.app.state.db.media_profile_settings() == before
    assert signed_in.app.state.media.selected() == alma


def test_legacy_singleton_is_migrated_to_encrypted_profile_state_without_losing_snapshot(signed_in, config):
    selected = upload(signed_in, UBUNTU)
    db = signed_in.app.state.db
    old = {key: value for key, value in selected.items() if key != "profile"}
    with db.connect() as connection:
        connection.execute("DROP TABLE media_profile_settings")
        connection.execute("UPDATE media_settings SET value=? WHERE id=1", (db.seal(old),))
    reopened = Database(config.data_dir, config.secret_key)
    assert reopened.media_settings(UBUNTU) == selected
    assert reopened.media_settings() == selected
    assert reopened.media_settings(ALMA10) is None
    with reopened.connect() as connection:
        row = connection.execute("SELECT profile,value FROM media_profile_settings").fetchone()
        assert row["profile"] == UBUNTU
        assert selected["path"] not in row["value"] and selected["name"] not in row["value"]
    manager = MediaManager(reopened, config)
    assert manager.selected(UBUNTU) == selected
    assert manager.validate_snapshot(selected).exists()


def test_environment_fallback_is_available_only_for_its_matching_os(config):
    config.ubuntu_iso.write_bytes(iso_body(UBUNTU))
    checksum = hashlib.sha256(iso_body(UBUNTU)).hexdigest()
    manager = MediaManager(Database(config.data_dir, config.secret_key), replace(config, ubuntu_sha256=checksum))
    assert manager.selected(UBUNTU) == manager.legacy()
    assert manager.selected(ALMA10) is None
    assert manager.catalog(ALMA10)["selected"] is None
    assert manager.profile_selections()[UBUNTU]["ready"]
    assert not manager.profile_selections()[UBUNTU]["has_saved_selection"]


def test_reselecting_another_profiles_server_file_preserves_its_verified_profile(signed_in, config, inspect_alma):
    manager = signed_in.app.state.media
    alma_path = config.ubuntu_iso.with_name("alma.iso")
    alma_path.write_bytes(iso_body(ALMA10))
    checksum = hashlib.sha256(iso_body(ALMA10)).hexdigest()
    alma = manager.select(manager._server_id(alma_path), checksum, ALMA10)["selected"]
    ubuntu = upload(signed_in, UBUNTU)
    result = manager.select(alma["id"], checksum)
    assert result["selected"]["profile"] == ALMA10
    assert manager.selected(UBUNTU) == ubuntu


@pytest.mark.parametrize("method", ["get", "delete"])
def test_scoped_media_endpoint_rejects_unknown_profiles_before_mutation(signed_in, method):
    selected = upload(signed_in, UBUNTU)
    response = getattr(signed_in, method)("/api/settings/media", params={"profile": "unsafe-profile"})
    assert response.status_code == 422
    assert signed_in.app.state.media.selected() == selected
    with pytest.raises(MediaError, match="OS installer"):
        signed_in.app.state.media.selected("unsafe-profile")


def test_scoped_media_clear_requires_csrf_and_account_setup(signed_in):
    selected = upload(signed_in, UBUNTU)
    csrf = signed_in.headers.pop("X-CSRF-Token")
    assert signed_in.delete("/api/settings/media", params={"profile": UBUNTU}).status_code == 403
    signed_in.headers["X-CSRF-Token"] = csrf
    with signed_in.app.state.db.connect() as connection:
        connection.execute("UPDATE administrator SET must_change_credentials=1")
    assert signed_in.delete("/api/settings/media", params={"profile": UBUNTU}).status_code == 403
    assert signed_in.app.state.media.selected(UBUNTU) == selected
