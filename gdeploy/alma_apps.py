"""RPM provisioning for AlmaLinux 10 application guests.

The Ubuntu scripts remain the source of the TLS, credential and application
health-check behavior. Only their package-management and guest-policy steps
are replaced here. Payloads still arrive through the protected SFTP channel;
no passwords or service tokens are interpolated into shell commands.

These installation paths provide GDeploy lab compatibility, not a claim of
vendor certification for a particular Splunk or Elastic release on AlmaLinux.
"""

from __future__ import annotations

import inspect
import ipaddress
import json
import re
import subprocess


class AlmaInstallError(RuntimeError):
    """A bounded, credential-free failure configuring an AlmaLinux guest."""


def run_command(args: list[str], *, timeout: int = 60) -> str:
    """Never include unrestricted package-manager output in a failure."""
    try:
        result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AlmaInstallError(f"AlmaLinux guest command timed out: {args[0]}.") from None
    except OSError:
        raise AlmaInstallError(f"AlmaLinux guest command is unavailable: {args[0]}.") from None
    if result.returncode:
        raise AlmaInstallError(f"AlmaLinux guest command failed: {args[0]} (exit {result.returncode}).")
    return result.stdout.strip()


def configure_firewall(ip: str, ports: tuple[int, ...]) -> None:
    """Open the application's management ports without losing SSH access."""
    try:
        address = str(ipaddress.IPv4Address(ip))
    except (ValueError, TypeError):
        raise AlmaInstallError("AlmaLinux firewall configuration requires a valid management IPv4 address.") from None
    if any(not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535 for port in ports):
        raise AlmaInstallError("AlmaLinux firewall configuration received an invalid TCP port.")
    try:
        adapters = json.loads(run_command(["ip", "-j", "-4", "address", "show"]))
    except json.JSONDecodeError:
        raise AlmaInstallError("AlmaLinux firewall discovery returned invalid network information.") from None
    if not isinstance(adapters, list):
        raise AlmaInstallError("AlmaLinux firewall discovery returned invalid network information.")
    interfaces = [
        adapter["ifname"] for adapter in adapters
        if isinstance(adapter, dict) and isinstance(adapter.get("ifname"), str)
        and isinstance(adapter.get("addr_info"), list)
        and any(isinstance(entry, dict) and entry.get("local") == address for entry in adapter["addr_info"])
    ]
    if len(interfaces) != 1:
        raise AlmaInstallError("AlmaLinux firewall discovery could not identify one management interface.")
    interface = interfaces[0]
    state = run_command(["systemctl", "show", "firewalld.service", "-p", "ActiveState", "--value"])
    if state not in ("active", "inactive"):
        raise AlmaInstallError("AlmaLinux firewall service is not ready for safe configuration.")
    if state == "inactive":
        # Add SSH before starting a default firewall on a fresh minimal guest.
        run_command(["firewall-offline-cmd", "--add-service=ssh"])
    run_command(["systemctl", "enable", "--now", "firewalld"])
    zone = run_command(["firewall-cmd", "--get-default-zone"])
    active_zones = run_command(["firewall-cmd", "--get-active-zones"])
    current_zone = None
    for line in active_zones.splitlines():
        if line and not line[0].isspace():
            current_zone = line.strip()
        elif line.strip().startswith("interfaces:") and interface in line.partition(":")[2].split():
            zone = current_zone
    if not isinstance(zone, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", zone):
        raise AlmaInstallError("AlmaLinux firewall discovery returned an invalid management zone.")
    for permanent in (True, False):
        prefix = ["firewall-cmd", f"--zone={zone}"] + (["--permanent"] if permanent else [])
        run_command(prefix + ["--add-service=ssh"])
        for port in ports:
            run_command(prefix + [f"--add-port={port}/tcp"])
    run_command(["firewall-cmd", f"--zone={zone}", "--query-service=ssh"])
    for port in ports:
        run_command(["firewall-cmd", f"--zone={zone}", f"--query-port={port}/tcp"])


def restore_selinux(paths: tuple[str, ...], *, splunk: bool = False) -> None:
    """Use normal service file labels; never make SELinux permissive."""
    mode = run_command(["getenforce"])
    if mode not in ("Enforcing", "Permissive", "Disabled"):
        raise AlmaInstallError("AlmaLinux SELinux discovery returned an unsupported state.")
    if mode == "Disabled":
        return
    if splunk:
        expression = "/opt/splunk/bin/splunk(d)?"
        existing = run_command(["semanage", "fcontext", "-l", "-C"])
        operation = "-m" if any(line.split()[0] == expression for line in existing.splitlines() if line.split()) else "-a"
        run_command(["semanage", "fcontext", operation, "-t", "bin_t", expression])
    run_command(["restorecon", "-RF", *paths], timeout=180)


def rpm_package(name: str) -> dict[str, str]:
    if name not in ("elasticsearch", "kibana"):
        raise AlmaInstallError("Unsupported Elastic RPM package.")
    installed = run_command(["rpm", "-q", "--qf", "%{VERSION}|%{RELEASE}|%{ARCH}", name]).split("|")
    if (
        len(installed) != 3 or not re.fullmatch(r"9\.\d+\.\d+", installed[0])
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_]*", installed[1]) or installed[2] != "x86_64"
    ):
        raise AlmaInstallError("Unexpected Elastic RPM package version or architecture.")
    return {"version": installed[0], "rpm_release": installed[1]}


def install_kibana_rpm(elastic: dict) -> None:
    version = elastic.get("version") if isinstance(elastic, dict) else None
    if not isinstance(version, str) or not re.fullmatch(r"9\.\d+\.\d+", version):
        raise AlmaInstallError("AlmaLinux Kibana requires a verified Elasticsearch 9.x version.")
    release = elastic.get("rpm_release")
    if release is not None and (not isinstance(release, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_]*", release)):
        raise AlmaInstallError("AlmaLinux Kibana received an invalid Elasticsearch RPM release.")
    package = "kibana-" + version + ("-" + release + ".x86_64" if release is not None else "")
    run_command([
        "dnf", "-q", "--setopt=timeout=60", "--setopt=retries=3", "install", "-y", "--", package,
    ], timeout=900)
    installed = rpm_package("kibana")
    if installed["version"] != version or (release is not None and installed["rpm_release"] != release):
        raise AlmaInstallError("Kibana RPM does not match the verified Elasticsearch package.")


_ALMA_HEADER = r"""#!/bin/bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "$0")"
trap 'echo "GDeploy AlmaLinux provisioning failed at script line $LINENO" >&2' ERR
. /etc/os-release
test "${ID:-}" = almalinux
test "${VERSION_ID%%.*}" = 10
test "$(uname -m)" = x86_64
"""

_DNF_DEPENDENCIES = r"""
timeout --kill-after=15s 600s dnf -q --setopt=timeout=60 --setopt=retries=3 install -y ca-certificates curl gnupg2 openssl python3 firewalld policycoreutils-python-utils
"""

_ELASTIC_RPM_REPO = _DNF_DEPENDENCIES + r"""
curl --fail --silent --show-error --retry 3 --connect-timeout 20 --max-time 120 --proto '=https' --tlsv1.2 https://artifacts.elastic.co/GPG-KEY-elasticsearch -o elastic.asc
fingerprint=$(gpg --batch --show-keys --with-colons elastic.asc | awk -F: '$1=="fpr" { print $10; exit }')
test "$fingerprint" = "46095ACC8548582C1A2699A9D27D666CD88E42B4"
install -m 644 elastic.asc /etc/pki/rpm-gpg/GPG-KEY-elasticsearch
rpm --import /etc/pki/rpm-gpg/GPG-KEY-elasticsearch
cat > /etc/yum.repos.d/gdeploy-elastic.repo <<'REPO'
[gdeploy-elastic]
name=Elastic 9.x signed RPM packages
baseurl=https://artifacts.elastic.co/packages/9.x/yum
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/GPG-KEY-elasticsearch
autorefresh=1
type=rpm-md
REPO
chmod 644 /etc/yum.repos.d/gdeploy-elastic.repo
"""


def _python_helpers(*functions) -> str:
    return (
        "import ipaddress, json, re, subprocess\n"
        + inspect.getsource(AlmaInstallError) + "\n"
        + inspect.getsource(run_command) + "\n"
        + "\n".join(inspect.getsource(function) for function in functions)
    )


def _policy_steps(role: str) -> str:
    ports = {"elasticsearch": (9200,), "kibana": (5601,), "splunk": (8000, 8089)}[role]
    paths = {
        "elasticsearch": ("/etc/elasticsearch", "/var/lib/elasticsearch", "/var/log/elasticsearch"),
        "kibana": ("/etc/kibana",),
        "splunk": ("/opt/splunk",),
    }[role]
    return (
        "python3 - <<'PY'\n" + _python_helpers(configure_firewall, restore_selinux)
        + "\np = json.loads(__import__('pathlib').Path('payload.json').read_text())\n"
        + f"configure_firewall(p['ip'], {ports!r})\n"
        + f"restore_selinux({paths!r}, splunk={role == 'splunk'!r})\nPY\n"
    )


def _replace(script: str, original: str, replacement: str) -> str:
    if script.count(original) != 1:
        raise AlmaInstallError("Application script changed; AlmaLinux package provisioning needs review.")
    return script.replace(original, replacement, 1)


def application_script(role: str) -> str:
    """Return the protected existing application flow with native Alma steps."""
    from .guest import _ELASTIC_REPO, _ELASTICSEARCH_SCRIPT, _KIBANA_SCRIPT, _SCRIPT_HEADER, _SPLUNK_SCRIPT

    if role not in ("elasticsearch", "kibana", "splunk"):
        raise AlmaInstallError("Unsupported AlmaLinux application role.")
    script = {"elasticsearch": _ELASTICSEARCH_SCRIPT, "kibana": _KIBANA_SCRIPT, "splunk": _SPLUNK_SCRIPT}[role]
    script = _replace(script, _SCRIPT_HEADER, _ALMA_HEADER)
    if role in ("elasticsearch", "kibana"):
        script = _replace(script, _ELASTIC_REPO, _ELASTIC_RPM_REPO)
    if role == "elasticsearch":
        script = _replace(script, "apt-get -q install -y elasticsearch", "timeout --kill-after=15s 900s dnf -q --setopt=timeout=60 --setopt=retries=3 install -y elasticsearch.x86_64")
        script = _replace(script, "apt-mark hold elasticsearch", "# Future RPM upgrades remain an administrator action.")
        script = _replace(script, "systemctl enable --now elasticsearch", _policy_steps(role) + "systemctl enable --now elasticsearch")
        script = _replace(
            script, "version = subprocess.check_output(['dpkg-query','-W','-f=${Version}','elasticsearch'], text=True).strip()",
            _python_helpers(rpm_package) + "\ninstalled = rpm_package('elasticsearch')\nversion = installed['version']",
        )
        script = _replace(script, "'version':version}))", "'version':version,'rpm_release':installed['rpm_release']}))")
    elif role == "kibana":
        script = _replace(
            script, "subprocess.run(['apt-get','-q','install','-y','--','kibana='+p['elastic']['version']], check=True)",
            _python_helpers(rpm_package, install_kibana_rpm) + "\ninstall_kibana_rpm(p['elastic'])",
        )
        script = _replace(script, "apt-mark hold kibana", "# Future RPM upgrades remain an administrator action.")
        script = _replace(script, "systemctl enable --now kibana", _policy_steps(role) + "systemctl enable --now kibana")
    else:
        script = _replace(script, "apt-get -q update\napt-get -q install -y ca-certificates openssl python3 libnuma1", _DNF_DEPENDENCIES + "timeout --kill-after=15s 600s dnf -q --setopt=timeout=60 --setopt=retries=3 install -y numactl-libs")
        script = _replace(script, "runuser -u splunk -- /opt/splunk/bin/splunk start", _policy_steps(role) + "runuser -u splunk -- /opt/splunk/bin/splunk start")
    return script
