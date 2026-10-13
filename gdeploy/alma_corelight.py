"""Experimental AlmaLinux 10 Corelight guest installers.

Corelight 29.2 documents AlmaLinux 8/9, not AlmaLinux 10. The sensor deliberately
uses the documented EL9 and architecture-independent RPM repositories. Reusing
the existing guest installers preserves their license, pairing and health checks.
"""

from __future__ import annotations

import ast
import inspect
import os
import platform
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import fleet_guest, sensor_guest
from .alma_apps import AlmaInstallError, configure_firewall as _configure_alma_firewall, run_command
from .fleet_guest import (
    ROOT,
    SECRETS,
    FleetInstallError,
    NoKeyRedirect,
    RepositoryDownloadError,
    _repository_url_origin,
    download_repository_file,
    run,
    write_file,
)
from .fleet_guest import configure_fleet as _original_configure_fleet
from .fleet_guest import install as _original_install

InstallerError = FleetInstallError
RPM_VERSION_PATTERN = r"(?:[0-9]+:)?[0-9][A-Za-z0-9.+_~^]*-[A-Za-z0-9][A-Za-z0-9.+_~^]*\.(?:x86_64|noarch)"
PUBLIC_SIGNING_KEY = "https://downloads.corelight.cloud/public/signing/corelight-package-signing-key.asc"
ALMA_MANAGEMENT_IP = None


def requested_rpm_version(snapshot):
    """Return an exact RPM epoch:version-release.arch, never an APT selection."""
    value = snapshot.get("online_version_rpm", snapshot.get("online_version", ""))
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 128 or (value and not re.fullmatch(RPM_VERSION_PATTERN, value)):
        raise InstallerError("Choose Latest or an exact Fleet Manager RPM version, including its release and architecture.")
    return value if not value or ":" in value else "0:" + value


def _alma_host_checks(role):
    try:
        release = dict(line.split("=", 1) for line in (ROOT / "etc/os-release").read_text().splitlines() if "=" in line)
        memory = int(next(line.split()[1] for line in (ROOT / "proc/meminfo").read_text().splitlines() if line.startswith("MemTotal:"))) * 1024
    except (OSError, ValueError, StopIteration):
        raise InstallerError("Could not identify AlmaLinux guest operating system and memory before Corelight installation.") from None
    if release.get("ID", "").strip('"') != "almalinux" or release.get("VERSION_ID", "").strip('"').split(".")[0] != "10" or platform.machine() != "x86_64":
        raise InstallerError("Experimental Corelight installation requires an AlmaLinux 10 x86_64 guest.")
    required_memory = 16 if role == "sensor" else 8
    required_cpus = 4 if role == "sensor" else 2
    # Linux excludes reserved firmware memory from MemTotal; permit that normal
    # overhead without accepting genuinely undersized VMs.
    if (os.cpu_count() or 0) < required_cpus or memory < required_memory * 1024**3 * 0.9:
        raise InstallerError(f"Corelight requires at least {required_cpus} CPU cores and {required_memory} GiB assigned RAM.")
    if role == "sensor":
        if shutil.disk_usage(ROOT / "var").free < 500 * 1000**3:
            raise InstallerError("Software Sensor requires at least 500 GB free under /var; enlarge the dedicated VM disk.")
        options = run(["findmnt", "-n", "-o", "OPTIONS", "--target", "/var"], "Could not verify the Software Sensor /var mount", timeout=30)
        if "noexec" in options.strip().split(","):
            raise InstallerError("Software Sensor requires an executable /var filesystem; remove its noexec mount option.")
        loader = run([str(ROOT / "lib64/ld-linux-x86-64.so.2"), "--help"], "Could not verify the Software Sensor CPU instruction set", timeout=30)
        if not re.search(r"(?m)^\s*x86-64-v3\s+\(supported, searched\)\s*$", loader):
            raise InstallerError("Software Sensor requires an x86-64-v3 CPU exposed to the AlmaLinux guest.")
    else:
        for directory, minimum in (("var", 30), ("tmp", 20)):
            if shutil.disk_usage(ROOT / directory).free < minimum * 1024**3:
                raise InstallerError(f"Fleet Manager requires at least {minimum} GiB available under /{directory}; enlarge the VM disk.")


def _alma_fleet_host_checks():
    _alma_host_checks("fleet")


def _alma_sensor_host_checks():
    _alma_host_checks("sensor")


def _download_public_signing_key():
    """Fetch the fixed public package key without any customer authentication."""
    current = PUBLIC_SIGNING_KEY
    deadline = time.monotonic() + 60
    opener = urllib.request.build_opener(NoKeyRedirect(), urllib.request.HTTPSHandler())
    for hop in range(6):
        _public_key_origin(current)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InstallerError("Corelight public package signing-key download timed out.")
        try:
            with opener.open(urllib.request.Request(current), timeout=remaining) as response:
                if response.status != 200:
                    raise InstallerError("Corelight public signing-key server returned an unexpected response.")
                pieces, size = [], 0
                while True:
                    if time.monotonic() >= deadline:
                        raise InstallerError("Corelight public package signing-key download timed out.")
                    piece = response.read1(min(65536, 1024**2 + 1 - size))
                    if not piece:
                        break
                    pieces.append(piece)
                    size += len(piece)
                    if size > 1024**2:
                        raise InstallerError("Corelight public signing key exceeds the allowed download size.")
                data = b"".join(pieces)
                if not data:
                    raise InstallerError("Corelight public signing key is empty.")
                return data
        except urllib.error.HTTPError as error:
            try:
                if error.code in {301, 302, 303, 307, 308} and hop < 5:
                    location = error.headers.get("Location") if error.headers is not None else None
                    if not isinstance(location, str) or not location or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in location):
                        raise InstallerError("Corelight public signing key returned an invalid HTTPS redirect.")
                    target = urllib.parse.urljoin(current, location)
                    _public_key_origin(target)
                    current = target
                    continue
                raise InstallerError(f"Could not download Corelight public signing key (HTTP {error.code}).") from None
            finally:
                error.close()
        except RepositoryDownloadError:
            raise InstallerError("Corelight public signing-key downloads require HTTPS without embedded credentials.") from None
        except (OSError, ValueError, urllib.error.URLError):
            raise InstallerError("Could not download Corelight public package signing key over verified TLS.") from None
    raise InstallerError("Corelight public signing-key download exceeded its redirect limit.")


def _public_key_origin(url):
    try:
        return _repository_url_origin(url, "package-signing key")
    except RepositoryDownloadError:
        raise InstallerError("Corelight public signing-key downloads require HTTPS without embedded credentials.") from None


def _alma_repository(snapshot, role):
    product = "fleet" if role == "fleet" else "sensor"
    repository = f"https://pkgrepos.corelight.cloud/corelight/{product}-stable/"
    token = snapshot.get("repository_token")
    # Validate before writing dnf config so INI controls/interpolation cannot be
    # supplied by a token. The shared downloader accepts the same token scope.
    if not isinstance(token, str) or not token or len(token) > 4096 or not token.isascii() or not token.isprintable() or any(char.isspace() or char in ":%" for char in token):
        raise InstallerError("Enter a valid customer repository token for Corelight RPM installation.")
    SECRETS.update((token, urllib.parse.quote(token, safe="")))
    run(["dnf", "-q", "-y", "install", "--", "ca-certificates", "gnupg2", "dnf-plugins-core", "policycoreutils"] + (["firewalld"] if role == "fleet" else []), "Could not install Corelight RPM repository prerequisites", timeout=1200)
    try:
        key = download_repository_file(token, "gpgkey", repository=repository)
        package_key = _download_public_signing_key()
    except RepositoryDownloadError as error:
        raise InstallerError(str(error).replace("Fleet Manager", "Corelight")) from None
    # Let gpg parse both keys before trusting either; rpm then imports the package
    # signing key. Metadata trust uses the separately downloaded repository key.
    for filename, contents in ((f"corelight-{product}-repository.asc", key), ("corelight-package-signing-key.asc", package_key)):
        run(["gpg", "--batch", "--dearmor"], "Could not parse Corelight RPM signing key", input=contents, binary=True, private=True, timeout=60)
        write_file(ROOT / "etc/pki/rpm-gpg" / filename, contents, 0o644)
        run(["rpm", "--import", str(ROOT / "etc/pki/rpm-gpg" / filename)], "Could not import Corelight RPM signing key", timeout=60)
    sections = [(f"corelight_{product}-stable_any", "rpm_any/rpm_any/$basearch")]
    if role == "sensor":
        # Explicit experiment requested by the administrator: Corelight 29.2
        # documents EL9, not EL10. Do not invent an EL10 endpoint or convert DEBs.
        sections.insert(0, ("corelight_sensor-stable_el_9", "el/9/$basearch"))
    config = "# Experimental AlmaLinux 10; Corelight 29.2 documents AlmaLinux 8/9.\n"
    for identity, path in sections:
        config += (
            f"[{identity}]\nname={identity}\nbaseurl={repository}{path}\n"
            # Match the vendor's flags: signed repository metadata validates
            # package checksums; the guide disables per-package RPM signatures.
            f"username={token}\npassword=\nrepo_gpgcheck=1\ngpgcheck=0\nenabled=1\n"
            f"gpgkey=file:///etc/pki/rpm-gpg/corelight-{product}-repository.asc file:///etc/pki/rpm-gpg/corelight-package-signing-key.asc\n"
            "sslverify=1\nsslcacert=/etc/pki/tls/certs/ca-bundle.crt\nmetadata_expire=300\n"
        )
    write_file(ROOT / f"etc/yum.repos.d/corelight_{product}-stable.repo", config, 0o600)
    run(["dnf", "-q", "-y", "makecache", "--disablerepo=*", f"--enablerepo=corelight_{product}-stable*"], "Could not verify signed Corelight RPM repository metadata; check token entitlement and AlmaLinux 10 compatibility", timeout=600)


def _alma_installed_version(package, requested=""):
    output = run(["rpm", "-q", "--qf", "%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\\n", package], "Could not verify installed Corelight RPM version", timeout=30).strip()
    if not re.fullmatch(RPM_VERSION_PATTERN, output) or (requested and requested != output):
        raise InstallerError("The installed Corelight RPM does not match the selected exact package version; inspect AlmaLinux 10 compatibility and the repository.")
    return output


def _alma_installed_fleet_version(requested=""):
    return _alma_installed_version("corelight-fleet", requested)


def _alma_configure_fleet_repository(snapshot):
    requested = requested_rpm_version(snapshot)
    _alma_repository(snapshot, "fleet")
    if requested:
        available = run(["dnf", "-q", "repoquery", "--show-duplicates", "--disablerepo=*", "--enablerepo=corelight_fleet-stable_any", "--qf", "%{epoch}:%{version}-%{release}.%{arch}", "corelight-fleet"], "Could not list Fleet Manager RPM versions", timeout=120)
        if requested not in {line.strip() for line in available.splitlines()}:
            raise InstallerError("The selected exact Fleet Manager RPM version is unavailable; choose a listed RPM version or Latest.")
    package = "corelight-fleet" + ("-" + requested if requested else "")
    run(["dnf", "-q", "-y", "install", "--", package], "Could not install Fleet Manager RPM on experimental AlmaLinux 10; check dependencies and vendor compatibility", timeout=1200)
    return _alma_installed_fleet_version(requested)


def _alma_configure_sensor_repository(snapshot):
    _alma_repository(snapshot, "sensor")
    run(["dnf", "-q", "-y", "install", "--", "corelightctl", "corelight-sensor"], "Could not install Software Sensor EL9 RPMs on experimental AlmaLinux 10; check dependencies and vendor compatibility", timeout=1200)
    return _alma_installed_version("corelight-sensor")


@contextmanager
def _alma_suppress_fleet_start():
    """Mask only Fleet startup while installing; preserve prior unit files."""
    path = ROOT / "etc/systemd/system/corelight-fleetd.service"
    backup = path.with_name(".gdeploy-fleet-service-" + uuid.uuid4().hex)
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = os.path.lexists(path)
    if saved:
        os.replace(path, backup)
    os.symlink("/dev/null", path)
    try:
        run(["systemctl", "daemon-reload"], "Could not prevent Fleet Manager startup before its license exists", timeout=30)
        yield
    finally:
        # Some vendor RPMs put their unit in /etc rather than /usr/lib and can
        # replace the mask during unpack. Retain that newly installed unit; only
        # remove our own mask. A previous administrator unit is restored below.
        if path.is_symlink() and os.readlink(path) == "/dev/null":
            path.unlink()
        if saved:
            path.unlink(missing_ok=True)
            os.replace(backup, path)
        run(["systemctl", "daemon-reload"], "Could not restore Fleet Manager unit after RPM installation", timeout=30)


def _alma_fleet_firewall():
    """Use the same verified management-zone/SSH policy as other Alma roles."""
    try:
        _configure_alma_firewall(ALMA_MANAGEMENT_IP, (443, 1443))
    except AlmaInstallError as error:
        raise InstallerError(str(error)) from None


def _alma_install(payload):
    global ALMA_MANAGEMENT_IP
    ALMA_MANAGEMENT_IP = payload.get("ip") if isinstance(payload, dict) else None
    return _original_install(payload)


def _alma_configure_fleet(snapshot):
    _original_configure_fleet(snapshot)
    run(["restorecon", "-F", "/etc/corelight-fleetd.pem", "/etc/corelight-fleetd.conf"], "Could not apply Fleet Manager SELinux file labels", timeout=60)


def _runtime_source():
    source = Path(__file__).read_text()
    excluded = {"_runtime_source", "_script", "fleet_installer_script", "sensor_installer_script"}
    body = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in excluded:
            segment = ast.get_source_segment(source, node)
            if node.decorator_list:
                segment = "@contextmanager\n" + segment
            body.append(segment)
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in {"RPM_VERSION_PATTERN", "PUBLIC_SIGNING_KEY"} for target in node.targets):
            body.append(ast.get_source_segment(source, node))
    policy = (
        inspect.getsource(AlmaInstallError) + "\n" + inspect.getsource(run_command) + "\n"
        + inspect.getsource(_configure_alma_firewall).replace("def configure_firewall(", "def _configure_alma_firewall(", 1)
    )
    return "from contextlib import contextmanager\n" + policy + "\n" + "\n\n".join(body)


def _script(role):
    existing = fleet_guest.installer_script() if role == "fleet" else sensor_guest.installer_script()
    marker = "GDEPLOY_FLEET_PY" if role == "fleet" else "GDEPLOY_SENSOR_PY"
    source = existing.split(f"python3 - <<'{marker}'\n", 1)[1].rsplit(f"\n{marker}", 1)[0]
    source = source.rsplit('if __name__ == "__main__":', 1)[0]
    overrides = (
        "InstallerError = FleetInstallError\nhost_checks = _alma_fleet_host_checks\n"
        "requested_online_version = requested_rpm_version\nconfigure_online_repository = _alma_configure_fleet_repository\n"
        "installed_package_version = _alma_installed_fleet_version\nsuppress_package_start = _alma_suppress_fleet_start\nconfigure_firewall = _alma_fleet_firewall\n"
        "_original_configure_fleet = configure_fleet\nconfigure_fleet = _alma_configure_fleet\n"
        "ALMA_MANAGEMENT_IP = None\n_original_install = install\ninstall = _alma_install\n"
        if role == "fleet" else
        "InstallerError = SensorInstallError\nhost_checks = _alma_sensor_host_checks\nconfigure_repository = _alma_configure_sensor_repository\n"
    )
    source += "\n" + _runtime_source() + "\n" + overrides + '\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
    return '#!/bin/bash\nset -euo pipefail\ncd -- "$(dirname -- "$0")"\n' + f"python3 - <<'{marker}'\n" + source + f"\n{marker}\n"


def fleet_installer_script():
    return _script("fleet")


def sensor_installer_script():
    return _script("sensor")
