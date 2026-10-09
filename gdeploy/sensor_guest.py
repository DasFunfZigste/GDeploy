"""Corelight Software Sensor provisioner executed inside its dedicated Ubuntu guest.

The emitted script needs only Python's standard library. Repository downloads
share the Fleet installer implementation, including credential-safe redirects.
"""

from __future__ import annotations

import ast
import base64
import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .fleet_guest import RepositoryDownloadError, download_repository_file


ROOT = Path("/")
REPOSITORY = "https://pkgrepos.corelight.cloud/corelight/sensor-stable/"
SECRETS: set[str] = set()
MANAGEMENT = "gdeploymgmt"
MONITORING = "gdeploymon"


class SensorInstallError(Exception):
    """An actionable provisioning failure without confidential input values."""


def secret_values(snapshot):
    values = set()
    for key in ("repository_token", "community_string", "license_key", "pairing_token"):
        value = snapshot.get(key)
        if isinstance(value, str) and value:
            values.update((value, json.dumps(value)[1:-1], base64.b64encode(value.encode()).decode(), urllib.parse.quote(value, safe="")))
    token = snapshot.get("repository_token")
    if isinstance(token, str) and token:
        values.update(base64.b64encode((token + suffix).encode()).decode() for suffix in (":", ":irrelevant"))
    return values


def sanitize(text):
    for secret in sorted(SECRETS, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    lines = []
    for line in text.splitlines():
        if re.search(r"(?i)(?:password|community[-_]string|license[-_]key|pairing[-_]token|authorization|token)\s*[:=]", line):
            lines.append("[credential-bearing diagnostic omitted]")
        else:
            lines.append("".join(char for char in line if char == "\t" or ord(char) >= 32))
    return "\n".join(lines)[-12000:]


def diagnostic(text):
    clean = sanitize(text)
    if clean:
        print(clean, file=sys.stderr)


def run(command, label, *, input=None, timeout=600, private=False, binary=False, cwd=None):
    try:
        result = subprocess.run(
            command, input=input, capture_output=True, timeout=timeout, cwd=cwd,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SensorInstallError(label + "; the command was unavailable or timed out.") from None
    if result.returncode:
        if not private:
            diagnostic((result.stdout + result.stderr).decode("utf-8", "replace"))
        raise SensorInstallError(label + f" (exit {result.returncode}).")
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def _mac(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", value) or int(value[:2], 16) & 1 or value.lower() == "00:00:00:00:00:00":
        raise SensorInstallError("Software Sensor requires valid management and monitoring MAC addresses.")
    return value.lower()


def _hostname(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 253:
        raise SensorInstallError("Software Sensor requires the server SSL name from Fleet Manager.")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in value.split(".")):
        raise SensorInstallError("Software Sensor requires a valid Fleet Manager server SSL name without a scheme or port.")
    return value


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise SensorInstallError("Configure Software Sensor and provide its unique pairing token before deploying.")
    result = {}
    for key, label, limit in (
        ("repository_token", "repository token", 4096), ("community_string", "community string", 4096),
        ("license_key", "sensor license key", 65536), ("pairing_token", "unique Fleet pairing token", 4096),
    ):
        value = snapshot.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= limit or not value.isprintable():
            raise SensorInstallError(f"Software Sensor requires a valid single-line {label}.")
        if key in {"repository_token", "pairing_token"} and (not value.isascii() or any(char.isspace() for char in value)):
            raise SensorInstallError(f"Software Sensor requires an ASCII {label} without whitespace.")
        result[key] = value
    if ":" in result["repository_token"]:
        raise SensorInstallError("Software Sensor repository tokens must not contain a colon.")
    url = snapshot.get("fleet_url")
    try:
        if not isinstance(url, str) or not 1 <= len(url) <= 2048 or not url.isascii() or any(char.isspace() or ord(char) < 32 for char in url) or "\\" in url:
            raise ValueError
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError
        _hostname(parsed.hostname)
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        if parsed.port is None:
            authority = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            url = urllib.parse.urlunsplit(("https", authority + ":1443", parsed.path, "", ""))
    except (ValueError, SensorInstallError):
        raise SensorInstallError("Software Sensor requires the HTTPS Fleet Manager pairing URL, normally on port 1443.") from None
    result.update(fleet_url=url, server_sslname=_hostname(snapshot.get("server_sslname")))
    for key in ("management_mac", "monitor_mac"):
        result[key] = _mac(snapshot.get(key))
    if result["management_mac"] == result["monitor_mac"]:
        raise SensorInstallError("Software Sensor management and monitoring MAC addresses must be distinct.")
    try:
        result["api_network"] = str(ipaddress.IPv4Network(snapshot.get("api_network", "0.0.0.0/0"), strict=False))
    except (ValueError, TypeError):
        raise SensorInstallError("Choose a valid IPv4 CIDR network for Software Sensor API access on TCP 443.") from None
    return result


def host_checks():
    try:
        release = dict(line.split("=", 1) for line in (ROOT / "etc/os-release").read_text().splitlines() if "=" in line)
    except OSError:
        raise SensorInstallError("Could not identify the guest operating system for Software Sensor.") from None
    if release.get("ID", "").strip('"') != "ubuntu" or release.get("VERSION_ID", "").strip('"') != "24.04" or platform.machine() != "x86_64":
        raise SensorInstallError("Corelight Software Sensor requires a minimal Ubuntu 24.04 x86_64 guest.")
    if shutil.disk_usage(ROOT / "var").free < 500 * 1000**3:
        raise SensorInstallError("Software Sensor requires at least 500 GB free under /var; enlarge the dedicated VM disk.")
    options = run(["findmnt", "-n", "-o", "OPTIONS", "--target", "/var"], "Could not verify the Software Sensor /var mount", timeout=30)
    if "noexec" in options.strip().split(","):
        raise SensorInstallError("Software Sensor requires an executable /var filesystem; remove its noexec mount option.")
    loader = run([str(ROOT / "lib64/ld-linux-x86-64.so.2"), "--help"], "Could not verify the Software Sensor CPU instruction set", timeout=30)
    if not re.search(r"(?m)^\s*x86-64-v3\s+\(supported, searched\)\s*$", loader):
        raise SensorInstallError("Software Sensor requires an x86-64-v3 CPU exposed to the guest; check ESXi hardware and CPU compatibility settings.")


def verify_network(snapshot, ip):
    try:
        expected_ip = str(ipaddress.IPv4Address(ip))
        interfaces = json.loads(run(["ip", "-j", "address", "show"], "Could not inspect Software Sensor network interfaces", timeout=30))
        by_name = {row["ifname"]: row for row in interfaces}
        management, monitor = by_name[MANAGEMENT], by_name[MONITORING]
        if management["address"].lower() != snapshot["management_mac"] or monitor["address"].lower() != snapshot["monitor_mac"]:
            raise ValueError
        addresses = [row.get("local") for row in management.get("addr_info", []) if row.get("family") == "inet"]
        if expected_ip not in addresses or monitor.get("addr_info"):
            raise ValueError
        for family in ("-4", "-6"):
            routes = json.loads(run(["ip", "-j", family, "route", "show", "table", "all"], "Could not inspect Software Sensor routes", timeout=30))
            for route in routes:
                uses_monitor = route.get("dev") == MONITORING or any(hop.get("dev") == MONITORING for hop in route.get("nexthops", []))
                if not uses_monitor:
                    continue
                # Linux can retain its per-link IPv6 multicast route even with
                # no assigned addresses, DHCP, RA or link-local networking. It
                # is not a unicast/default route or gateway for captured traffic.
                kernel_multicast = (
                    family == "-6" and route.get("dev") == MONITORING
                    and route.get("type") == "multicast" and route.get("dst") == "ff00::/8"
                    and route.get("table") in ("local", 255) and route.get("protocol") == "kernel"
                    and not any(route.get(key) for key in ("gateway", "via", "nexthops", "encap"))
                )
                if not kernel_multicast:
                    raise ValueError
    except (ValueError, TypeError, KeyError):
        raise SensorInstallError("Software Sensor network verification failed: management must match its reserved/static IPv4 and MAC; monitoring must have its own MAC with no assigned IP addresses or routed/default networking.") from None
    run(["ip", "link", "set", "dev", MONITORING, "up"], "Could not activate the Software Sensor monitoring interface", timeout=30)


def write_file(path, contents, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".gdeploy-" + uuid.uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents.encode() if isinstance(contents, str) else contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def configure_repository(snapshot):
    run(["apt-get", "-q", "update"], "Could not update Ubuntu dependency repositories")
    install = ["apt-get", "-q", "-y", "-o", "Dpkg::Options::=--force-confold", "install", "--"]
    run(install + ["ca-certificates", "gnupg", "apt-transport-https"], "Could not install Software Sensor repository prerequisites")
    try:
        key = download_repository_file(snapshot["repository_token"], "gpgkey", repository=REPOSITORY)
    except RepositoryDownloadError as error:
        raise SensorInstallError(str(error).replace("Fleet Manager", "Software Sensor")) from None
    binary_key = run(["gpg", "--batch", "--dearmor"], "Could not read the Software Sensor signing key", input=key, binary=True, private=True, timeout=60)
    keyring = "/etc/apt/keyrings/corelight_sensor-stable-archive-keyring.gpg"
    write_file(ROOT / keyring.lstrip("/"), binary_key, 0o644)
    write_file(ROOT / "etc/apt/auth.conf.d/corelight_sensor-stable.conf", "machine pkgrepos.corelight.cloud/corelight/sensor-stable/ login " + snapshot["repository_token"] + "\npassword irrelevant\n", 0o600)
    write_file(ROOT / "etc/apt/sources.list.d/corelight_sensor-stable.list", f"deb [signed-by={keyring}] {REPOSITORY}any/ any main\n", 0o644)
    run(["apt-get", "-q", "update"], "Could not update the signed Software Sensor repository; check token entitlement and guest internet access")
    run(install + ["corelightctl", "corelight-sensor"], "Could not install corelightctl and corelight-sensor", timeout=1200)
    version = run(["dpkg-query", "-W", "-f=${Version}", "corelight-sensor"], "Could not verify the installed Software Sensor package", timeout=30).strip()
    if not re.fullmatch(r"(?:[0-9]+:)?[0-9][A-Za-z0-9.+~\-]{0,127}", version):
        raise SensorInstallError("Software Sensor did not report a valid installed package version.")
    return version


def disable_competing_firewalls():
    # Sensor owns this new, dedicated guest's firewall through RKE2. Its vendor
    # firewall automatically permits SSH; API access is configured below.
    if shutil.which("ufw"):
        run(["ufw", "disable"], "Could not disable UFW before Software Sensor manages the dedicated guest firewall", timeout=60)
    listing = run(["systemctl", "list-unit-files", "--no-legend", "--no-pager", "ufw.service", "firewalld.service"], "Could not discover competing Software Sensor firewall services", timeout=30)
    installed = {parts[0]: parts[1] for line in listing.splitlines() if len(parts := line.split()) >= 2}
    for unit in ("ufw.service", "firewalld.service"):
        if unit not in installed:
            continue
        if installed[unit] == "masked":
            run(["systemctl", "stop", unit], "Could not stop a masked competing Software Sensor firewall service", timeout=60)
        else:
            run(["systemctl", "disable", "--now", unit], "Could not stop a competing Software Sensor firewall service", timeout=60)
        active = run(["systemctl", "show", unit, "-p", "ActiveState", "--value"], "Could not verify the competing firewall stopped", timeout=30).strip()
        if active != "inactive":
            raise SensorInstallError("A competing firewall is still active on the dedicated Software Sensor guest.")


def sensor_configuration(snapshot):
    quote = json.dumps
    return (
        "sensor:\n"
        "  api:\n"
        f"    password: {quote(snapshot['community_string'])}\n"
        f"  license_key: {quote(snapshot['license_key'])}\n"
        "  management_interface:\n"
        f"    - name: {quote(MANAGEMENT)}\n"
        "  monitoring_interface:\n"
        f"    name: {quote(MONITORING)}\n"
        "  pairing:\n"
        f"    server_sslname: {quote(snapshot['server_sslname'])}\n"
        f"    token: {quote(snapshot['pairing_token'])}\n"
        f"    url: {quote(snapshot['fleet_url'])}\n"
        "  kubernetes:\n"
        "    allow_ports:\n"
        "      - protocol: tcp\n"
        "        port: 443\n"
        f"        net: {quote(snapshot['api_network'])}\n"
    )


def service_health(output):
    """Require real service evidence; a successful CLI exit alone is insufficient."""
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output)
    marker = re.search(r"(?m)^\s*Services:\s*$", text)
    if marker is None:
        return False, "Software Sensor status did not report its Services section."
    states = {}
    for line in text[marker.end():].splitlines():
        match = re.fullmatch(r"\s*([a-z][a-z0-9-]*)\s*:\s*(\S.*?)\s*", line)
        if match:
            name, state = match.groups()
            if name in states:
                return False, "Software Sensor status contained conflicting service entries."
            states[name] = state
    for name in ("sensor-core", "sensor-api", "connection-manager"):
        if states.get(name) != "Ok":
            return False, f"Software Sensor {name} is not healthy; inspect its license, Fleet pairing and service status."
    for name, state in states.items():
        if state == "Ok":
            continue
        if name == "suricata" and re.fullmatch(r"Warning\s*-\s*Suricata doesn't have any rules installed\.?", state):
            continue
        return False, "Software Sensor has a service that is not healthy; inspect corelightctl sensor status."
    return True, ""


def verify_health(*, timeout=300):
    deadline = time.monotonic() + timeout
    reason = "Software Sensor health verification did not complete."
    last = ""
    while time.monotonic() < deadline:
        try:
            last = run(["corelightctl", "sensor", "status"], "Could not read Software Sensor service status", timeout=min(30, max(1, int(deadline - time.monotonic()))), cwd=ROOT / "etc/corelight")
            healthy, reason = service_health(last)
            if healthy:
                if "Suricata doesn't have any rules installed" in last:
                    diagnostic("Software Sensor is healthy. Suricata has no rules installed; configure its rules in Fleet Manager.")
                return
        except SensorInstallError as error:
            reason = str(error)
        time.sleep(min(10, max(0, deadline - time.monotonic())))
    diagnostic(last)
    raise SensorInstallError(reason + " Check sudo corelightctl sensor status and the Fleet Manager sensor record.")


def install(payload):
    raw = payload.get("corelight_sensor") if isinstance(payload, dict) else None
    if isinstance(raw, dict):
        SECRETS.update(secret_values(raw))
    snapshot = validate_snapshot(raw)
    host_checks()
    verify_network(snapshot, payload.get("ip"))
    version = configure_repository(snapshot)
    disable_competing_firewalls()
    run(["corelightctl", "sensor", "prepare"], "Could not prepare the dedicated Software Sensor guest", timeout=600)
    directory = ROOT / "etc/corelight"
    directory.mkdir(parents=True, exist_ok=True)
    run(["corelightctl", "init"], "Could not initialize Software Sensor configuration", cwd=directory, timeout=60)
    write_file(directory / "corelightctl.yaml", sensor_configuration(snapshot), 0o600)
    run(["corelightctl", "sensor", "deploy", "-v"], "Could not deploy Software Sensor; check its license, pairing, resources and internet access", cwd=directory, timeout=1200)
    verify_health()
    verify_network(snapshot, payload.get("ip"))
    return {"healthy": True, "version": version}


def main():
    try:
        payload = json.loads(Path("payload.json").read_text())
        result = install(payload)
    except SensorInstallError as error:
        diagnostic(str(error))
        return 1
    except Exception:
        diagnostic("Software Sensor provisioning failed while preparing the dedicated guest; inspect its package, configuration and disk permissions.")
        return 1
    print("GDEPLOY_RESULT=" + json.dumps(result))
    return 0


def installer_script():
    # Embed the shared downloader, not a second implementation. Extracting only
    # these definitions keeps the emitted guest script independent of GDeploy.
    from . import fleet_guest

    source = Path(fleet_guest.__file__).read_text()
    names = {"FleetInstallError", "RepositoryDownloadError", "NoKeyRedirect", "_repository_url_origin", "download_repository_file"}
    shared = "import http.client\nimport ssl\nREPOSITORY = 'https://pkgrepos.corelight.cloud/corelight/fleet-stable/'\n" + "\n\n".join(
        ast.get_source_segment(source, node) for node in ast.parse(source).body if getattr(node, "name", None) in names
    )
    source = Path(__file__).read_text().replace("from .fleet_guest import RepositoryDownloadError, download_repository_file", shared, 1)
    return '#!/bin/bash\nset -euo pipefail\ncd -- "$(dirname -- "$0")"\n' + "python3 - <<'GDEPLOY_SENSOR_PY'\n" + source + "\nGDEPLOY_SENSOR_PY\n"


if __name__ == "__main__":
    raise SystemExit(main())
