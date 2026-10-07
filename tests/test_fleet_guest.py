"""Exercise the real guest installer without invoking an installer or network."""

import base64
import hashlib
import io
import json
import os
import re
import ssl
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from gdeploy import fleet_guest as fleet
from gdeploy import guest


@pytest.fixture(scope="module")
def license_pem():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-fleet-identity.invalid")])
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1)).sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode() + key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def payload(license_pem, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(fleet, "SECRETS", set())
    return {"fleetmanager": {
        "mode": "offline", "community_string": "private-community-should-never-be-logged",
        "repository_token": "private-repository-token", "license_pem": license_pem,
        "license_sha256": hashlib.sha256(license_pem.encode()).hexdigest(), "license_name": "customer-fleet.pem",
    }, "fleet_files": []}


def add_file(payload, tmp_path, filename="fleetmanager.deb", contents=b"verified installer bytes"):
    path = tmp_path / filename
    path.write_bytes(contents)
    entry = {"filename": filename, "sha256": hashlib.sha256(contents).hexdigest()}
    payload["fleet_files"].append(entry)
    return path


@pytest.fixture
def online_repository(payload, tmp_path, monkeypatch):
    payload["fleetmanager"]["mode"] = "online"
    root = tmp_path / "root"
    monkeypatch.setattr(fleet, "ROOT", root)
    state = {"commands": [], "available": ["29.2.2-1"], "installed": "29.2.2-1", "root": root}

    def runner(command, label, **kwargs):
        state["commands"].append(command)
        if command[0] == "gpg":
            return b"dearmored-key"
        if command[0] == "apt-cache":
            return "\n".join(f" corelight-fleet | {version} | {fleet.REPOSITORY}any/ any/main amd64 Packages" for version in state["available"])
        if command[0] == "dpkg-query":
            return state["installed"]
        if command[-1].startswith("corelight-fleet") and state.get("install_error"):
            raise fleet.FleetInstallError("Requested Fleet Manager package could not be installed.")
        return ""

    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(b"vendor-signing-key")

    monkeypatch.setattr(fleet, "run", runner)
    monkeypatch.setattr(fleet.urllib.request, "build_opener", lambda *args: Opener())
    return state


@pytest.mark.parametrize("changed", ["fleetmanager.deb", "dependency-1.deb"])
def test_modified_transfer_stops_before_package_manager(payload, tmp_path, monkeypatch, changed):
    add_file(payload, tmp_path)
    add_file(payload, tmp_path, "dependency-1.deb", b"dependency bytes")
    (tmp_path / changed).write_bytes(b"replaced while transferring")
    runner = Mock(side_effect=AssertionError("No commands may run before verification"))
    monkeypatch.setattr(fleet.subprocess, "run", runner)
    with pytest.raises(fleet.FleetInstallError, match="SHA-256 changed during transfer"):
        fleet.install(payload)
    runner.assert_not_called()


@pytest.mark.parametrize("change", ["license", "missing_package", "symlink", "metadata", "duplicate"])
def test_invalid_integrity_input_fails_before_any_install(payload, tmp_path, monkeypatch, change):
    path = add_file(payload, tmp_path)
    if change == "license":
        payload["fleetmanager"]["license_pem"] += "changed"
    elif change == "missing_package":
        path.unlink()
    elif change == "symlink":
        path.rename(tmp_path / "elsewhere")
        path.symlink_to(tmp_path / "elsewhere")
    elif change == "metadata":
        payload["fleet_files"][0]["sha256"] = "not-a-digest"
    else:
        payload["fleet_files"].append(dict(payload["fleet_files"][0]))
    runner = Mock(side_effect=AssertionError("No command expected"))
    monkeypatch.setattr(fleet.subprocess, "run", runner)
    with pytest.raises(fleet.FleetInstallError):
        fleet.install(payload)
    runner.assert_not_called()


def test_generated_script_runs_same_integrity_guard(payload, tmp_path):
    add_file(payload, tmp_path)
    script = fleet.installer_script()
    source = re.search(r"python3 - <<'GDEPLOY_FLEET_PY'\n(.*)\nGDEPLOY_FLEET_PY", script, re.S)[1]
    namespace = {"__name__": "fleet_guard_test"}
    exec(compile(source, "fleet-guest-script", "exec"), namespace)
    fingerprint = namespace["verify_transfers"](payload)
    certificate = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", payload["fleetmanager"]["license_pem"], re.S)[0]
    assert fingerprint == hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest()
    (tmp_path / "fleetmanager.deb").write_bytes(b"replaced")
    with pytest.raises(namespace["FleetInstallError"], match="SHA-256 changed"):
        namespace["verify_transfers"](payload)


@pytest.mark.parametrize("online_version", [None, "29.2.2-1", "invalid-unused-online-version"])
def test_offline_pipeline_uses_only_local_packages_and_preserves_configuration(payload, tmp_path, monkeypatch, online_version):
    payload["fleetmanager"]["online_version"] = online_version
    add_file(payload, tmp_path)
    add_file(payload, tmp_path, "dependency-1.deb", b"dependency bytes")
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/corelight-fleetd.conf").write_text(json.dumps({"log-level": "WARN", "retain-setting": "yes"}))
    monkeypatch.setattr(fleet, "ROOT", root)
    monkeypatch.setattr(fleet, "host_checks", lambda: None)
    monkeypatch.setattr(fleet.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()))
    monkeypatch.setattr(fleet, "verify_health", lambda fingerprint: None)
    monkeypatch.setattr(fleet.urllib.request, "build_opener", Mock(side_effect=AssertionError("Offline network access")))
    observed = []

    def runner(command, label, **kwargs):
        observed.append(command)
        if command[0] == "apt-get":
            policy = root / "usr/sbin/policy-rc.d"
            assert policy.read_text() == "#!/bin/sh\nexit 101\n"
            return ""
        assert not (root / "usr/sbin/policy-rc.d").exists()
        if command[0] == "dpkg-query":
            return "29.2.2-1"
        if command[0] == "runuser":
            assert kwargs["private"] is True
            return 'User "admin" successfully created.\nPassword: one-time-private-password\nFleetAdmin: true\n'
        return ""

    monkeypatch.setattr(fleet, "run", runner)
    result = fleet.install(payload)
    assert result == {"username": "admin", "password": "one-time-private-password", "password_change_required": True, "version": "29.2.2-1"}
    apt = [command for command in observed if command[0] == "apt-get"]
    assert len(apt) == 1
    assert "update" not in apt[0] and "--no-download" in apt[0]
    assert "Dir::Etc::sourcelist=/dev/null" in apt[0] and "Dir::Etc::sourceparts=-" in apt[0]
    assert any(value.startswith("Dir::State::lists=") for value in apt[0])
    assert "Dpkg::Options::=--force-confold" in apt[0]
    assert apt[0][-2:] == ["./fleetmanager.deb", "./dependency-1.deb"]
    config = json.loads((root / "etc/corelight-fleetd.conf").read_text())
    assert config["retain-setting"] == "yes" and config["log-level"] == "WARN"
    assert config["bind-address"] == ":443"
    assert config["messaging"]["community-string"] == payload["fleetmanager"]["community_string"]
    assert config["messaging"]["http-options"] == {
        "bind-address": ":1443", "certificate-path": "/etc/corelight-fleetd.pem", "key-path": "/etc/corelight-fleetd.pem",
    }
    identity = root / "etc/corelight-fleetd.pem"
    assert identity.read_text() == payload["fleetmanager"]["license_pem"]
    assert identity.stat().st_mode & 0o777 == 0o400
    assert (root / "etc/corelight-fleetd.conf").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("existing", ["none", "regular", "symlink"])
@pytest.mark.parametrize("fails", [False, True])
def test_install_start_suppression_restores_original_policy(tmp_path, monkeypatch, existing, fails):
    monkeypatch.setattr(fleet, "ROOT", tmp_path)
    policy = tmp_path / "usr/sbin/policy-rc.d"
    policy.parent.mkdir(parents=True)
    original = b"#!/bin/sh\nexit 0\n"
    if existing == "regular":
        policy.write_bytes(original)
        policy.chmod(0o711)
    elif existing == "symlink":
        target = tmp_path / "actual-policy"
        target.write_bytes(original)
        policy.symlink_to(target)
    try:
        with fleet.suppress_package_start():
            assert policy.read_text() == "#!/bin/sh\nexit 101\n"
            if fails:
                raise RuntimeError("installation failed")
    except RuntimeError:
        assert fails
    if existing == "none":
        assert not policy.exists()
    else:
        assert policy.read_bytes() == original
        assert policy.is_symlink() == (existing == "symlink")
        if existing == "regular":
            assert policy.stat().st_mode & 0o777 == 0o711
    assert not list(policy.parent.glob(".gdeploy-policy-*"))


def test_online_repo_keeps_token_out_of_url_argv_and_public_files(payload, tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "ROOT", tmp_path)
    commands = []
    requests = []

    def runner(command, label, **kwargs):
        commands.append(command)
        return b"dearmored-key" if command[0] == "gpg" else ""

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return io.BytesIO(b"vendor-signing-key")

    monkeypatch.setattr(fleet, "run", runner)
    monkeypatch.setattr(fleet.urllib.request, "build_opener", lambda *args: Opener())
    fleet.configure_online_repository(payload["fleetmanager"])
    token = payload["fleetmanager"]["repository_token"]
    assert len(requests) == 1 and requests[0].full_url == fleet.REPOSITORY + "gpgkey"
    assert requests[0].get_header("Authorization") == "Basic " + base64.b64encode((token + ":").encode()).decode()
    assert token not in json.dumps(commands)
    auth = tmp_path / "etc/apt/auth.conf.d/corelight_fleet-stable.conf"
    sources = tmp_path / "etc/apt/sources.list.d/corelight_fleet-stable.list"
    key = tmp_path / "etc/apt/keyrings/corelight_fleet-stable-archive-keyring.gpg"
    assert token in auth.read_text() and auth.stat().st_mode & 0o777 == 0o600
    assert token not in sources.read_text() and "signed-by=" in sources.read_text()
    assert key.read_bytes() == b"dearmored-key" and key.stat().st_mode & 0o777 == 0o644
    assert commands[-1][-2:] == ["--", "corelight-fleet"]
    assert "Dpkg::Options::=--force-confold" in commands[-1]
    with pytest.raises(fleet.FleetInstallError, match="redirected"):
        fleet.NoKeyRedirect().redirect_request(requests[0], None, 302, "", {}, "https://elsewhere.invalid/key")


@pytest.mark.parametrize("version", ["29.2.2-1", "1:29.2.2-1", "29.2.2~rc1+build.4-2"])
def test_online_repository_installs_and_verifies_exact_debian_version(payload, online_repository, version):
    payload["fleetmanager"]["online_version"] = version
    online_repository["available"] = ["30.0.0-1", version]
    online_repository["installed"] = version + "\n"
    assert fleet.configure_online_repository(payload["fleetmanager"]) == version
    commands = online_repository["commands"]
    assert commands[-3:] == [
        ["apt-cache", "madison", "corelight-fleet"],
        fleet.APT_INSTALL + ["--", "corelight-fleet=" + version],
        ["dpkg-query", "-W", "-f=${Version}", "corelight-fleet"],
    ]
    assert commands[-4] == ["apt-get", "-q", "update"]
    assert not any(command[0] == "apt-mark" for command in commands)
    assert not (online_repository["root"] / "etc/apt/preferences.d").exists()


@pytest.mark.parametrize("snapshot_selection", [{}, {"online_version": None}, {"online_version": ""}])
def test_online_legacy_and_latest_install_the_repository_candidate(payload, online_repository, snapshot_selection):
    payload["fleetmanager"].update(snapshot_selection)
    assert fleet.configure_online_repository(payload["fleetmanager"]) is None
    commands = online_repository["commands"]
    assert commands[-1] == fleet.APT_INSTALL + ["--", "corelight-fleet"]
    assert not any(command[0] in {"apt-cache", "dpkg-query", "apt-mark"} for command in commands)


@pytest.mark.parametrize("available", [[], ["29.2.2"], ["29.2.2-10"], ["1:29.2.2-1"], ["30.0.0-1"]])
def test_unavailable_exact_version_does_not_install_or_fall_back(payload, online_repository, available):
    payload["fleetmanager"]["online_version"] = "29.2.2-1"
    online_repository["available"] = available
    with pytest.raises(fleet.FleetInstallError, match="29.2.2-1 is not available.*Choose an available exact"):
        fleet.configure_online_repository(payload["fleetmanager"])
    assert not any(command[0] == "apt-get" and command[-1].startswith("corelight-fleet") for command in online_repository["commands"])


def test_failed_exact_install_does_not_retry_with_latest(payload, online_repository):
    payload["fleetmanager"]["online_version"] = "29.2.2-1"
    online_repository["install_error"] = True
    with pytest.raises(fleet.FleetInstallError, match="could not be installed"):
        fleet.configure_online_repository(payload["fleetmanager"])
    commands = online_repository["commands"]
    assert commands[-1] == fleet.APT_INSTALL + ["--", "corelight-fleet=29.2.2-1"]
    assert not any(command == fleet.APT_INSTALL + ["--", "corelight-fleet"] for command in commands)


@pytest.mark.parametrize("installed", ["29.2.2", "29.2.2-2", "30.0.0-1", ""])
def test_version_mismatch_stops_before_configuration_health_and_admin(payload, online_repository, monkeypatch, installed):
    payload["fleetmanager"]["online_version"] = "29.2.2-1"
    online_repository["installed"] = installed
    monkeypatch.setattr(fleet, "host_checks", lambda: None)
    configure = Mock(side_effect=AssertionError("Version must match before configuration"))
    health = Mock(side_effect=AssertionError("Version must match before health checks"))
    admin = Mock(side_effect=AssertionError("Version must match before creating accounts"))
    monkeypatch.setattr(fleet, "configure_fleet", configure)
    monkeypatch.setattr(fleet, "verify_health", health)
    monkeypatch.setattr(fleet, "create_admin", admin)
    with pytest.raises(fleet.FleetInstallError, match="does not match requested version|did not report its installed"):
        fleet.install(payload)
    configure.assert_not_called()
    health.assert_not_called()
    admin.assert_not_called()
    assert not any(command[0] == "systemctl" for command in online_repository["commands"])
    assert not (online_repository["root"] / "usr/sbin/policy-rc.d").exists()


@pytest.mark.parametrize("version", ["29.2.2-1", "1:29.2.2-1"])
def test_exact_online_pipeline_reports_verified_version(payload, online_repository, monkeypatch, version):
    payload["fleetmanager"]["online_version"] = version
    online_repository["available"] = [version]
    online_repository["installed"] = version
    monkeypatch.setattr(fleet, "host_checks", lambda: None)
    monkeypatch.setattr(fleet, "verify_health", lambda fingerprint: None)
    monkeypatch.setattr(fleet, "create_admin", lambda: "private-initial-password")

    def configure(snapshot):
        assert online_repository["commands"][-1][0] == "dpkg-query"
        assert (online_repository["root"] / "usr/sbin/policy-rc.d").exists()

    monkeypatch.setattr(fleet, "configure_fleet", configure)
    result = fleet.install(payload)
    assert result == {"username": "admin", "password": "private-initial-password", "password_change_required": True, "version": version}
    assert sum(command[0] == "dpkg-query" for command in online_repository["commands"]) == 1


@pytest.mark.parametrize("version", [
    "29.2.2;touch /tmp/unsafe", "$(id)", "29.2.2\n", "29.2.2 --allow-unauthenticated", "29.2.*", "latest",
    "29.2.2/unstable", "-1", "1::29.2.2", "1:rc1", "1" * 129, 29, True, [], {},
])
@pytest.mark.parametrize("entrypoint", ["install", "configure_online_repository"])
def test_invalid_online_version_is_rejected_before_commands(payload, monkeypatch, version, entrypoint):
    payload["fleetmanager"].update({"mode": "online", "online_version": version})
    runner = Mock(side_effect=AssertionError("No package-manager command may run for an invalid version"))
    monkeypatch.setattr(fleet.subprocess, "run", runner)
    with pytest.raises(fleet.FleetInstallError, match="exact Fleet Manager Debian package version"):
        getattr(fleet, entrypoint)(payload if entrypoint == "install" else payload["fleetmanager"])
    runner.assert_not_called()


def test_online_key_failure_never_reports_token(payload, monkeypatch, capsys):
    token = payload["fleetmanager"]["repository_token"]
    register = fleet.register_secrets(payload["fleetmanager"])
    assert register is None
    monkeypatch.setattr(fleet.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout=(token + "\n" + base64.b64encode((token + ":").encode()).decode()).encode(), stderr=b"apt repository unavailable",
    ))
    with pytest.raises(fleet.FleetInstallError, match="exit 1"):
        fleet.run(["apt-get", "update"], "Could not update repository")
    output = capsys.readouterr().err
    assert token not in output and base64.b64encode((token + ":").encode()).decode() not in output
    assert "apt repository unavailable" in output


def test_admin_failure_never_emits_raw_output(monkeypatch, capsys):
    monkeypatch.setattr(fleet.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout=b"Password: password-not-yet-registered\n", stderr=b"private account metadata",
    ))
    with pytest.raises(fleet.FleetInstallError, match="initial Fleet Manager administrator"):
        fleet.create_admin()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("output", ["", "Password:\n", "Password: first\nPassword: second\n"])
def test_admin_requires_one_nonempty_temporary_password(monkeypatch, output):
    monkeypatch.setattr(fleet, "run", lambda *args, **kwargs: output)
    with pytest.raises(fleet.FleetInstallError, match="exactly one"):
        fleet.create_admin()


@pytest.mark.parametrize("free_var,free_tmp,expected", [(29, 40, "/var"), (40, 19, "/tmp"), (30, 20, None)])
def test_host_space_checks_apply_vendor_minimums(tmp_path, monkeypatch, free_var, free_tmp, expected):
    monkeypatch.setattr(fleet, "ROOT", tmp_path)
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
    monkeypatch.setattr(fleet.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(fleet.shutil, "disk_usage", lambda path: SimpleNamespace(free=(free_var if path.name == "var" else free_tmp) * 1024**3))
    if expected:
        with pytest.raises(fleet.FleetInstallError, match=expected):
            fleet.host_checks()
    else:
        fleet.host_checks()


def test_https_rejects_wrong_certificate_before_sending_http(monkeypatch):
    tls = MagicMock()
    tls.__enter__.return_value = tls
    tls.getpeercert.return_value = b"wrong certificate"
    context = Mock()
    context.wrap_socket.return_value = tls
    monkeypatch.setattr(fleet.ssl, "SSLContext", lambda protocol: context)
    connection = MagicMock()
    monkeypatch.setattr(fleet.socket, "create_connection", lambda *args, **kwargs: connection)
    with pytest.raises(fleet.FleetInstallError, match="different from the supplied"):
        fleet.probe_https(hashlib.sha256(b"expected certificate").hexdigest())
    tls.sendall.assert_not_called()


def test_https_exact_certificate_pin_accepts_private_identity(monkeypatch):
    tls = MagicMock()
    tls.__enter__.return_value = tls
    tls.getpeercert.return_value = b"expected certificate"
    context = Mock()
    context.wrap_socket.return_value = tls
    monkeypatch.setattr(fleet.ssl, "SSLContext", lambda protocol: context)
    monkeypatch.setattr(fleet.socket, "create_connection", lambda *args, **kwargs: MagicMock())
    response = Mock(status=302)
    monkeypatch.setattr(fleet.http.client, "HTTPResponse", lambda connection: response)
    fleet.probe_https(hashlib.sha256(b"expected certificate").hexdigest())
    tls.sendall.assert_called_once()
    response.begin.assert_called_once()


def test_session_transfers_snapshot_files_and_returns_private_initial_password(payload, tmp_path, monkeypatch):
    snapshot = dict(payload["fleetmanager"])
    snapshot["package"] = {"path": str(tmp_path / "main.deb"), "sha256": "a" * 64}
    snapshot["dependencies"] = [{"path": str(tmp_path / "dependency.deb"), "sha256": "b" * 64}]
    session = guest.GuestSession("192.0.2.20", "gdeploy", "ssh-private-key", "guest-password")
    logs = []

    def scripted(script, data, *, extra_files):
        assert extra_files == {"fleetmanager.deb": Path(snapshot["package"]["path"]), "dependency-1.deb": Path(snapshot["dependencies"][0]["path"])}
        assert data["fleet_files"] == [{"filename": "fleetmanager.deb", "sha256": "a" * 64}, {"filename": "dependency-1.deb", "sha256": "b" * 64}]
        assert data["fleetmanager"]["online_version"] == ""
        assert snapshot["community_string"] in session._secrets
        assert snapshot["repository_token"] in session._secrets
        assert snapshot["license_pem"] in session._secrets
        assert "policy-rc.d" in script
        return {"username": "admin", "password": "fleet-temporary-secret", "password_change_required": True, "version": "29.2.2"}

    monkeypatch.setattr(session, "_run_script", scripted)
    result = session.install("fleetmanager", {}, fleetmanager=snapshot, log=logs.append)
    assert result["services"] == [{"name": "Fleet Manager", "url": "https://192.0.2.20", "username": "admin", "password": "fleet-temporary-secret", "password_change_required": True, "version": "29.2.2", "community_string": snapshot["community_string"]}]
    assert "fleet-temporary-secret" in session._secrets
    assert "fleet-temporary-secret" not in "\n".join(logs)


@pytest.mark.parametrize("selection", [{}, {"online_version": None}, {"online_version": ""}, {"online_version": "1:29.2.2-1"}])
def test_session_forwards_selected_version_to_actual_generated_installer(payload, monkeypatch, selection):
    snapshot = {**payload["fleetmanager"], "mode": "online", **selection}
    session = guest.GuestSession("192.0.2.20", "gdeploy", "ssh-private-key", "guest-password")
    expected = selection.get("online_version") or ""

    def scripted(script, data, *, extra_files):
        assert data["fleetmanager"]["online_version"] == expected
        assert extra_files == {} and data["fleet_files"] == []
        source = re.search(r"python3 - <<'GDEPLOY_FLEET_PY'\n(.*)\nGDEPLOY_FLEET_PY", script, re.S)[1]
        namespace = {"__name__": "fleet_version_test"}
        exec(compile(source, "fleet-guest-script", "exec"), namespace)
        namespace["verify_transfers"](data)
        assert namespace["requested_online_version"](data["fleetmanager"]) == expected
        return {"username": "admin", "password": "initial-private-password", "password_change_required": True, "version": expected or "30.0.0-1"}

    monkeypatch.setattr(session, "_run_script", scripted)
    result = session.install("fleetmanager", {}, fleetmanager=snapshot)
    assert result["services"][0]["version"] == (expected or "30.0.0-1")


@pytest.mark.parametrize("version", ["29.2.2\n", "$(id)", 29, False, []])
def test_session_rejects_invalid_online_selection_before_ssh(payload, monkeypatch, version):
    snapshot = {**payload["fleetmanager"], "mode": "online", "online_version": version}
    session = guest.GuestSession("192.0.2.20", "gdeploy", "ssh-private-key", "guest-password")
    scripted = Mock(side_effect=AssertionError("Do not transfer invalid version selections"))
    monkeypatch.setattr(session, "_run_script", scripted)
    with pytest.raises(guest.GuestError, match="exact Fleet Manager Debian package version"):
        session.install("fleetmanager", {}, fleetmanager=snapshot)
    scripted.assert_not_called()


@pytest.mark.parametrize("filename", ["../outside", "/outside", "payload.json", "install.sh", "splunk.tgz", ".hidden", "line\nbreak.deb"])
def test_extra_transfer_names_cannot_escape_or_replace_payload(tmp_path, filename):
    session = guest.GuestSession("192.0.2.20", "gdeploy", "ssh-private-key", "guest-password")
    session.client = Mock()
    with pytest.raises(guest.GuestError, match="safe, unique"):
        session._run_script("script", {}, extra_files={filename: tmp_path / "source"})
    session.client.open_sftp.assert_not_called()


def test_extra_transfer_partial_failure_cleans_private_staging(tmp_path, monkeypatch):
    session = guest.GuestSession("192.0.2.20", "gdeploy", "ssh-private-key", "guest-password")
    session.client = MagicMock()
    sftp = session.client.open_sftp.return_value.__enter__.return_value
    sftp.put.side_effect = OSError("transfer interrupted")
    execute = Mock(return_value="")
    monkeypatch.setattr(session, "_exec", execute)
    with pytest.raises(guest.GuestError, match="Could not transfer"):
        session._run_script("script", {}, extra_files={"fleetmanager.deb": tmp_path / "source"})
    assert execute.call_count == 1 and execute.call_args.args[0].startswith("rm -rf -- /home/gdeploy/.gdeploy-")


def test_extra_transfer_keeps_positional_splunk_contract_and_file_permissions(tmp_path, monkeypatch):
    session = guest.GuestSession("192.0.2.20", "gdeploy", "ssh-private-key", "guest-password")
    session.client = MagicMock()
    sftp = session.client.open_sftp.return_value.__enter__.return_value
    monkeypatch.setattr(session, "_exec", lambda *args, **kwargs: 'GDEPLOY_RESULT={"ok":true}\n')
    result = session._run_script("script", {}, tmp_path / "splunk.tgz", extra_files={"fleetmanager.deb": tmp_path / "fleet.deb"})
    assert result == {"ok": True}
    destinations = [call.args[1] for call in sftp.put.call_args_list]
    assert destinations[0].endswith("/splunk.tgz") and destinations[1].endswith("/fleetmanager.deb")
    assert all(call.args[1] == 0o600 for call in sftp.chmod.call_args_list)


def test_journal_redacts_community_token_and_license_fragments(payload):
    snapshot = payload["fleetmanager"]
    fleet.register_secrets(snapshot)
    fragment = snapshot["license_pem"].splitlines()[1]
    text = "\n".join([
        "service cannot start", snapshot["community_string"], snapshot["repository_token"], fragment,
        base64.b64encode((snapshot["repository_token"] + ":irrelevant").encode()).decode(),
        snapshot["license_pem"], "Password: previously-unknown-dynamic-password",
    ])
    output = fleet.sanitize(text)
    assert "service cannot start" in output
    for secret in (snapshot["community_string"], snapshot["repository_token"], fragment, "previously-unknown-dynamic-password"):
        assert secret not in output


def test_timeout_does_not_include_command_or_partial_output(monkeypatch, capsys):
    monkeypatch.setattr(fleet.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired(["secret-command"], 1, output=b"secret-output")))
    with pytest.raises(fleet.FleetInstallError, match="timed out") as error:
        fleet.run(["secret-command"], "Install failed")
    assert "secret" not in str(error.value)
    assert capsys.readouterr() == ("", "")
