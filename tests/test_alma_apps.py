import json
import re
import subprocess

import pytest

from gdeploy import alma_apps, guest


@pytest.mark.parametrize("role", ["elasticsearch", "kibana", "splunk"])
def test_alma_scripts_parse_and_keep_protected_payloads(role):
    script = alma_apps.application_script(role)
    shell = subprocess.run(["/bin/bash", "-n"], input=script, text=True, capture_output=True)
    assert shell.returncode == 0, shell.stderr
    for index, snippet in enumerate(re.findall(r"python3 - <<'PY'\n(.*?)\nPY", script, re.S)):
        compile(snippet, f"alma-{role}-{index}", "exec")
    assert "test \"${ID:-}\" = almalinux" in script
    assert 'test "${VERSION_ID%%.*}" = 10' in script
    assert "payload.json" in script
    assert "umask 077" in script
    assert "apt-get" not in script
    assert "apt-mark" not in script
    assert "dpkg-query" not in script
    assert "set -x" not in script
    assert "setenforce" not in script
    assert "GDEPLOY_RESULT=" in script
    assert "ssl.create_default_context" in script
    assert "_create_unverified_context" not in script


def test_elastic_rpm_keys_packages_health_and_cross_vm_material_remain_verified():
    elastic = alma_apps.application_script("elasticsearch")
    kibana = alma_apps.application_script("kibana")
    for script in (elastic, kibana):
        assert "46095ACC8548582C1A2699A9D27D666CD88E42B4" in script
        assert "gpgcheck=1" in script
        assert "https://artifacts.elastic.co/packages/9.x/yum" in script
        assert "rpm --import /etc/pki/rpm-gpg/GPG-KEY-elasticsearch" in script
        assert "--nogpgcheck" not in script
        assert "excludepkgs" not in script
        assert "versionlock" not in script
    assert "elasticsearch.x86_64" in elastic
    assert "elastic-package.log 2>&1" in elastic
    assert "rpm_package('elasticsearch')" in elastic
    assert "'rpm_release':installed['rpm_release']" in elastic
    assert "/_cluster/health?wait_for_status=yellow" in elastic
    assert "/_security/service/elastic/kibana/credential/token/" in elastic
    assert "input=(password+'\\n'+password+'\\n').encode()" in elastic
    assert "install_kibana_rpm(p['elastic'])" in kibana
    assert "(cert/'elasticsearch-ca.crt').write_text(p['elastic']['ca_pem'])" in kibana
    assert "status',{}).get('overall',{}).get('level') == 'available'" in kibana
    assert elastic.index("configure_firewall(p['ip'], (9200,))") < elastic.index("systemctl enable --now elasticsearch")
    assert kibana.index("configure_firewall(p['ip'], (5601,))") < kibana.index("systemctl enable --now kibana")


def test_splunk_keeps_archive_integrity_credentials_tls_and_systemd():
    script = alma_apps.application_script("splunk")
    assert "numactl-libs" in script
    assert "hmac.compare_digest(actual, p['package_sha256'])" in script
    assert script.index("hmac.compare_digest") < script.index("dnf -q")
    assert "splunk.tgz" in script
    assert "--no-same-owner" in script
    assert "p['secrets']['splunk_password']" in script
    assert "chmod 600 /opt/splunk/etc/system/local/user-seed.conf" in script
    assert "systemctl enable --now Splunkd" in script
    assert "services/server/info?output_mode=json" in script
    assert "user-seed.conf').unlink(missing_ok=True)" in script
    assert script.index("configure_firewall(p['ip'], (8000, 8089))") < script.index("runuser -u splunk -- /opt/splunk/bin/splunk start")


@pytest.mark.parametrize("role", ["ubuntu", "fleetmanager", "corelight_sensor", "", "../splunk"])
def test_unrelated_roles_fail_closed(role):
    with pytest.raises(alma_apps.AlmaInstallError, match="Unsupported"):
        alma_apps.application_script(role)


def test_upstream_script_changes_require_explicit_review(monkeypatch):
    monkeypatch.setattr(guest, "_SPLUNK_SCRIPT", guest._SPLUNK_SCRIPT.replace("apt-get -q update", "changed-repository-step"))
    with pytest.raises(alma_apps.AlmaInstallError, match="needs review"):
        alma_apps.application_script("splunk")


@pytest.fixture
def firewall_commands(monkeypatch):
    calls = []
    responses = {
        ("ip", "-j", "-4", "address", "show"): json.dumps([
            {"ifname": "lo", "addr_info": [{"local": "127.0.0.1"}]},
            {"ifname": "ens33", "addr_info": [{"local": "192.0.2.20"}]},
        ]),
        ("systemctl", "show", "firewalld.service", "-p", "ActiveState", "--value"): "active",
        ("firewall-cmd", "--get-default-zone"): "public",
        ("firewall-cmd", "--get-active-zones"): "work\n  interfaces: ens33\ntrusted\n  interfaces: ens34\n",
    }

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return responses.get(tuple(args), "")

    monkeypatch.setattr(alma_apps, "run_command", run)
    return calls, responses


def test_firewall_uses_management_zone_and_preserves_ssh_before_ports(firewall_commands):
    calls, _ = firewall_commands
    alma_apps.configure_firewall("192.0.2.20", (8000, 8089))
    commands = [args for args, _ in calls]
    for permanent in (True, False):
        prefix = ["firewall-cmd", "--zone=work"] + (["--permanent"] if permanent else [])
        ssh = prefix + ["--add-service=ssh"]
        assert commands.index(ssh) < commands.index(prefix + ["--add-port=8000/tcp"])
        assert prefix + ["--add-port=8089/tcp"] in commands
    assert ["firewall-cmd", "--zone=work", "--query-service=ssh"] in commands
    assert ["firewall-cmd", "--zone=work", "--query-port=8000/tcp"] in commands
    assert all("--reload" not in args and "--complete-reload" not in args for args in commands)
    assert all("--zone=trusted" not in args and "--zone=public" not in args for args in commands)


def test_starting_firewall_keeps_default_ssh_open_first(firewall_commands):
    calls, responses = firewall_commands
    responses[("systemctl", "show", "firewalld.service", "-p", "ActiveState", "--value")] = "inactive"
    responses[("firewall-cmd", "--get-active-zones")] = ""
    alma_apps.configure_firewall("192.0.2.20", (5601,))
    commands = [args for args, _ in calls]
    assert commands.index(["firewall-offline-cmd", "--add-service=ssh"]) < commands.index(["systemctl", "enable", "--now", "firewalld"])
    assert ["firewall-cmd", "--zone=public", "--add-port=5601/tcp"] in commands


@pytest.mark.parametrize("output", ["not-json", "{}", "[]", '[{"ifname":"ens33","addr_info":[]}]'])
def test_firewall_cannot_open_a_guessed_interface(firewall_commands, output):
    calls, responses = firewall_commands
    responses[("ip", "-j", "-4", "address", "show")] = output
    with pytest.raises(alma_apps.AlmaInstallError, match="network information|management interface"):
        alma_apps.configure_firewall("192.0.2.20", (9200,))
    assert not any(args[0] == "firewall-cmd" for args, _ in calls)


@pytest.mark.parametrize("state", ["failed", "activating", "", "not-found"])
def test_failed_firewall_service_does_not_rewrite_policy(firewall_commands, state):
    calls, responses = firewall_commands
    responses[("systemctl", "show", "firewalld.service", "-p", "ActiveState", "--value")] = state
    with pytest.raises(alma_apps.AlmaInstallError, match="not ready"):
        alma_apps.configure_firewall("192.0.2.20", (9200,))
    assert not any(args[0].startswith("firewall") for args, _ in calls)


def test_zone_discovery_errors_remain_fatal(firewall_commands):
    calls, responses = firewall_commands
    responses[("firewall-cmd", "--get-active-zones")] = ""
    responses[("firewall-cmd", "--get-default-zone")] = "public --drop"
    with pytest.raises(alma_apps.AlmaInstallError, match="invalid management zone"):
        alma_apps.configure_firewall("192.0.2.20", (9200,))
    assert not any("--add-port=9200/tcp" in args for args, _ in calls)


@pytest.mark.parametrize("mode", ["Enforcing", "Permissive"])
def test_selinux_restores_labels_without_changing_enforcement(monkeypatch, mode):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return mode if args == ["getenforce"] else ""

    monkeypatch.setattr(alma_apps, "run_command", run)
    alma_apps.restore_selinux(("/etc/elasticsearch", "/var/lib/elasticsearch"))
    assert calls[-1] == (["restorecon", "-RF", "/etc/elasticsearch", "/var/lib/elasticsearch"], {"timeout": 180})
    assert not any(args[0] == "setenforce" for args, _ in calls)


@pytest.mark.parametrize("existing, operation", [("", "-a"), ("/opt/splunk/bin/splunk(d)? all files system_u:object_r:bin_t:s0", "-m")])
def test_splunk_uses_persistent_standard_executable_selinux_labels(monkeypatch, existing, operation):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args == ["getenforce"]:
            return "Enforcing"
        return existing if args[:2] == ["semanage", "fcontext"] and "-l" in args else ""

    monkeypatch.setattr(alma_apps, "run_command", run)
    alma_apps.restore_selinux(("/opt/splunk",), splunk=True)
    assert ["semanage", "fcontext", operation, "-t", "bin_t", "/opt/splunk/bin/splunk(d)?"] in calls
    assert calls[-1] == ["restorecon", "-RF", "/opt/splunk"]


def test_disabled_selinux_needs_no_policy_changes(monkeypatch):
    calls = []
    monkeypatch.setattr(alma_apps, "run_command", lambda args, **kwargs: calls.append(args) or "Disabled")
    alma_apps.restore_selinux(("/opt/splunk",), splunk=True)
    assert calls == [["getenforce"]]


@pytest.mark.parametrize("installed", ["9.3.1|1|x86_64", "9.3.1|1.el10|x86_64"])
def test_rpm_readback_returns_matched_version_release(monkeypatch, installed):
    monkeypatch.setattr(alma_apps, "run_command", lambda *args, **kwargs: installed)
    assert alma_apps.rpm_package("elasticsearch") == {"version": "9.3.1", "rpm_release": installed.split("|")[1]}


@pytest.mark.parametrize("installed", ["8.18.0|1|x86_64", "9.3.1|1|aarch64", "9.3.1|1;echo bad|x86_64", "9.3.1", "9.3.1|1|x86_64\n9.3.1|2|x86_64"])
def test_bad_rpm_readbacks_are_fatal(monkeypatch, installed):
    monkeypatch.setattr(alma_apps, "run_command", lambda *args, **kwargs: installed)
    with pytest.raises(alma_apps.AlmaInstallError, match="Unexpected"):
        alma_apps.rpm_package("elasticsearch")


@pytest.mark.parametrize("release", [None, "1", "1.el10"])
def test_kibana_installs_exact_version_and_native_release_when_available(monkeypatch, release):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return "9.3.1|" + (release or "1") + "|x86_64" if args[0] == "rpm" else ""

    monkeypatch.setattr(alma_apps, "run_command", run)
    elastic = {"version": "9.3.1"}
    if release is not None:
        elastic["rpm_release"] = release
    alma_apps.install_kibana_rpm(elastic)
    install, options = calls[0]
    assert install[-1] == "kibana-9.3.1" + ("-" + release + ".x86_64" if release else "")
    assert install[-2] == "--"
    assert options["timeout"] == 900
    assert calls[1][0][-1] == "kibana"


@pytest.mark.parametrize("elastic", [{}, {"version": "9.3.1;touch /tmp/owned"}, {"version": "9.3.1", "rpm_release": "1;bad"}, {"version": "9.3.1", "rpm_release": []}])
def test_kibana_invalid_package_identifiers_fail_before_dnf(monkeypatch, elastic):
    calls = []
    monkeypatch.setattr(alma_apps, "run_command", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(alma_apps.AlmaInstallError):
        alma_apps.install_kibana_rpm(elastic)
    assert calls == []


@pytest.mark.parametrize("installed", ["9.3.2|1|x86_64", "9.3.1|2|x86_64"])
def test_kibana_mismatched_installed_build_is_fatal(monkeypatch, installed):
    monkeypatch.setattr(alma_apps, "run_command", lambda args, **kwargs: installed if args[0] == "rpm" else "")
    with pytest.raises(alma_apps.AlmaInstallError, match="does not match"):
        alma_apps.install_kibana_rpm({"version": "9.3.1", "rpm_release": "1"})


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("dnf", 900), FileNotFoundError("dnf")])
def test_guest_command_failures_are_bounded_and_do_not_disclose_arguments(monkeypatch, error):
    def fail(*args, **kwargs):
        assert kwargs["timeout"] == 900
        raise error

    monkeypatch.setattr(alma_apps.subprocess, "run", fail)
    with pytest.raises(alma_apps.AlmaInstallError) as caught:
        alma_apps.run_command(["dnf", "private-token"], timeout=900)
    assert "private-token" not in str(caught.value)


def test_exit_failures_discard_package_output(monkeypatch):
    monkeypatch.setattr(alma_apps.subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "secret material", "private-token"))
    with pytest.raises(alma_apps.AlmaInstallError, match="exit 1") as caught:
        alma_apps.run_command(["dnf", "install"])
    assert "private-token" not in str(caught.value)
    assert "secret material" not in str(caught.value)
