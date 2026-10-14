"""Exercise emitted RPM installers without running packages or live requests."""

import gzip
import hashlib
import io
import os
import time
import urllib.error
import uuid
from types import SimpleNamespace

import pytest

from gdeploy import alma_corelight, rpm_repository
from gdeploy.fleet_guest import RepositoryDownloadError
from gdeploy.fleet_repository import FleetRepositoryError
from gdeploy.db import Database
from gdeploy.os_profiles import ALMA10, UBUNTU
from gdeploy.service import DeploymentError, DeploymentService


@pytest.fixture(params=["fleet", "sensor"])
def runtime(request, tmp_path, monkeypatch):
    role = request.param
    factory = alma_corelight.fleet_installer_script if role == "fleet" else alma_corelight.sensor_installer_script
    script = factory()
    marker = "GDEPLOY_FLEET_PY" if role == "fleet" else "GDEPLOY_SENSOR_PY"
    source = script.split(f"python3 - <<'{marker}'\n", 1)[1].rsplit(f"\n{marker}", 1)[0]
    namespace = {"__name__": "emitted_alma_test"}
    exec(compile(source, "alma-corelight-guest", "exec"), namespace)
    namespace["ROOT"] = tmp_path
    namespace["test_role"] = role
    namespace["SECRETS"] = set()
    monkeypatch.setattr(namespace["platform"], "machine", lambda: "x86_64")
    monkeypatch.setattr(namespace["os"], "cpu_count", lambda: 4)
    (tmp_path / "etc").mkdir()
    (tmp_path / "proc").mkdir()
    (tmp_path / "etc/os-release").write_text('ID="almalinux"\nVERSION_ID="10.0"\n')
    (tmp_path / "proc/meminfo").write_text("MemTotal:       16777216 kB\n")
    monkeypatch.setattr(namespace["shutil"], "disk_usage", lambda path: SimpleNamespace(free=600 * 1000**3))
    namespace["run"] = lambda command, *args, **kwargs: "rw,relatime" if command[0] == "findmnt" else "  x86-64-v3 (supported, searched)\n"
    return namespace


def test_emitted_installers_use_alma_host_guard_and_original_health(runtime):
    role = runtime["test_role"]
    assert runtime["host_checks"].__name__ == f"_alma_{role}_host_checks"
    runtime["host_checks"]()
    assert runtime["verify_health"].__name__ == "verify_health"
    if role == "sensor":
        assert runtime["MANAGEMENT"] == "gdeploymgmt" and runtime["MONITORING"] == "gdeploymon"
        assert runtime["disable_competing_firewalls"].__name__ == "disable_competing_firewalls"
        assert runtime["sensor_configuration"]({
            "community_string": "community", "license_key": "license", "server_sslname": "fleet.test",
            "pairing_token": "pairing", "fleet_url": "https://fleet.test:1443", "api_network": "192.168.1.0/24",
        }).count("gdeploymon") == 1


@pytest.mark.parametrize("failure", ["alma9", "ubuntu", "arm", "cpu", "memory", "space", "missing-os"])
def test_undersized_or_wrong_guest_stops_before_packages(runtime, monkeypatch, failure):
    root = runtime["ROOT"]
    if failure == "alma9":
        (root / "etc/os-release").write_text('ID=almalinux\nVERSION_ID="9.6"\n')
    elif failure == "ubuntu":
        (root / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
    elif failure == "arm":
        monkeypatch.setattr(runtime["platform"], "machine", lambda: "aarch64")
    elif failure == "cpu":
        monkeypatch.setattr(runtime["os"], "cpu_count", lambda: 1)
    elif failure == "memory":
        (root / "proc/meminfo").write_text("MemTotal:       4194304 kB\n")
    elif failure == "space":
        monkeypatch.setattr(runtime["shutil"], "disk_usage", lambda path: SimpleNamespace(free=10 * 1024**3))
    else:
        (root / "etc/os-release").unlink()
    with pytest.raises(runtime["InstallerError"]):
        runtime["host_checks"]()


@pytest.mark.parametrize("version", ["29.2.2-1.x86_64", "0:29.2.2-1.x86_64", "1:29.2.2^snapshot-9.el9.noarch"])
def test_exact_rpm_versions_are_canonicalized_and_apt_choice_is_not_used(version):
    assert alma_corelight.requested_rpm_version({"online_version_rpm": version, "online_version": "29.9.9-1"}) == (version if ":" in version else "0:" + version)


@pytest.mark.parametrize("version", ["29.2.2-1", "corelight-fleet-29.2.2-1.x86_64", "29.2.2-1.aarch64", "29.*-1.x86_64", "29.2.2-1.x86_64;id", "--allowerasing", 123, "9" * 201])
def test_rpm_selection_rejects_apt_versions_other_architectures_and_options(version):
    with pytest.raises(alma_corelight.FleetInstallError, match="exact Fleet Manager RPM"):
        alma_corelight.requested_rpm_version({"online_version_rpm": version})


@pytest.fixture
def repository_runtime(runtime):
    calls = []
    role = runtime["test_role"]
    runtime["download_repository_file"] = lambda token, path, **kwargs: b"repository-signing-key"
    runtime["_download_public_signing_key"] = lambda: b"public-package-signing-key"
    state = {"available": "0:29.2.2-1.x86_64\n", "installed": "0:29.2.2-1.x86_64\n", "calls": calls}

    def run(command, label, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "gpg":
            return b"binary-signing-key"
        if command[:2] == ["rpm", "-q"]:
            return state["installed"]
        if command[:3] == ["dnf", "-q", "repoquery"]:
            return state["available"]
        return ""

    runtime["run"] = run
    state.update(runtime=runtime, role=role)
    return state


def test_documented_repository_paths_have_protected_auth_signed_metadata_and_no_el10_guess(repository_runtime):
    state, runtime = repository_runtime, repository_runtime["runtime"]
    token = "private-token-plus+slash/"
    runtime["_alma_repository"]({"repository_token": token}, state["role"])
    path = runtime["ROOT"] / f"etc/yum.repos.d/corelight_{state['role']}-stable.repo"
    text = path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert "username=" + token in text and "password=\n" in text
    assert "repo_gpgcheck=1\ngpgcheck=0\n" in text and "sslverify=1" in text
    assert "rpm_any/rpm_any/$basearch" in text
    assert ("el/9/$basearch" in text) == (state["role"] == "sensor")
    assert "el/10" not in text and "@pkgrepos" not in text
    assert all(token not in repr(command) for command, options in state["calls"])
    assert token not in runtime["sanitize"]("download failed: " + token)
    assert not any("setenforce" in command or "selinux" in " ".join(command) for command, options in state["calls"])


def test_fleet_exact_rpm_checked_before_and_after_install(repository_runtime):
    runtime = repository_runtime["runtime"]
    if repository_runtime["role"] != "fleet":
        return
    assert runtime["configure_online_repository"]({"repository_token": "private-token", "online_version_rpm": "29.2.2-1.x86_64"}) == "0:29.2.2-1.x86_64"
    assert any(command[-1] == "corelight-fleet-0:29.2.2-1.x86_64" for command, options in repository_runtime["calls"])
    repository_runtime["installed"] = "0:29.2.3-1.x86_64\n"
    with pytest.raises(runtime["InstallerError"], match="does not match"):
        runtime["configure_online_repository"]({"repository_token": "private-token", "online_version_rpm": "29.2.2-1.x86_64"})


def test_absent_selected_fleet_rpm_never_installs_latest(repository_runtime):
    runtime = repository_runtime["runtime"]
    if repository_runtime["role"] != "fleet":
        return
    repository_runtime["available"] = "0:29.2.3-1.x86_64\n"
    with pytest.raises(runtime["InstallerError"], match="unavailable"):
        runtime["configure_online_repository"]({"repository_token": "private-token", "online_version_rpm": "29.2.2-1.x86_64"})
    assert not any(any(item.startswith("corelight-fleet") for item in command) and "install" in command for command, options in repository_runtime["calls"])


def test_sensor_rpm_keeps_corelightctl_and_sensor_as_native_packages(repository_runtime):
    runtime = repository_runtime["runtime"]
    if repository_runtime["role"] != "sensor":
        return
    assert runtime["configure_repository"]({"repository_token": "private-token"}) == "0:29.2.2-1.x86_64"
    assert ["dnf", "-q", "-y", "install", "--", "corelightctl", "corelight-sensor"] in [command for command, options in repository_runtime["calls"]]


@pytest.mark.parametrize("active", ["active", "inactive"])
def test_fleet_firewall_uses_management_zone_and_keeps_ssh_when_enabling(runtime, active):
    if runtime["test_role"] != "fleet":
        return
    calls = []
    runtime["ALMA_MANAGEMENT_IP"] = "192.168.1.10"

    def run(command, *args, **kwargs):
        calls.append(command)
        if command[:2] == ["ip", "-j"]:
            return '[{"ifname":"ens192","addr_info":[{"local":"192.168.1.10"}]}]'
        if command[0] == "systemctl":
            return active
        if "--get-default-zone" in command:
            return "public"
        if "--get-active-zones" in command:
            return "trusted\n  interfaces: ens192\n"
        return "yes" if any(item.startswith("--query-") for item in command) else "success"

    runtime["run_command"] = run
    runtime["configure_firewall"]()
    assert ["systemctl", "enable", "--now", "firewalld"] in calls
    assert (["firewall-offline-cmd", "--add-service=ssh"] in calls) == (active == "inactive")
    if active == "inactive":
        assert calls.index(["firewall-offline-cmd", "--add-service=ssh"]) < calls.index(["systemctl", "enable", "--now", "firewalld"])
    assert ["firewall-cmd", "--zone=trusted", "--add-service=ssh"] in calls
    for port in (443, 1443):
        assert ["firewall-cmd", "--zone=trusted", "--permanent", f"--add-port={port}/tcp"] in calls
        assert ["firewall-cmd", "--zone=trusted", f"--query-port={port}/tcp"] in calls


@pytest.mark.parametrize("old_unit", [None, "old-unit"])
def test_fleet_rpm_service_start_mask_is_restored_after_install_failure(runtime, old_unit):
    if runtime["test_role"] != "fleet":
        return
    path = runtime["ROOT"] / "etc/systemd/system/corelight-fleetd.service"
    path.parent.mkdir(parents=True)
    if old_unit is not None:
        path.write_text(old_unit)
    runtime["run"] = lambda *args, **kwargs: ""
    with pytest.raises(RuntimeError, match="package failure"):
        with runtime["suppress_package_start"]():
            assert path.is_symlink() and os.readlink(path) == "/dev/null"
            raise RuntimeError("package failure")
    assert path.read_text() == old_unit if old_unit is not None else not path.exists()
    assert not list(path.parent.glob(".gdeploy-fleet-service-*"))


def test_fleet_rpm_unit_written_in_etc_is_not_deleted_after_install(runtime):
    if runtime["test_role"] != "fleet":
        return
    path = runtime["ROOT"] / "etc/systemd/system/corelight-fleetd.service"
    runtime["run"] = lambda *args, **kwargs: ""
    with runtime["suppress_package_start"]():
        path.unlink()
        path.write_text("vendor RPM service unit\n")
    assert path.read_text() == "vendor RPM service unit\n"


def test_original_input_integrity_guards_remain_before_rpm_guest_commands(runtime):
    commands = []
    runtime["run"] = lambda command, *args, **kwargs: commands.append(command)
    if runtime["test_role"] == "fleet":
        payload = {"fleetmanager": {"mode": "online", "license_pem": "changed", "license_sha256": "f" * 64}}
        with pytest.raises(runtime["InstallerError"], match="license changed during transfer"):
            runtime["install"](payload)
    else:
        with pytest.raises(runtime["InstallerError"], match="repository token"):
            runtime["install"]({"corelight_sensor": {"repository_token": ""}})
    assert commands == []


def test_sensor_keeps_network_checks_prepare_pairing_and_health_sequence(runtime):
    if runtime["test_role"] != "sensor":
        return
    snapshot = {
        "repository_token": "private-token", "community_string": "community", "license_key": "private-license",
        "pairing_token": "private-pairing", "fleet_url": "https://fleet.example.test:1443/pair",
        "server_sslname": "fleet.example.test", "management_mac": "00:50:56:01:02:03",
        "monitor_mac": "00:50:56:01:02:04", "api_network": "192.168.1.0/24",
    }
    steps = []
    runtime["host_checks"] = lambda: steps.append("host")
    runtime["verify_network"] = lambda snapshot, ip: steps.append("network")
    runtime["configure_repository"] = lambda snapshot: steps.append("rpm") or "0:29.2.1-1.x86_64"
    runtime["disable_competing_firewalls"] = lambda: steps.append("firewall")
    runtime["run"] = lambda command, *args, **kwargs: steps.append(" ".join(command)) or ""
    runtime["verify_health"] = lambda: steps.append("health")
    result = runtime["install"]({"corelight_sensor": snapshot, "ip": "192.168.1.10"})
    assert steps == ["host", "network", "rpm", "firewall", "corelightctl sensor prepare", "corelightctl init", "corelightctl sensor deploy -v", "health", "network"]
    assert result == {"healthy": True, "version": "0:29.2.1-1.x86_64"}
    configuration = runtime["ROOT"] / "etc/corelight/corelightctl.yaml"
    assert configuration.stat().st_mode & 0o777 == 0o600
    assert "private-pairing" in configuration.read_text() and "gdeploymon" in configuration.read_text()


class Response(io.BytesIO):
    status = 200


def test_public_signing_key_redirect_uses_no_repository_credentials(runtime, monkeypatch):
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise urllib.error.HTTPError(request.full_url, 302, "redirect", {"Location": "https://download.example.test/key.asc?signature=private"}, io.BytesIO())
            return Response(b"public-signing-key")

    monkeypatch.setattr(runtime["urllib"].request, "build_opener", lambda *args: Opener())
    assert runtime["_download_public_signing_key"]() == b"public-signing-key"
    assert len(requests) == 2 and all(not request.headers for request in requests)


@pytest.mark.parametrize("location", ["http://download.test/key", "https://secret@download.test/key", "https://download.test/key\nprivate", ""])
def test_public_signing_key_rejects_unsafe_redirect_without_echoing_it(runtime, monkeypatch, location):
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 302, "redirect", {"Location": location}, io.BytesIO())

    monkeypatch.setattr(runtime["urllib"].request, "build_opener", lambda *args: Opener())
    with pytest.raises(runtime["InstallerError"]):
        runtime["_download_public_signing_key"]()


def package(version="29.2.2", release="1", epoch="0", architecture="x86_64", name="corelight-fleet"):
    return f'<package type="rpm"><name>{name}</name><arch>{architecture}</arch><version epoch="{epoch}" ver="{version}" rel="{release}"/></package>'


def metadata(*packages):
    return ('<metadata xmlns="http://linux.duke.edu/metadata/common">' + "".join(packages) + "</metadata>").encode()


def index(compressed, path="repodata/abc-primary.xml.gz", *, checksum=None):
    checksum = checksum or hashlib.sha256(compressed).hexdigest()
    return f'<repomd xmlns="http://linux.duke.edu/metadata/repo"><data type="primary"><checksum type="sha256">{checksum}</checksum><location href="{path}"/><size>{len(compressed)}</size></data></repomd>'.encode()


@pytest.fixture(autouse=True)
def no_catalog_network(monkeypatch):
    monkeypatch.setattr(rpm_repository, "download_repository_file", lambda *args, **kwargs: pytest.fail("No live package catalog requests permitted"))


def test_rpm_catalog_follows_repomd_with_checksum_and_native_epoch_version_release_arch(monkeypatch):
    compressed = gzip.compress(metadata(package(), package(release="10"), package(epoch="1"), package(architecture="aarch64"), package(name="unrelated")))
    calls = []

    def download(token, path, **kwargs):
        calls.append((token, path, kwargs))
        return index(compressed) if len(calls) == 1 else compressed

    monkeypatch.setattr(rpm_repository, "download_repository_file", download)
    assert rpm_repository.available_versions("synthetic-token") == ["1:29.2.2-1.x86_64", "0:29.2.2-10.x86_64", "0:29.2.2-1.x86_64"]
    assert [row[1] for row in calls] == ["rpm_any/rpm_any/x86_64/repodata/repomd.xml", "rpm_any/rpm_any/x86_64/repodata/abc-primary.xml.gz"]
    assert all(row[2]["resource"] == "package metadata" and 0 < row[2]["timeout"] <= 60 for row in calls)


@pytest.mark.parametrize("path", ["../secret", "repodata/../../key.xml.gz", "https://unrelated.test/primary.xml.gz", "repodata/primary.xml.gz?token=secret", "/repodata/primary.xml.gz"])
def test_remote_catalog_location_cannot_escape_fixed_repository(monkeypatch, path):
    calls = []

    def download(token, requested, **kwargs):
        calls.append(requested)
        return index(b"compressed", path=path)

    monkeypatch.setattr(rpm_repository, "download_repository_file", download)
    with pytest.raises(FleetRepositoryError, match="malformed index"):
        rpm_repository.available_versions("synthetic-token")
    assert len(calls) == 1


def test_metadata_checksum_failure_stops_before_xml_parsing(monkeypatch):
    calls = []

    def download(token, path, **kwargs):
        calls.append(path)
        return index(b"original") if len(calls) == 1 else b"tampered-private-body"

    monkeypatch.setattr(rpm_repository, "download_repository_file", download)
    with pytest.raises(FleetRepositoryError, match="checksum") as error:
        rpm_repository.available_versions("synthetic-token")
    assert "private" not in str(error.value)


@pytest.mark.parametrize("body", [b"<html>error</html>", b"<!DOCTYPE metadata [<!ENTITY secret 'private'>]><metadata/>", metadata(package(version="--bad")), metadata(package(release="bad;id")), metadata(package(architecture="arm64"))])
def test_rpm_catalog_rejects_malformed_or_incompatible_metadata(body):
    with pytest.raises(FleetRepositoryError):
        rpm_repository._parse_versions(body, time.monotonic() + 60)


def test_rpm_order_numeric_tilde_and_caret():
    compare = rpm_repository.compare_rpm_versions
    assert compare("0:29.2.2-10.x86_64", "0:29.2.2-9.x86_64") > 0
    assert compare("0:29.2.2~rc1-1.x86_64", "0:29.2.2-1.x86_64") < 0
    assert compare("0:29.2.2^snapshot-1.x86_64", "0:29.2.2-1.x86_64") > 0
    assert compare("0:29.2.2^snapshot-1.x86_64", "0:29.2.2.1-1.x86_64") < 0
    assert compare("1:1.0-1.x86_64", "0:99.0-1.x86_64") > 0


def test_rpm_catalog_auth_errors_are_safe_and_do_not_fallback(monkeypatch):
    paths = []

    def download(token, path, **kwargs):
        paths.append(path)
        raise RepositoryDownloadError("Safe authentication error", status_code=401)

    monkeypatch.setattr(rpm_repository, "download_repository_file", download)
    with pytest.raises(FleetRepositoryError, match="Safe authentication error"):
        rpm_repository.available_versions("synthetic-token")
    assert paths == [rpm_repository.INDEX_PATH]


@pytest.fixture
def redeploy_media_job(config, spec, monkeypatch):
    """Keep real media hashing/state; stub only VMware and unrelated preflight."""
    db = Database(config.data_dir, config.secret_key)
    calls, hooks = [], {}

    class Cleanup:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def find_owned_vms(self, deployment_id):
            calls.append(("find-owned", deployment_id))
            return [{"vm_id": "original-vm"}]

        def destroy_vm(self, vm_id, deployment_id):
            calls.append(("destroy", vm_id))
            if "destroy" in hooks:
                hooks["destroy"]()

    service = DeploymentService(db, config, Cleanup)

    def media(profile, name):
        path = config.ubuntu_iso.parent / name
        body = bytearray(40 * 2048)
        body[16 * 2048:16 * 2048 + 7] = b"\x01CD001\x01"
        path.write_bytes(body)
        return service.media._snapshot(path, sha256=hashlib.sha256(body).hexdigest(), profile=profile)

    original_media = media(UBUNTU, "original.iso")
    current = {"media": original_media}
    monkeypatch.setattr(service.media, "selected", lambda profile=None: current["media"])
    monkeypatch.setattr(service, "preflight", lambda *args, **kwargs: {"ok": True, "checks": []})
    return service, db, spec, original_media, current, media, calls, hooks


@pytest.mark.parametrize("role", ["ubuntu", "corelight_sensor"])
@pytest.mark.parametrize("profile_source", ["spec", "snapshot"])
def test_redeploy_changed_os_rejected_before_deletion_or_sensor_reservation(redeploy_media_job, monkeypatch, role, profile_source):
    service, db, spec, _, _, media, calls, _ = redeploy_media_job
    spec["vms"][0]["role"] = role
    original_media = media(ALMA10, "alma-original.iso")
    if profile_source == "spec":
        spec["os_profile"] = ALMA10
    else:
        spec["os_profile"] = None
    identifier = str(uuid.uuid4())
    db.create(identifier, spec, {"esxi": {"host": "esxi.example.test"}, "os_media": original_media})
    db.update(identifier, status="failed")
    before, events = db.get(identifier, private=True), db.events(identifier)
    monkeypatch.setattr(db, "begin_sensor_redeployment", lambda *args, **kwargs: pytest.fail("Mismatched OS reserved a sensor replacement"))
    monkeypatch.setattr(service.corelight_sensor, "snapshot", lambda *args, **kwargs: pytest.fail("Mismatched OS consumed sensor input"))
    with pytest.raises(DeploymentError, match="restore verified AlmaLinux 10 OS media in Setup"):
        service.redeploy(identifier, spec["name"], sensor_pairing_token="replacement-token" if role == "corelight_sensor" else "")
    assert calls == []
    assert db.get(identifier, private=True) == before and db.events(identifier) == events
    assert len(db.list()) == 1


@pytest.mark.parametrize("problem", ["no-media", "missing-file", "changed-bytes", "bad-checksum"])
def test_redeploy_unverified_media_leaves_original_job_and_vms_intact(redeploy_media_job, problem):
    service, db, spec, original_media, current, _, calls, _ = redeploy_media_job
    identifier = str(uuid.uuid4())
    db.create(identifier, spec, {"esxi": {"host": "esxi.example.test"}, "os_media": original_media})
    db.update(identifier, status="failed")
    before, events = db.get(identifier, private=True), db.events(identifier)
    if problem == "no-media":
        current["media"] = None
    elif problem == "missing-file":
        config_path = service.config.ubuntu_iso.parent / "original.iso"
        config_path.unlink()
    elif problem == "changed-bytes":
        config_path = service.config.ubuntu_iso.parent / "original.iso"
        config_path.write_bytes(b"invalid installer")
    else:
        current["media"] = {**original_media, "sha256": "0" * 64}
    with pytest.raises(DeploymentError, match="Cannot delete and redeploy: restore verified Ubuntu"):
        service.redeploy(identifier, spec["name"])
    assert calls == []
    assert db.get(identifier, private=True) == before and db.events(identifier) == events


@pytest.mark.parametrize("legacy", [False, True])
def test_redeploy_freezes_verified_media_before_setup_changes_during_cleanup(redeploy_media_job, legacy):
    service, db, spec, original_media, current, media, calls, hooks = redeploy_media_job
    if not legacy:
        spec["os_profile"] = UBUNTU
    identifier = str(uuid.uuid4())
    secrets = {"esxi": {"host": "esxi.example.test"}}
    if not legacy:
        secrets["os_media"] = original_media
    db.create(identifier, spec, secrets)
    db.update(identifier, status="failed")
    alternate = media(ALMA10, "new-alma.iso")
    hooks["destroy"] = lambda: current.update(media=alternate)
    replacement = service.redeploy(identifier, spec["name"])
    assert ("destroy", "original-vm") in calls
    assert db.get(identifier)["status"] == "reverted"
    assert replacement["spec"]["os_profile"] == UBUNTU and replacement["parent_id"] == identifier
    assert db.get(replacement["id"], private=True)["secrets"]["os_media"] == original_media
    assert service.media.selected()["profile"] == ALMA10
