"""A deployment chooses its own configured OS without changing other jobs."""

import copy

import pytest

from gdeploy.db import Database
from gdeploy.os_profiles import ALMA10, UBUNTU
from gdeploy.service import DeploymentError, DeploymentService
from test_corelight_sensor_deployments import sensor_job as sensor_job
from test_os_profiles import InventoryClient, choose, write_iso
from test_os_profiles import alma_inspection as alma_inspection


@pytest.fixture
def profile_job(config, spec, monkeypatch, alma_inspection):
    import gdeploy.service as module

    calls, hooks = [], {}

    class ESXi(InventoryClient):
        def find_owned_vms(self, owner):
            calls.append(("find-owned", owner))
            return [{"vm_id": "original-vm"}]

        def destroy_vm(self, vm_id, owner):
            calls.append(("destroy", vm_id))
            if "destroy" in hooks:
                hooks["destroy"]()

    db = Database(config.data_dir, config.secret_key)
    service = DeploymentService(db, config, ESXi)
    db.set_settings({"host": "esxi.lab", "username": "root", "password": "esxi-secret"})
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/xorriso")
    monkeypatch.setattr(module, "generate_ssh_key", lambda: ("automation-private", "automation-public"))
    snapshots = {}
    for profile, name, marker in ((UBUNTU, "ubuntu-server.iso", b"UBUNTU"), (ALMA10, "alma-dvd.iso", b"ALMA")):
        path = config.ubuntu_iso.parent / name
        snapshots[profile] = choose(service.media, path, write_iso(path, marker), profile)
    assert service.media.selected()["profile"] == ALMA10
    return service, db, spec, snapshots, calls, hooks


@pytest.mark.parametrize("profile", [UBUNTU, ALMA10])
def test_explicit_os_preflight_and_queue_use_matching_saved_media(profile_job, profile):
    service, db, spec, snapshots, _, _ = profile_job
    spec["os_profile"] = profile
    result = service.preflight(spec)
    assert result["ok"], result["checks"]
    assert next(check for check in result["checks"] if check["name"] == "OS ISO")["message"].startswith(snapshots[profile]["name"])
    job = service.enqueue(spec)
    assert job["spec"]["os_profile"] == profile
    assert db.get(job["id"], private=True)["secrets"]["os_media"] == snapshots[profile]
    # Making a job does not change the most recently configured Setup profile.
    assert service.media.selected()["profile"] == ALMA10


def test_legacy_request_without_explicit_os_keeps_last_selected_behavior(profile_job):
    service, db, spec, snapshots, _, _ = profile_job
    spec.pop("os_profile", None)
    job = service.enqueue(spec)
    assert not job["spec"].get("os_profile")
    assert db.get(job["id"], private=True)["secrets"]["os_media"] == snapshots[ALMA10]


@pytest.mark.parametrize("profile", [UBUNTU, ALMA10])
def test_requested_os_without_saved_media_has_actionable_preflight_and_never_falls_back(profile_job, monkeypatch, profile):
    import gdeploy.service as module

    service, db, spec, _, _, _ = profile_job
    service.media.clear_selection(profile)
    assert service.media.selected(profile) is None
    spec["os_profile"] = profile
    monkeypatch.setattr(module, "generate_ssh_key", lambda: pytest.fail("Unconfigured OS generated credentials"))
    result = service.preflight(spec)
    media_check = next(check for check in result["checks"] if check["name"] == "OS ISO")
    assert not result["ok"] and not media_check["ok"]
    assert "Select and verify an ISO for" in media_check["message"]
    assert media_check["action"]["href"] == "#settings/media"
    with pytest.raises(DeploymentError, match="Select and verify an ISO for"):
        service.enqueue(spec)
    assert db.list() == []


def test_invalid_frozen_media_shape_produces_preflight_error_without_dispatch(profile_job):
    service, _, spec, _, _, _ = profile_job
    spec["os_profile"] = ALMA10
    result = service.preflight(spec, os_media=["not-a-media-snapshot"])
    assert not result["ok"]
    assert not next(check for check in result["checks"] if check["name"] == "OS ISO")["ok"]


@pytest.mark.parametrize("profile", [UBUNTU, ALMA10])
@pytest.mark.parametrize("problem", ["missing-file", "changed-bytes", "wrong-snapshot"])
def test_requested_os_invalid_media_cannot_queue_or_generate_credentials(profile_job, monkeypatch, profile, problem):
    import gdeploy.service as module

    service, db, spec, snapshots, _, _ = profile_job
    spec["os_profile"] = profile
    if problem == "wrong-snapshot":
        other = ALMA10 if profile == UBUNTU else UBUNTU
        monkeypatch.setattr(service.media, "selected", lambda requested=None: snapshots[other])
    else:
        path = service.config.ubuntu_iso.parent / snapshots[profile]["name"]
        if problem == "missing-file":
            path.unlink()
        else:
            path.write_bytes(b"replacement installer bytes")
    monkeypatch.setattr(module, "generate_ssh_key", lambda: pytest.fail("Invalid requested OS generated credentials"))
    result = service.preflight(spec)
    assert not result["ok"]
    assert any(check["name"] in {"OS ISO", "OS selection"} and not check["ok"] for check in result["checks"])
    with pytest.raises(DeploymentError, match="Preflight failed"):
        service.enqueue(spec)
    assert db.list() == []


@pytest.mark.parametrize("profile", [UBUNTU, ALMA10])
def test_queued_os_dispatch_keeps_exact_snapshot_after_both_setup_profiles_change(profile_job, monkeypatch, profile):
    import gdeploy.service as module

    service, db, spec, snapshots, _, _ = profile_job
    spec["os_profile"] = profile
    job = service.enqueue(spec)
    original = copy.deepcopy(snapshots[profile])
    for changed_profile, name, marker in ((ALMA10, "alma-replacement.iso", b"ALMA"), (UBUNTU, "ubuntu-replacement.iso", b"UBUNTU")):
        path = service.config.ubuntu_iso.parent / name
        choose(service.media, path, write_iso(path, marker), changed_profile)
    seen = []

    def build(source, target, vm, *args, **kwargs):
        seen.append((str(source), vm["os_profile"]))
        raise DeploymentError("Stopped after checking the queued OS dispatch")

    monkeypatch.setattr(module, "build_seed_iso", build)
    db.claim()
    service.run(job["id"])
    assert seen == [(original["path"], profile)]
    assert db.get(job["id"], private=True)["secrets"]["os_media"] == original


def test_mismatched_queued_snapshot_stops_before_guest_or_media_creation(profile_job, monkeypatch):
    import gdeploy.service as module

    service, db, spec, snapshots, _, _ = profile_job
    spec["os_profile"] = ALMA10
    job = service.enqueue(spec)
    data = db.get(job["id"], private=True)["secrets"]
    data["os_media"] = snapshots[UBUNTU]
    db.update(job["id"], secrets=data)
    monkeypatch.setattr(module, "build_seed_iso", lambda *args, **kwargs: pytest.fail("A mismatched queued OS started installation"))
    db.claim()
    service.run(job["id"])
    failed = db.get(job["id"])
    assert failed["status"] == "failed" and "does not match" in failed["error"]


@pytest.mark.parametrize("profile", [UBUNTU, ALMA10])
def test_redeploy_selects_original_os_even_when_setup_last_selected_other_os(profile_job, profile):
    service, db, spec, snapshots, calls, _ = profile_job
    spec["os_profile"] = profile
    original = service.enqueue(spec)
    db.update(original["id"], status="failed")
    other = ALMA10 if profile == UBUNTU else UBUNTU
    service.media.select(snapshots[other]["id"], snapshots[other]["sha256"], other)
    replacement = service.redeploy(original["id"], original["name"])
    assert ("destroy", "original-vm") in calls
    assert replacement["spec"]["os_profile"] == profile
    assert db.get(replacement["id"], private=True)["secrets"]["os_media"] == snapshots[profile]
    assert service.media.selected()["profile"] == other


def test_redeploy_freezes_same_os_media_while_its_setup_selection_changes(profile_job):
    service, db, spec, snapshots, calls, hooks = profile_job
    spec["os_profile"] = UBUNTU
    original = service.enqueue(spec)
    db.update(original["id"], status="failed")
    replacement_path = service.config.ubuntu_iso.parent / "ubuntu-new-during-cleanup.iso"
    checksum = write_iso(replacement_path, b"UBUNTU")
    hooks["destroy"] = lambda: choose(service.media, replacement_path, checksum, UBUNTU)
    replacement = service.redeploy(original["id"], original["name"])
    assert ("destroy", "original-vm") in calls
    assert db.get(replacement["id"], private=True)["secrets"]["os_media"] == snapshots[UBUNTU]
    assert service.media.selected(UBUNTU)["name"] == replacement_path.name


def test_sensor_recovery_uses_original_os_and_reuses_early_token_after_setup_os_change(sensor_job, alma_inspection):
    service, db, spec, calls, _, _ = sensor_job
    spec["os_profile"] = UBUNTU
    original = service.enqueue(spec)
    original_secret = db.get(original["id"], private=True)["secrets"]
    db.update(original["id"], status="failed")
    alma_path = service.config.ubuntu_iso.parent / "alma-sensor.iso"
    choose(service.media, alma_path, write_iso(alma_path), ALMA10)
    calls.clear()
    replacement = service.redeploy(original["id"], original["name"])
    saved = db.get(replacement["id"], private=True)["secrets"]
    assert saved["os_media"] == original_secret["os_media"]
    assert saved["corelight_sensor"] == original_secret["corelight_sensor"]
    assert not db.sensor_pairing_token_used(saved["corelight_sensor"]["pairing_token"], deployment_id=replacement["id"])
    db.claim()
    service.run(replacement["id"])
    assert db.get(replacement["id"])["status"] == "completed"
    built = next(args[1] for args in calls if args[0] == "build-iso")
    assert built["os_profile"] == UBUNTU and built["management_mac"] != built["monitor_mac"]


def test_sensor_recovery_missing_original_iso_preserves_vm_and_pairing_claim(sensor_job, monkeypatch, alma_inspection):
    service, db, spec, calls, _, _ = sensor_job
    spec["os_profile"] = UBUNTU
    original = service.enqueue(spec)
    db.update(original["id"], status="failed")
    alma_path = service.config.ubuntu_iso.parent / "alma-sensor.iso"
    choose(service.media, alma_path, write_iso(alma_path), ALMA10)
    service.config.ubuntu_iso.unlink()
    before = db.get(original["id"], private=True)
    calls.clear()
    monkeypatch.setattr(db, "begin_sensor_redeployment", lambda *args, **kwargs: pytest.fail("Missing original media reserved a replacement token"))
    with pytest.raises(DeploymentError, match="restore verified Ubuntu"):
        service.redeploy(original["id"], original["name"], sensor_pairing_token="fresh-pairing-token")
    assert calls == [] and db.get(original["id"], private=True) == before
    assert db.sensor_pairing_token_used(before["secrets"]["corelight_sensor"]["pairing_token"])
