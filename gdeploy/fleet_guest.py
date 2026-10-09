"""Corelight Fleet Manager installer, also executed verbatim inside the Ubuntu guest.

Keep this module on the standard library: the guest need not have GDeploy installed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import platform
import pwd
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path


ROOT = Path("/")
REPOSITORY = "https://pkgrepos.corelight.cloud/corelight/fleet-stable/"
SECRETS: set[str] = set()


class FleetInstallError(Exception):
    """A safe, actionable installer error, containing no raw command arguments."""


class RepositoryDownloadError(FleetInstallError):
    """A redacted download failure with an optional HTTP status for safe fallback."""

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def secret_values(snapshot):
    result = set()
    values = [snapshot.get(key) for key in ("community_string", "repository_token", "license_pem")]
    for value in values:
        if isinstance(value, str) and value:
            result.add(value)
            result.add(json.dumps(value)[1:-1])
            result.add(base64.b64encode(value.encode()).decode())
    for line in (snapshot.get("license_pem") or "").splitlines():
        if line and not line.startswith("-----"):
            result.add(line)
    token = snapshot.get("repository_token")
    if token:
        for suffix in (":", ":irrelevant"):
            result.add(base64.b64encode((token + suffix).encode()).decode())
    return result


def register_secrets(snapshot):
    SECRETS.update(secret_values(snapshot))


def sanitize(text):
    text = re.sub(r"-----BEGIN [^-]+-----.*?(?:-----END [^-]+-----|\Z)", "[PEM omitted]", text, flags=re.S)
    for value in sorted(SECRETS, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    # Journals may repeat configuration values in forms different from the input.
    lines = []
    for line in text.splitlines():
        if re.search(r"(?i)(?:password|community[-_]string|authorization|private[-_ ]key)\s*[:=]", line):
            lines.append("[credential-bearing diagnostic omitted]")
        else:
            lines.append(line)
    return "\n".join(lines)[-12000:]


def _diagnostic(text):
    cleaned = sanitize(text)
    if cleaned:
        print(cleaned, file=sys.stderr)


def run(command, label, *, input=None, timeout=1200, private=False, binary=False):
    try:
        result = subprocess.run(
            command, input=input, capture_output=True, timeout=timeout,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise FleetInstallError(label + "; the command was unavailable or timed out.") from None
    if result.returncode:
        if not private:
            _diagnostic((result.stdout + result.stderr).decode("utf-8", "replace"))
        raise FleetInstallError(label + f" (exit {result.returncode}).")
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def requested_online_version(snapshot):
    """Accept legacy snapshots without a selection; reject APT expressions."""
    version = snapshot.get("online_version")
    if version is None:
        return ""
    if not isinstance(version, str) or len(version) > 128 or (
        version and not re.fullmatch(r"(?:[0-9]+:)?[0-9][A-Za-z0-9.+~\-]*", version)
    ):
        raise FleetInstallError("Choose Latest or an exact Fleet Manager Debian package version, including any epoch or revision.")
    return version


def verify_transfers(payload):
    """Validate all snapshotted bytes before executing any package-manager command."""
    snapshot = payload["fleetmanager"]
    register_secrets(snapshot)
    if snapshot.get("mode") == "offline":
        raise FleetInstallError("OFFLINE Fleet Manager installation is no longer supported. Configure repository access in Setup and create a new deployment.")
    if snapshot.get("mode") != "online":
        raise FleetInstallError("Configure Fleet Manager repository installation in Setup.")
    requested_online_version(snapshot)
    if payload.get("fleet_files"):
        raise FleetInstallError("Fleet Manager installation uses its repository; uploaded installer and dependency packages are no longer supported.")
    license_pem = snapshot.get("license_pem")
    license_sha256 = snapshot.get("license_sha256")
    if (
        not isinstance(license_pem, str)
        or not isinstance(license_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", license_sha256)
        or not hmac.compare_digest(hashlib.sha256(license_pem.encode()).hexdigest(), license_sha256)
    ):
        raise FleetInstallError("Fleet Manager license changed during transfer; select its product identity PEM again in Setup.")
    community = snapshot.get("community_string")
    if not isinstance(community, str) or not community or any(c in community for c in "\r\n\0\"'"):
        raise FleetInstallError("Fleet Manager requires a community string without quotes or line breaks.")
    certs = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", license_pem, re.S)
    if not certs:
        raise FleetInstallError("Fleet Manager requires its vendor product identity PEM, including its certificate.")
    try:
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certs[0])).hexdigest()
    except (ValueError, TypeError):
        raise FleetInstallError("Fleet Manager product identity certificate is invalid; select the vendor PEM in Setup.") from None
    return fingerprint


def host_checks():
    release = {}
    try:
        for line in (ROOT / "etc/os-release").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                release[key] = value.strip('"')
    except OSError:
        raise FleetInstallError("Could not identify the guest OS before installing Fleet Manager.") from None
    if release.get("ID") != "ubuntu" or release.get("VERSION_ID") not in {"22.04", "24.04"} or platform.machine() != "x86_64":
        raise FleetInstallError("Fleet Manager requires an Ubuntu 22.04 or 24.04 x86_64 guest.")
    for directory, minimum in (("var", 30), ("tmp", 20)):
        if shutil.disk_usage(ROOT / directory).free < minimum * 1024**3:
            raise FleetInstallError(f"Fleet Manager requires at least {minimum} GiB available under /{directory}; enlarge the VM disk.")


def write_file(path, contents, mode, *, owner=None):
    """Replace configuration atomically with final permissions set before publication."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".gdeploy-" + uuid.uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents.encode() if isinstance(contents, str) else contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if owner is not None:
            os.chown(temporary, owner.pw_uid, owner.pw_gid)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def suppress_package_start():
    """Prevent postinst startup until the vendor license and configuration exist."""
    policy = ROOT / "usr/sbin/policy-rc.d"
    backup = policy.with_name(".gdeploy-policy-" + uuid.uuid4().hex)
    policy.parent.mkdir(parents=True, exist_ok=True)
    saved = os.path.lexists(policy)
    if saved:
        os.replace(policy, backup)
    try:
        write_file(policy, "#!/bin/sh\nexit 101\n", 0o755)
        yield
    finally:
        policy.unlink(missing_ok=True)
        if saved:
            os.replace(backup, policy)


APT_INSTALL = ["apt-get", "-q", "-y", "-o", "Dpkg::Options::=--force-confold", "install"]


class NoKeyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        # Surface the original response for the manual, credential-aware loop.
        return None


def _repository_url_origin(url, label):
    """Validate a repository URL without including it in an error message."""
    if not isinstance(url, str) or not url or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise RepositoryDownloadError(f"The Fleet Manager {label} repository returned an invalid redirect address.")
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port if parsed.port is not None else 443
        if parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None or not 1 <= port <= 65535:
            raise ValueError
        if ":" in host or re.fullmatch(r"[0-9.]+", host):
            ipaddress.ip_address(host)
        elif len(host) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.rstrip(".").split(".")
        ):
            raise ValueError
    except (ValueError, UnicodeError):
        raise RepositoryDownloadError(
            f"Fleet Manager {label} downloads require a valid HTTPS address without embedded credentials."
        ) from None
    return parsed.scheme, host, port


def download_repository_file(token, relative_path, *, max_bytes=1024 * 1024, timeout=60, resource="signing key"):
    """Fetch a fixed-repository resource with bounded, credential-safe redirects.

    Initial paths cannot choose a host, escape the repository, or contain a query.
    HTTPS redirects chosen by the repository may contain signed query strings.
    """
    if not isinstance(resource, str) or resource not in {"signing key", "package metadata"}:
        raise RepositoryDownloadError("Choose a supported Fleet Manager repository resource.")
    if (
        not isinstance(relative_path, str) or len(relative_path) > 1024
        or not re.fullmatch(r"[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*", relative_path)
        or any(part in {".", ".."} for part in relative_path.split("/"))
    ):
        raise RepositoryDownloadError("Choose a file within the configured Fleet Manager repository.")
    if (
        not isinstance(token, str) or not token or len(token) > 4096 or not token.isascii()
        or not token.isprintable() or any(char.isspace() or char == ":" for char in token)
    ):
        raise RepositoryDownloadError("Enter a valid Fleet Manager customer repository token.")
    if type(max_bytes) is not int or not 0 < max_bytes <= 32 * 1024 * 1024 or type(timeout) not in {int, float} or not 0 < timeout <= 60:
        raise RepositoryDownloadError("Fleet Manager repository downloads require valid size and timeout limits.")
    signing_key = resource == "signing key"
    label = "signing-key" if signing_key else resource
    network = "guest" if signing_key else "GDeploy host"
    invalid_body = (
        "The Fleet Manager repository returned an invalid signing key." if signing_key
        else "The Fleet Manager repository returned empty or oversized package metadata."
    )
    current = REPOSITORY + relative_path
    original_origin = _repository_url_origin(current, label)
    may_authenticate = True
    authorization = "Basic " + base64.b64encode((token + ":").encode()).decode()
    opener = urllib.request.build_opener(NoKeyRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    deadline = time.monotonic() + timeout
    for hop in range(6):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RepositoryDownloadError(f"The Fleet Manager {label} download timed out; check {network} network access.")
        # Fresh requests avoid carrying cookies or credentials through redirects.
        request = urllib.request.Request(current, headers={"Authorization": authorization} if may_authenticate else {})
        try:
            with opener.open(request, timeout=remaining) as response:
                if response.status != 200:
                    raise RepositoryDownloadError(f"The Fleet Manager {label} server returned an unexpected response.", status_code=response.status)
                chunks = []
                size = 0
                while True:
                    if time.monotonic() >= deadline:
                        raise RepositoryDownloadError(f"The Fleet Manager {label} download timed out; check {network} network access.")
                    # read1 performs at most one buffered/socket read, allowing a
                    # deadline check between chunks even when data trickles in.
                    chunk = response.read1(min(64 * 1024, max_bytes + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        raise RepositoryDownloadError(invalid_body)
                data = b"".join(chunks)
        except urllib.error.HTTPError as error:
            try:
                if error.code in {301, 302, 303, 307, 308}:
                    if hop == 5:
                        raise RepositoryDownloadError(f"The Fleet Manager {label} download exceeded its redirect limit; check the repository service.")
                    location = error.headers.get("Location") if error.headers is not None else None
                    if not isinstance(location, str) or not location or any(
                        char.isspace() or ord(char) < 32 or ord(char) == 127 for char in location
                    ):
                        raise RepositoryDownloadError(f"The Fleet Manager {label} repository returned an invalid redirect address.")
                    try:
                        if urllib.parse.urlsplit(location).scheme:
                            _repository_url_origin(location, label)
                        target = urllib.parse.urljoin(current, location)
                    except (ValueError, UnicodeError):
                        raise RepositoryDownloadError(f"The Fleet Manager {label} repository returned an invalid redirect address.") from None
                    target_origin = _repository_url_origin(target, label)
                    # Once the repository delegates to another origin, never
                    # restore its credential, even if a later hop returns there.
                    may_authenticate = may_authenticate and target_origin == original_origin
                    current = target
                    continue
                if error.code == 401 and hop == 0:
                    advice = (
                        "check the saved customer repository token and repository entitlement, then save it before creating a new deployment."
                        if signing_key else "check the customer repository token and repository entitlement."
                    )
                    raise RepositoryDownloadError(
                        "The Fleet Manager repository rejected authentication (HTTP 401); "
                        + advice, status_code=error.code,
                    ) from None
                if hop:
                    raise RepositoryDownloadError(
                        f"The redirected Fleet Manager {label} download failed (HTTP {error.code}); "
                        "check repository access or contact Corelight support.", status_code=error.code,
                    ) from None
                raise RepositoryDownloadError(
                    f"Could not download the Fleet Manager {resource} (HTTP {error.code}); "
                    "check the customer repository token and repository access.", status_code=error.code,
                ) from None
            finally:
                error.close()
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError, UnicodeError):
            raise RepositoryDownloadError(
                f"Could not download the Fleet Manager {resource} over verified TLS; "
                f"check {network} network access and trusted CA certificates."
            ) from None
        if not data:
            raise RepositoryDownloadError(invalid_body)
        return data


def download_signing_key(token):
    """Retrieve the key over verified HTTPS, without exposing redirect secrets."""
    return download_repository_file(token, "gpgkey")


def configure_online_repository(snapshot):
    version = requested_online_version(snapshot)
    token = snapshot.get("repository_token")
    if not isinstance(token, str) or not token or any(c.isspace() or ord(c) < 32 for c in token):
        raise FleetInstallError("ONLINE Fleet Manager installation requires the customer repository token in Setup.")
    run(["apt-get", "-q", "update"], "Could not update Ubuntu dependency repositories")
    run(APT_INSTALL + ["--", "ca-certificates", "gnupg", "apt-transport-https", "ufw"], "Could not install Fleet Manager repository and firewall prerequisites")
    key = download_signing_key(token)
    binary_key = run(["gpg", "--batch", "--dearmor"], "Could not read the Fleet Manager repository signing key", input=key, binary=True, private=True)
    keyring = "/etc/apt/keyrings/corelight_fleet-stable-archive-keyring.gpg"
    write_file(ROOT / keyring.lstrip("/"), binary_key, 0o644)
    write_file(
        ROOT / "etc/apt/auth.conf.d/corelight_fleet-stable.conf",
        "machine pkgrepos.corelight.cloud/corelight/fleet-stable/ login " + token + "\npassword irrelevant\n", 0o600,
    )
    write_file(
        ROOT / "etc/apt/sources.list.d/corelight_fleet-stable.list",
        f"deb [signed-by={keyring}] {REPOSITORY}any/ any main\n", 0o644,
    )
    run(["apt-get", "-q", "update"], "Could not update the signed Fleet Manager repository; check the customer repository token and network access")
    if version:
        available = run(["apt-cache", "madison", "corelight-fleet"], "Could not list available Fleet Manager repository versions")
        versions = {
            fields[1].strip()
            for line in available.splitlines()
            if len(fields := line.split("|")) >= 2 and fields[0].strip() == "corelight-fleet"
        }
        if version not in versions:
            raise FleetInstallError(
                f"Fleet Manager version {version} is not available from the configured repository. "
                "Choose an available exact package version or Latest before retrying."
            )
    package = "corelight-fleet" + ("=" + version if version else "")
    run(APT_INSTALL + ["--", package], "Could not install corelight-fleet from the vendor repository")
    # Check the exact selection before writing configuration or starting services.
    # This is an initial-install selection, with no hold preventing later upgrades.
    return installed_package_version(version) if version else None


def installed_package_version(requested=""):
    version = run(["dpkg-query", "-W", "-f=${Version}", "corelight-fleet"], "Could not determine the installed Fleet Manager version", timeout=30).strip()
    if not version:
        raise FleetInstallError("Fleet Manager did not report its installed package version.")
    if requested and version != requested:
        raise FleetInstallError(
            f"The installed Fleet Manager package does not match requested version {requested}; "
            "check the repository package and select an available exact version before redeploying."
        )
    return version


def configure_firewall():
    """Add only Fleet Manager's inbound TCP rules; retain UFW state and policy."""
    for port, purpose in ((443, "web interface"), (1443, "sensor connections")):
        # UFW skips an existing identical rule, so this is safe to repeat. Do not
        # reset/enable UFW or change SSH and unrelated application rules.
        run(["ufw", "allow", f"{port}/tcp"], f"Could not allow Fleet Manager {purpose} through UFW on TCP {port}", timeout=60)
    added = run(["ufw", "show", "added"], "Could not verify Fleet Manager UFW rules", timeout=30)
    for port in (443, 1443):
        if not re.search(rf"(?m)^ufw allow (?:in )?{port}/tcp(?: comment .*)?$", added):
            raise FleetInstallError(f"Fleet Manager UFW rule for TCP {port} could not be verified; inspect sudo ufw show added and sudo ufw status verbose.")


def configure_fleet(snapshot):
    try:
        owner = pwd.getpwnam("corelight-fleetd")
    except KeyError:
        raise FleetInstallError("The Fleet Manager package did not create its corelight-fleetd service account.") from None
    write_file(ROOT / "etc/corelight-fleetd.pem", snapshot["license_pem"], 0o400, owner=owner)
    path = ROOT / "etc/corelight-fleetd.conf"
    try:
        config = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(config, dict):
            raise ValueError
        messaging = config.setdefault("messaging", {})
        options = messaging.setdefault("http-options", {})
        if not isinstance(messaging, dict) or not isinstance(options, dict):
            raise ValueError
    except (OSError, ValueError, AttributeError):
        raise FleetInstallError("Fleet Manager's existing configuration is not valid JSON; review /etc/corelight-fleetd.conf.") from None
    config.setdefault("log-level", "INFO")
    config.setdefault("data-path", "/var/lib/corelight-fleetd")
    config.update({"ssl-key-path": "/etc/corelight-fleetd.pem", "ssl-certificate-path": "/etc/corelight-fleetd.pem", "bind-address": ":443"})
    messaging["community-string"] = snapshot["community_string"]
    options.update({"bind-address": ":1443", "certificate-path": "/etc/corelight-fleetd.pem", "key-path": "/etc/corelight-fleetd.pem"})
    write_file(path, json.dumps(config, indent=2) + "\n", 0o600, owner=owner)


def probe_https(fingerprint):
    """Authenticate the exact supplied identity certificate before sending HTTP.

    Fleet product identities need not name this VM's IP or chain to public CAs.
    An explicit certificate pin replaces hostname/CA validation on loopback only.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection(("127.0.0.1", 443), timeout=10) as connection:
        with context.wrap_socket(connection, server_hostname=None) as tls:
            actual = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
            if not hmac.compare_digest(actual, fingerprint):
                raise FleetInstallError("Fleet Manager HTTPS presented a certificate different from the supplied product identity PEM.")
            tls.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            response = http.client.HTTPResponse(tls)
            response.begin()
            if response.status not in {200, 301, 302, 303, 307, 308, 401, 403}:
                raise OSError("Fleet Manager web interface is not ready")


def verify_health(fingerprint):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            status = subprocess.run(["systemctl", "is-active", "--quiet", "corelight-fleetd"], capture_output=True, timeout=15)
            if status.returncode == 0:
                probe_https(fingerprint)
                # Sensor communication uses mTLS; a local listening socket check
                # is appropriate without a sensor identity, not an HTTP probe.
                with socket.create_connection(("127.0.0.1", 1443), timeout=5):
                    return
        except (OSError, subprocess.TimeoutExpired, http.client.HTTPException):
            pass
        time.sleep(5)
    raise FleetInstallError("Fleet Manager did not become healthy within 10 minutes; check its product identity license, systemd service, and ports 443/1443.")


def create_admin():
    output = run(
        ["runuser", "-u", "corelight-fleetd", "--", "/usr/bin/corelight-fleetd", "-c", "/etc/corelight-fleetd.conf", "create-user", "-a", "admin"],
        "Could not create the initial Fleet Manager administrator; inspect the vendor account state", private=True, timeout=60,
    )
    passwords = re.findall(r"(?m)^\s*Password:[ \t]*(\S[^\r\n]*)$", output)
    if len(passwords) != 1 or not passwords[0].strip() or len(passwords[0]) > 4096:
        raise FleetInstallError("Fleet Manager did not return exactly one temporary administrator password; inspect the vendor account state.")
    password = passwords[0].strip()
    SECRETS.add(password)
    return password


def install(payload):
    fingerprint = verify_transfers(payload)
    host_checks()
    snapshot = payload["fleetmanager"]
    with suppress_package_start():
        version = configure_online_repository(snapshot)
        configure_fleet(snapshot)
    configure_firewall()
    run(["systemctl", "daemon-reload"], "Could not reload the Fleet Manager service")
    run(["systemctl", "enable", "corelight-fleetd"], "Could not enable Fleet Manager at startup")
    run(["systemctl", "restart", "corelight-fleetd"], "Could not start Fleet Manager; check the product identity license")
    verify_health(fingerprint)
    version = version or installed_package_version()
    return {"username": "admin", "password": create_admin(), "password_change_required": True, "version": version}


def main():
    try:
        payload = json.loads(Path("payload.json").read_text())
        result = install(payload)
    except FleetInstallError as error:
        _diagnostic(str(error))
        try:
            journal = subprocess.run(
                ["journalctl", "-u", "corelight-fleetd", "--no-pager", "-n", "40", "-o", "cat"],
                capture_output=True, timeout=10,
            )
            _diagnostic(journal.stdout.decode("utf-8", "replace"))
        except (OSError, subprocess.TimeoutExpired):
            pass
        return 1
    except Exception:
        # Raw exception/traceback text may contain secrets from config or argv.
        _diagnostic("Fleet Manager provisioning failed while preparing guest configuration; check its package, license, and guest disk permissions.")
        return 1
    print("GDEPLOY_RESULT=" + json.dumps(result))
    return 0


def installer_script():
    return (
        '#!/bin/bash\nset -euo pipefail\ncd -- "$(dirname -- "$0")"\n'
        "python3 - <<'GDEPLOY_FLEET_PY'\n" + Path(__file__).read_text() + "\nGDEPLOY_FLEET_PY\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
