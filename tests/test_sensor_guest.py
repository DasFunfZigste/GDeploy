"""Exercise sensor provisioning without executing vendor packages or guest mutations."""

import base64
import io
import json
import subprocess
import urllib.error
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import yaml

from gdeploy import fleet_guest, guest, sensor_guest as sensor


@pytest.fixture
def snapshot(monkeypatch):
    monkeypatch.setattr(sensor, "SECRETS", set())
    return {
        "repository_token": "customer-token-private", "community_string": 'private community "quoted"',
        "license_key": "license-key-private", "pairing_token": "single-use-private-token",
        "fleet_url": "https://fleet.example.test:1443/pair", "server_sslname": "fleet-cert.example.test",
        "management_mac": "00:50:56:01:02:03", "monitor_mac": "00:50:56:01:02:04",
        "api_network": "192.168.30.0/24",
    }


@pytest.fixture
def payload(snapshot):
    return {"corelight_sensor": snapshot, "ip": "192.168.30.10"}


@pytest.mark.parametrize("field,value", [
    ("repository_token", "user:password"), ("repository_token", "contains space"),
    ("community_string", "line\nbreak"), ("license_key", ""), ("license_key", None),
    ("pairing_token", ""), ("pairing_token", "contains space"), ("pairing_token", "\u00e9"),
    ("fleet_url", "http://fleet.example.test:1443"), ("fleet_url", "https://token@fleet.example.test"),
    ("fleet_url", "https://fleet.example.test/?token=secret"), ("fleet_url", "https://fleet.example.test:0"),
    ("fleet_url", "https://fleet.example.test/#secret"), ("fleet_url", "https://fleet.example.test\\secret"),
    ("server_sslname", "https://fleet.example.test"), ("server_sslname", "fleet..test"),
    ("management_mac", "01:50:56:01:02:03"), ("monitor_mac", "00:00:00:00:00:00"),
    ("monitor_mac", "00:50:56:01:02:03"), ("monitor_mac", None),
    ("api_network", "::/0"), ("api_network", "192.168.30.999/24"),
])
def test_invalid_snapshots_fail_before_guest_commands(payload, field, value, monkeypatch):
    payload["corelight_sensor"][field] = value
    runner = Mock(side_effect=AssertionError("No guest command should run"))
    monkeypatch.setattr(sensor, "run", runner)
    with pytest.raises(sensor.SensorInstallError):
        sensor.install(payload)
    runner.assert_not_called()


def test_configuration_matches_vendor_nested_schema_and_quotes_strings(snapshot):
    normalized = sensor.validate_snapshot(snapshot)
    configuration = yaml.safe_load(sensor.sensor_configuration(normalized))
    assert set(configuration) == {"sensor"}
    assert configuration["sensor"] == {
        "api": {"password": snapshot["community_string"]}, "license_key": snapshot["license_key"],
        "management_interface": [{"name": "gdeploymgmt"}], "monitoring_interface": {"name": "gdeploymon"},
        "pairing": {"url": snapshot["fleet_url"], "server_sslname": snapshot["server_sslname"], "token": snapshot["pairing_token"]},
        "kubernetes": {"allow_ports": [{"protocol": "tcp", "port": 443, "net": "192.168.30.0/24"}]},
    }


def test_snapshot_defaults_pairing_port_and_normalizes_api_network(snapshot):
    snapshot.update(fleet_url="https://fleet.example.test", api_network="192.168.30.101/24")
    normalized = sensor.validate_snapshot(snapshot)
    assert normalized["fleet_url"] == "https://fleet.example.test:1443"
    assert normalized["api_network"] == "192.168.30.0/24"


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(sensor, "ROOT", tmp_path)
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
    monkeypatch.setattr(sensor.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(sensor.shutil, "disk_usage", lambda path: NS(free=500 * 1000**3))
    state = {"mount": "rw,relatime", "loader": "  x86-64-v3 (supported, searched)\n"}

    def run(command, *args, **kwargs):
        return state["mount"] if command[0] == "findmnt" else state["loader"]

    monkeypatch.setattr(sensor, "run", run)
    return state


def test_guest_minimum_500_decimal_gb_and_cpu_v3(host):
    sensor.host_checks()


@pytest.mark.parametrize("failure", ["ubuntu22", "debian", "arm", "space", "noexec", "cpu-v2"])
def test_host_checks_reject_unsupported_resources(host, monkeypatch, failure):
    if failure == "ubuntu22":
        (sensor.ROOT / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="22.04"\n')
    if failure == "debian":
        (sensor.ROOT / "etc/os-release").write_text('ID=debian\nVERSION_ID="24.04"\n')
    if failure == "arm":
        monkeypatch.setattr(sensor.platform, "machine", lambda: "aarch64")
    if failure == "space":
        monkeypatch.setattr(sensor.shutil, "disk_usage", lambda path: NS(free=500 * 1000**3 - 1))
    if failure == "noexec":
        host["mount"] = "rw,noexec,relatime"
    if failure == "cpu-v2":
        host["loader"] = "  x86-64-v3\n  x86-64-v2 (supported, searched)\n"
    with pytest.raises(sensor.SensorInstallError):
        sensor.host_checks()


@pytest.fixture
def network(snapshot, monkeypatch):
    state = {"interfaces": [
        {"ifname": "gdeploymgmt", "address": snapshot["management_mac"], "addr_info": [{"family": "inet", "local": "192.168.30.10"}]},
        {"ifname": "gdeploymon", "address": snapshot["monitor_mac"], "addr_info": []},
    ], "routes": {"-4": [{"dst": "default", "dev": "gdeploymgmt"}], "-6": []}, "commands": []}

    def run(command, *args, **kwargs):
        state["commands"].append(command)
        if command[:3] == ["ip", "-j", "address"]:
            return json.dumps(state["interfaces"])
        if command[1] == "-j":
            return json.dumps(state["routes"][command[2]])
        return ""

    monkeypatch.setattr(sensor, "run", run)
    return state


def test_network_checks_management_identity_and_monitor_has_no_addresses_or_routes(snapshot, network):
    sensor.verify_network(snapshot, "192.168.30.10")
    assert network["commands"][-1] == ["ip", "link", "set", "dev", "gdeploymon", "up"]


def test_network_allows_only_address_free_kernel_ipv6_multicast_local_route(snapshot, network):
    network["routes"]["-6"] = [{
        "dev": "gdeploymon", "type": "multicast", "dst": "ff00::/8", "table": "local", "protocol": "kernel", "metric": 256,
    }]
    sensor.verify_network(snapshot, "192.168.30.10")


@pytest.mark.parametrize("changes", [
    {"type": "unicast"}, {"table": "main"}, {"protocol": "static"}, {"dst": "default"},
    {"dst": "2001:db8::/64"}, {"gateway": "fe80::1"}, {"via": {"host": "fe80::1"}},
    {"nexthops": [{"dev": "gdeploymon", "gateway": "fe80::1"}]},
])
def test_monitor_rejects_routed_networking_even_when_other_fields_resemble_multicast(snapshot, network, changes):
    network["routes"]["-6"] = [{
        "dev": "gdeploymon", "type": "multicast", "dst": "ff00::/8", "table": "local", "protocol": "kernel", **changes,
    }]
    with pytest.raises(sensor.SensorInstallError, match="network verification failed"):
        sensor.verify_network(snapshot, "192.168.30.10")


def test_monitor_rejects_multipath_gateway_without_top_level_device(snapshot, network):
    network["routes"]["-4"] = [{"dst": "default", "nexthops": [{"dev": "gdeploymon", "gateway": "192.168.30.1"}]}]
    with pytest.raises(sensor.SensorInstallError, match="network verification failed"):
        sensor.verify_network(snapshot, "192.168.30.10")


@pytest.mark.parametrize("failure", ["swapped", "unknown", "wrong-ip", "monitor-v4", "monitor-v6", "route-v4", "route-v6"])
def test_network_checks_fail_before_monitor_mutation(snapshot, network, failure):
    if failure == "swapped":
        network["interfaces"][0]["address"], network["interfaces"][1]["address"] = snapshot["monitor_mac"], snapshot["management_mac"]
    if failure == "unknown":
        network["interfaces"][0]["ifname"] = "ens33"
    if failure == "wrong-ip":
        network["interfaces"][0]["addr_info"][0]["local"] = "192.168.30.11"
    if failure.startswith("monitor"):
        network["interfaces"][1]["addr_info"] = [{"family": "inet" if failure == "monitor-v4" else "inet6", "local": "unexpected"}]
    if failure.startswith("route"):
        network["routes"]["-4" if failure == "route-v4" else "-6"] = [{"dst": "default", "dev": "gdeploymon"}]
    with pytest.raises(sensor.SensorInstallError, match="network verification failed"):
        sensor.verify_network(snapshot, "192.168.30.10")
    assert not any(command[:2] == ["ip", "link"] for command in network["commands"])


def test_repository_writes_private_auth_and_signed_source_without_token_in_commands(snapshot, tmp_path, monkeypatch):
    monkeypatch.setattr(sensor, "ROOT", tmp_path)
    commands = []

    def run(command, *args, **kwargs):
        commands.append(command)
        if command[0] == "gpg":
            assert kwargs["input"] == b"signing-key"
            return b"binary-key"
        return "29.2.1-1" if command[0] == "dpkg-query" else ""

    download = Mock(return_value=b"signing-key")
    monkeypatch.setattr(sensor, "run", run)
    monkeypatch.setattr(sensor, "download_repository_file", download)
    assert sensor.configure_repository(snapshot) == "29.2.1-1"
    download.assert_called_once_with(snapshot["repository_token"], "gpgkey", repository=sensor.REPOSITORY)
    auth = tmp_path / "etc/apt/auth.conf.d/corelight_sensor-stable.conf"
    assert auth.stat().st_mode & 0o777 == 0o600
    assert "login " + snapshot["repository_token"] in auth.read_text()
    source = (tmp_path / "etc/apt/sources.list.d/corelight_sensor-stable.list").read_text()
    assert "signed-by=/etc/apt/keyrings/corelight_sensor-stable-archive-keyring.gpg" in source
    assert sensor.REPOSITORY + "any/ any main" in source
    assert snapshot["repository_token"] not in str(commands) + source
    assert commands[-2][-2:] == ["corelightctl", "corelight-sensor"]
    assert not any("ufw" in command for command in commands)


@pytest.mark.parametrize("status", [401, 403])
def test_repository_errors_use_sensor_label_without_secret(snapshot, monkeypatch, status):
    monkeypatch.setattr(sensor, "run", Mock(return_value=""))
    monkeypatch.setattr(sensor, "download_repository_file", Mock(side_effect=fleet_guest.RepositoryDownloadError(f"Fleet Manager repository HTTP {status}")))
    with pytest.raises(sensor.SensorInstallError, match=f"Software Sensor repository HTTP {status}"):
        sensor.configure_repository(snapshot)


def test_dedicated_guest_disables_competing_firewall_without_using_fleet_ufw_rules(monkeypatch):
    commands = []
    monkeypatch.setattr(sensor.shutil, "which", lambda name: "/usr/sbin/ufw")

    def run(command, *args, **kwargs):
        commands.append(command)
        if command[1] == "list-unit-files":
            return "ufw.service enabled enabled\nfirewalld.service disabled enabled\n"
        return "inactive\n"

    monkeypatch.setattr(sensor, "run", run)
    sensor.disable_competing_firewalls()
    assert ["ufw", "disable"] in commands
    for unit in ("ufw.service", "firewalld.service"):
        assert ["systemctl", "disable", "--now", unit] in commands
    assert not any("allow" in command or "reset" in command for command in commands)


def test_missing_firewalls_need_no_mutation(monkeypatch):
    monkeypatch.setattr(sensor.shutil, "which", lambda name: None)
    runner = Mock(return_value="")
    monkeypatch.setattr(sensor, "run", runner)
    sensor.disable_competing_firewalls()
    assert runner.call_count == 1
    assert runner.call_args.args[0][1] == "list-unit-files"


HEALTHY = "Services:\n connection-manager : Ok\n sensor-api : Ok\n sensor-core : Ok\n sensor-operator : Ok\n"


@pytest.mark.parametrize("output,healthy", [
    ("", False), ("Deployment completed", False), ("Services:\n sensor-core : Ok\n", False),
    (HEALTHY, True), (HEALTHY.replace("sensor-core : Ok", "sensor-core : Error - missing license"), False),
    (HEALTHY.replace("connection-manager : Ok", "connection-manager : Error - pairing failed"), False),
    (HEALTHY + " kafka : Failed\n", False),
    (HEALTHY + " suricata : Warning - Suricata doesn't have any rules installed\n", True),
    (HEALTHY + " suricata : Warning - engine failed\n", False), (HEALTHY + " sensor-core : Error\n", False),
])
def test_health_requires_core_api_pairing_and_rejects_other_failed_services(output, healthy):
    assert sensor.service_health(output)[0] is healthy


def test_health_waits_for_core_and_uses_system_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(sensor, "ROOT", tmp_path)
    responses = iter((HEALTHY.replace("sensor-core : Ok", "sensor-core : Starting"), HEALTHY))
    runner = Mock(side_effect=lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(sensor, "run", runner)
    monkeypatch.setattr(sensor.time, "sleep", lambda seconds: None)
    sensor.verify_health(timeout=30)
    assert runner.call_count == 2
    assert runner.call_args.kwargs["cwd"] == tmp_path / "etc/corelight"


def test_health_timeout_never_reports_completed(monkeypatch):
    ticks = iter((0, 0, 0, 10, 30))
    monkeypatch.setattr(sensor.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(sensor.time, "sleep", lambda value: None)
    monkeypatch.setattr(sensor, "run", lambda *args, **kwargs: HEALTHY.replace("sensor-core : Ok", "sensor-core : Error - missing license"))
    with pytest.raises(sensor.SensorInstallError, match="sensor-core is not healthy"):
        sensor.verify_health(timeout=20)


def test_install_orders_prepare_init_configuration_deploy_health(payload, tmp_path, monkeypatch):
    monkeypatch.setattr(sensor, "ROOT", tmp_path)
    events = []
    monkeypatch.setattr(sensor, "host_checks", lambda: events.append("host"))
    monkeypatch.setattr(sensor, "verify_network", lambda *args: events.append("network"))
    monkeypatch.setattr(sensor, "configure_repository", lambda *args: events.append("repository") or "29.2.1-1")
    monkeypatch.setattr(sensor, "disable_competing_firewalls", lambda: events.append("firewall"))
    monkeypatch.setattr(sensor, "verify_health", lambda: events.append("health"))

    def run(command, *args, **kwargs):
        events.append(" ".join(command))
        if command[1] == "init":
            assert kwargs["cwd"] == tmp_path / "etc/corelight"
        if "deploy" in command:
            path = tmp_path / "etc/corelight/corelightctl.yaml"
            assert path.stat().st_mode & 0o777 == 0o600
            assert payload["corelight_sensor"]["pairing_token"] in path.read_text()
        return ""

    monkeypatch.setattr(sensor, "run", run)
    assert sensor.install(payload) == {"healthy": True, "version": "29.2.1-1"}
    assert events == ["host", "network", "repository", "firewall", "corelightctl sensor prepare", "corelightctl init", "corelightctl sensor deploy -v", "health", "network"]


def test_secret_values_are_redacted_from_failures_and_encoded_forms(snapshot, monkeypatch, capsys):
    sensor.SECRETS.update(sensor.secret_values(snapshot))
    raw = " ".join(str(value) for value in snapshot.values()) + " " + base64.b64encode((snapshot["repository_token"] + ":").encode()).decode()
    monkeypatch.setattr(sensor.subprocess, "run", lambda *args, **kwargs: NS(returncode=1, stdout=raw.encode(), stderr=b"license_key: unexpected-diagnostic-value"))
    with pytest.raises(sensor.SensorInstallError, match="exit 1"):
        sensor.run(["corelightctl"], "Could not deploy Software Sensor")
    diagnostics = capsys.readouterr().err
    for key in ("community_string", "repository_token", "license_key", "pairing_token"):
        assert snapshot[key] not in diagnostics
    assert "unexpected-diagnostic-value" not in diagnostics


def test_guest_result_exposes_documented_api_credential_without_invented_admin(snapshot, monkeypatch):
    session = guest.GuestSession("192.168.30.10", "gdeploy", "password", "private-key")
    runner = Mock(return_value={"healthy": True, "version": "29.2.1-1"})
    monkeypatch.setattr(session, "_run_script", runner)
    result = session.install("corelight_sensor", {}, corelight_sensor=snapshot)
    assert result == {"services": [{"name": "Corelight Software Sensor API", "url": "https://192.168.30.10", "community_string": snapshot["community_string"], "version": "29.2.1-1"}]}
    assert snapshot["pairing_token"] not in json.dumps(result)
    assert snapshot["license_key"] not in json.dumps(result)
    assert runner.call_args.kwargs["timeout"] == 5400


@pytest.mark.parametrize("response", [{}, {"healthy": False, "version": "29.2.1-1"}, {"healthy": True}])
def test_guest_rejects_unverified_sensor_result(snapshot, monkeypatch, response):
    session = guest.GuestSession("192.168.30.10", "gdeploy", "password", "private-key")
    monkeypatch.setattr(session, "_run_script", Mock(return_value=response))
    with pytest.raises(guest.GuestError, match="healthy sensor-core"):
        session.install("corelight_sensor", {}, corelight_sensor=snapshot)


def test_emitted_sensor_script_is_standalone_and_uses_shared_safe_downloader(tmp_path):
    script = sensor.installer_script()
    python = script.split("python3 - <<'GDEPLOY_SENSOR_PY'\n", 1)[1].rsplit("\nGDEPLOY_SENSOR_PY", 1)[0]
    namespace = {"__name__": "sensor_standalone"}
    exec(compile(python, "sensor_standalone.py", "exec"), namespace)
    assert namespace["REPOSITORY"] == sensor.REPOSITORY
    assert namespace["download_repository_file"].__code__.co_code == fleet_guest.download_repository_file.__code__.co_code
    path = tmp_path / "install.sh"
    path.write_text(script)
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True)
    assert result.returncode == 0


def test_sensor_downloader_authenticates_only_original_origin_and_fixed_repository(snapshot, monkeypatch):
    requests = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise urllib.error.HTTPError(request.full_url, 302, "redirect", {"Location": "https://storage.example.test/signed-key?private=query"}, io.BytesIO())
            return Response(b"signing-key")

    monkeypatch.setattr(fleet_guest.urllib.request, "build_opener", lambda *args: Opener())
    assert fleet_guest.download_repository_file(snapshot["repository_token"], "gpgkey", repository=sensor.REPOSITORY) == b"signing-key"
    assert requests[0].full_url == sensor.REPOSITORY + "gpgkey"
    assert requests[0].get_header("Authorization")
    assert requests[1].get_header("Authorization") is None
    with pytest.raises(fleet_guest.RepositoryDownloadError, match="supported Corelight"):
        fleet_guest.download_repository_file(snapshot["repository_token"], "gpgkey", repository="https://untrusted.example.test/")
