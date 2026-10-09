#!/usr/bin/env python3
"""Test the published-image setup recipe without connecting to ESXi."""

import argparse
import base64
import gzip
import hashlib
import http.cookiejar
import io
import json
import os
import secrets
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


# Public Ed25519 key from RFC 8032 test vector 1; used only in isolated smoke volumes.
SMOKE_SSH_KEY = "ssh-ed25519 " + base64.b64encode(
    b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
    + bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
).decode("ascii") + " container-smoke"

# Deliberately invalid vendor credentials exercise encrypted defaults only.
# Smoke checks never contact the repository, pair a sensor, or provision a VM.
SMOKE_SENSOR_SETTINGS = {
    "repository_token": "smoke-sensor-repository-only",
    "community_string": "smoke-sensor-community-only",
    "license_key": "smoke-sensor-license-only",
    "fleet_url": "https://fleet.example.invalid",
    "server_sslname": "fleet.example.invalid",
    "api_network": "192.0.2.0/24",
}
SMOKE_SENSOR_SECRETS = tuple(SMOKE_SENSOR_SETTINGS[key] for key in ("repository_token", "community_string", "license_key"))


def smoke_splunk_package():
    """Tiny archive-format fixture; never deployed or represented as a vendor installer."""
    body = io.BytesIO()
    with tarfile.open(fileobj=body, mode="w") as archive:
        elf = b"\x7fELF\x02\x01" + b"\0" * 12 + (62).to_bytes(2, "little")
        for name, contents in (("splunk/bin/splunk", b"container smoke only"), ("splunk/bin/splunkd", elf)):
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            archive.addfile(member, io.BytesIO(contents))
    return gzip.compress(body.getvalue(), mtime=0)


@lru_cache(maxsize=1)
def smoke_fleet_pem():
    """Ephemeral test identity; never a Corelight license or a real deployment."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GDeploy smoke fixture")])
    current = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(current - timedelta(days=1))
                   .not_valid_after(current + timedelta(days=2)).sign(key, hashes.SHA256()))
    return (certificate.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    )).decode()


HOST_ROUTE_PROBE = """
import json
import sys
import urllib.request

from gdeploy import __version__

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
checks = (
    ('/api/health', ('application/json',), 'health'),
    ('/', ('text/html',), 'id="login-screen"'),
    (f'/static/app.js?v={__version__}', ('text/javascript', 'application/javascript'), 'must_change_credentials'),
    (f'/static/app.css?v={__version__}', ('text/css',), ':root'),
)
for path, expected_types, marker in checks:
    with opener.open(sys.argv[1] + path, timeout=10) as response:
        assert response.status == 200, 'Published host route returned an unexpected status'
        assert response.headers.get_content_type() in expected_types, 'Published host route returned the wrong content type'
        if marker != 'health':
            assert response.headers['Cache-Control'] == 'no-cache', 'Browser assets must revalidate after updates'
        body = response.read(1024 * 1024).decode('utf-8')
    if marker == 'health':
        assert json.loads(body) == {'status': 'ok'}, 'Published host route is not healthy'
    else:
        assert marker in body, 'Published host route did not serve the expected GDeploy UI asset'
        if path == '/':
            assert f'/static/app.js?v={__version__}' in body and f'/static/app.css?v={__version__}' in body
            assert f'GDeploy v{__version__}' in body and '__GDEPLOY_VERSION__' not in body
print('Health, login page, JavaScript and CSS are reachable through the published host interface.')
"""


def run(*args, env=None, capture=False, cwd=None):
    return subprocess.run(args, check=True, env=env, capture_output=capture, text=capture, cwd=cwd)


def wait_healthy(url):
    for _ in range(30):
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=3) as response:
                if json.load(response) == {"status": "ok"}:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError("Container did not become healthy within 60 seconds")


def assert_published_binding(container, bind_ip, port):
    container = container.strip()
    assert container and len(container.splitlines()) == 1, "Expected one application container"
    result = run("docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", container, capture=True)
    bindings = json.loads(result.stdout).get("8000/tcp")
    assert bindings == [{"HostIp": bind_ip, "HostPort": port}], "Unexpected published host address or port"


def credential_values(credentials):
    fields = dict(line.split(": ", 1) for line in credentials.splitlines() if ": " in line)
    return fields["Username"], fields["Password"]


def verify_login_and_storage(url, credentials, write=False):
    username, password = credential_values(credentials)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, payload=None, method="GET", csrf=None):
        headers = {"Content-Type": "application/octet-stream" if isinstance(payload, bytes) else "application/json"}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        req = urllib.request.Request(
            url + path,
            data=payload if isinstance(payload, bytes) else json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers=headers,
        )
        with opener.open(req, timeout=10) as response:
            return json.load(response)

    session = request("/api/login", {"username": username, "password": password}, "POST")
    assert session["authenticated"] is True
    if session["must_change_credentials"]:
        assert write, "The chosen administrator account did not survive container recreation"
        assert request("/api/session")["must_change_credentials"] is True
        for path in ("/api/settings", "/api/settings/ssh-keys", "/api/settings/splunk-package", "/api/settings/fleetmanager", "/api/settings/corelight-sensor", "/api/deployments"):
            try:
                request(path)
            except urllib.error.HTTPError as error:
                assert error.code == 403
                assert json.load(error)["detail"]["code"] == "credentials_change_required"
            else:
                raise AssertionError("Default credentials were allowed to access deployment features")
        for method in ("PUT", "DELETE"):
            try:
                request("/api/settings/corelight-sensor", SMOKE_SENSOR_SETTINGS if method == "PUT" else {}, method, session["csrf_token"])
            except urllib.error.HTTPError as error:
                assert error.code == 403
                assert json.load(error)["detail"]["code"] == "credentials_change_required"
            else:
                raise AssertionError("Default credentials were allowed to change sensor settings")
        new_username, new_password = "smoke-operator", secrets.token_urlsafe(24)
        result = request(
            "/api/account/setup",
            {"username": new_username, "password": new_password, "password_confirm": new_password},
            "POST",
            session["csrf_token"],
        )
        assert result == {"ok": True}
        assert request("/api/session") == {"authenticated": False}
        try:
            request("/api/login", {"username": username, "password": password}, "POST")
        except urllib.error.HTTPError as error:
            assert error.code == 401
        else:
            raise AssertionError("Initial credentials still work after account setup")
        credentials = f"Username: {new_username}\nPassword: {new_password}\n"
        session = request("/api/login", {"username": new_username, "password": new_password}, "POST")
    assert session["must_change_credentials"] is False
    # Small ISO-format fixture exercises upload permissions and persistence;
    # this smoke check never attempts an OS installation or contacts ESXi.
    media_body = b"\0" * (16 * 2048) + b"\x01CD001\x01" + b"gdeploy-container-smoke"
    media_checksum = hashlib.sha256(media_body).hexdigest()
    package_body = smoke_splunk_package()
    package_checksum = hashlib.sha256(package_body).hexdigest()
    publisher_checksum = hashlib.sha512(package_body).hexdigest()
    fleet_pem = smoke_fleet_pem()
    if write:
        request(
            "/api/settings",
            {"host": "esxi.example.invalid", "username": "test", "password": "smoke-only"},
            "PUT",
            session["csrf_token"],
        )
        request(
            f"/api/settings/media/upload?filename=container-smoke.iso&sha256={media_checksum}",
            media_body, "POST", session["csrf_token"],
        )
        request(
            f"/api/settings/splunk-package/upload?filename=container-smoke-linux-amd64.tgz&sha512={publisher_checksum}",
            package_body, "POST", session["csrf_token"],
        )
        request(
            "/api/settings/fleetmanager",
            {"mode": "online", "community_string": "smoke-community-only", "license_pem": fleet_pem,
             "license_name": "smoke-license.pem", "repository_token": "smoke-repository-only",
             "online_version": "29.2.2-1"},
            "PUT", session["csrf_token"],
        )
        sensor_saved = request("/api/settings/corelight-sensor", SMOKE_SENSOR_SETTINGS, "PUT", session["csrf_token"])
        assert sensor_saved["ready"] and sensor_saved["configured"]
        assert all(secret not in json.dumps(sensor_saved) for secret in SMOKE_SENSOR_SECRETS)
        saved_keys = request(
            "/api/settings/ssh-keys", {"public_keys": [SMOKE_SSH_KEY, SMOKE_SSH_KEY]},
            "PUT", session["csrf_token"],
        )
        assert saved_keys["count"] == 1, "Duplicate SSH keys must be saved once"
    settings = request("/api/settings")
    assert settings["host"] == "esxi.example.invalid" and settings["configured"] is True
    assert "password" not in settings
    assert settings["iso_configured"] is True
    assert settings["splunk_configured"] is True
    assert settings["fleetmanager_configured"] is True and settings["fleetmanager_mode"] == "online"
    assert settings["corelight_sensor_configured"] is True
    assert all(secret not in json.dumps(settings) for secret in SMOKE_SENSOR_SECRETS)
    assert settings["ssh_key_count"] == 1
    ssh_keys = request("/api/settings/ssh-keys")
    assert ssh_keys["count"] == 1 and ssh_keys["keys"][0]["public_key"] == SMOKE_SSH_KEY
    assert ssh_keys["keys"][0]["fingerprint"].startswith("SHA256:")
    media = request("/api/settings/media")
    assert media["ready"] and media["selected"]["source"] == "upload"
    assert media["selected"]["name"] == "container-smoke.iso"
    assert media["selected"]["sha256"] == media_checksum
    package = request("/api/settings/splunk-package")
    assert package["ready"] and package["selected"]["source"] == "upload"
    assert package["selected"]["name"] == "container-smoke-linux-amd64.tgz"
    assert package["selected"]["sha256"] == package_checksum
    assert package["selected"]["sha512"] == publisher_checksum
    assert package["items"][0]["can_delete"] is False
    fleet = request("/api/settings/fleetmanager")
    assert fleet["ready"] and fleet["mode"] == "online"
    assert fleet["community_string_configured"] and fleet["repository_token_configured"]
    assert fleet["online_version"] == "29.2.2-1"
    assert fleet["license"]["sha256"] == hashlib.sha256(fleet_pem.encode()).hexdigest()
    assert fleet["packages"] == []
    assert all(secret not in json.dumps(fleet) for secret in ("smoke-community-only", "smoke-repository-only", "PRIVATE KEY"))
    sensor = request("/api/settings/corelight-sensor")
    assert sensor["ready"] and sensor["configured"]
    assert all(sensor[key + "_configured"] for key in ("repository_token", "community_string", "license_key"))
    assert sensor["fleet_url"] == "https://fleet.example.invalid:1443"
    assert sensor["server_sslname"] == SMOKE_SENSOR_SETTINGS["server_sslname"]
    assert sensor["api_network"] == SMOKE_SENSOR_SETTINGS["api_network"]
    assert "pairing_token" not in sensor and "sensor_pairing_token" not in sensor
    assert all(secret not in json.dumps(sensor) for secret in SMOKE_SENSOR_SECRETS)
    retained_sensor = request(
        "/api/settings/corelight-sensor", {"repository_token": "", "community_string": "", "license_key": ""},
        "PUT", session["csrf_token"],
    )
    assert retained_sensor == sensor, "Saved sensor secrets did not survive a blank-field save or container recreation"
    request("/api/logout", {}, "POST", session["csrf_token"])
    return credentials


def smoke_configured(image):
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="gdeploy-release-smoke-") as folder:
        directory = Path(folder)
        unique = "gdeploy-smoke-" + directory.name.rsplit("-", 1)[-1]
        volume = unique + "-data"
        port = "18080"
        url = f"http://127.0.0.1:{port}"
        env = dict(
            os.environ,
            GDEPLOY_IMAGE=image,
            GDEPLOY_DATA_VOLUME=volume,
            GDEPLOY_PORT=port,
            GDEPLOY_BIND_IP="127.0.0.1",
        )
        compose = [
            "docker",
            "compose",
            "--project-name",
            unique,
            "--project-directory",
            folder,
            "--env-file",
            str(directory / ".env"),
            "-f",
            str(root / "compose.release.yaml"),
        ]
        try:
            run(
                "docker",
                "run",
                "--rm",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--mount",
                f"type=bind,source={folder},target=/setup",
                image,
                "python",
                "/app/scripts/configure.py",
                "--directory",
                "/setup",
            )
            assert (directory / "docker.env").is_file()
            credentials = (directory / "bootstrap-credentials.txt").read_text()
            run(*compose, "config", "--quiet", env=env)
            run(*compose, "up", "--detach", "--pull", "never", env=env)
            wait_healthy(url)
            container = run(*compose, "ps", "--quiet", "gdeploy", env=env, capture=True).stdout
            assert_published_binding(container, "127.0.0.1", port)
            uid = run(*compose, "exec", "-T", "gdeploy", "id", "-u", env=env, capture=True).stdout.strip()
            assert uid == "10001"
            run(*compose, "exec", "-T", "gdeploy", "python", "-c", (root / "scripts/smoke_iso.py").read_text(), env=env)
            credentials = verify_login_and_storage(url, credentials, write=True)
            run(*compose, "down", env=env)
            run(
                "docker",
                "run",
                "--detach",
                "--name",
                unique,
                "--init",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--tmpfs",
                "/tmp:size=256m,mode=1777",
                "--env-file",
                str(directory / "docker.env"),
                "--publish",
                f"127.0.0.1:{port}:8000",
                "--mount",
                f"type=volume,source={volume},target=/data",
                "--mount",
                f"type=bind,source={directory / 'media'},target=/media,readonly",
                image,
            )
            wait_healthy(url)
            assert_published_binding(unique, "127.0.0.1", port)
            verify_login_and_storage(url, credentials)
            print(
                "Image bootstrap, required setup, explicit loopback binding, login, persistence and docker run checks passed."
            )
        except BaseException:
            subprocess.run([*compose, "logs", "--tail", "60"], env=env, check=False)
            subprocess.run(["docker", "logs", "--tail", "60", unique], check=False)
            raise
        finally:
            # Only randomly named test resources created by this invocation.
            subprocess.run(["docker", "rm", "--force", unique], capture_output=True, check=False)
            subprocess.run([*compose, "down"], env=env, capture_output=True, check=False)
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True, check=False)


def smoke_automatic(image, source_compose=False, blank_env=False):
    """Exercise the documented startup command without running configure.py."""
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="gdeploy-first-start-") as folder:
        directory = Path(folder)
        unique = directory.name
        volume = unique + "-data"
        probe_name = unique + "-host-probe"
        port = "18082" if blank_env else "18081"
        bind_ip = "0.0.0.0"
        url = f"http://127.0.0.1:{port}"
        # Isolate this first-installation check from the caller's own settings.
        env = {key: value for key, value in os.environ.items() if not key.startswith("GDEPLOY_")}
        env.update(GDEPLOY_IMAGE=image, GDEPLOY_DATA_VOLUME=volume, GDEPLOY_PORT=port)
        # Leave GDEPLOY_BIND_IP unset: both a fresh checkout and the shipped
        # example must expose the published port using their own defaults.
        (directory / "media").mkdir(mode=0o755)
        if blank_env:
            (directory / ".env").write_bytes((root / ".env.example").read_bytes())
        else:
            assert not (directory / ".env").exists()
        compose_file = root / ("compose.yaml" if source_compose else "compose.release.yaml")
        compose = [
            "docker",
            "compose",
            "--project-name",
            unique,
            "--project-directory",
            folder,
            "-f",
            str(compose_file),
        ]
        if source_compose:
            # Test the actual source Compose settings using the image CI just
            # built. --no-build avoids rebuilding for each first-start case.
            override = directory / "compose.smoke.yaml"
            override.write_text("services:\n  gdeploy:\n    image: ${GDEPLOY_IMAGE}\n")
            compose.extend(["-f", str(override)])

        def command(*arguments, capture=False):
            return run(*compose, *arguments, env=env, capture=capture, cwd=directory)

        def read_file(path):
            return command("exec", "-T", "gdeploy", "cat", path, capture=True).stdout

        def verify_private_files():
            assert command("exec", "-T", "gdeploy", "id", "-u", capture=True).stdout.strip() == "10001"
            for path in ("/data/bootstrap.json", "/data/bootstrap-credentials.txt"):
                result = command("exec", "-T", "gdeploy", "stat", "-c", "%a:%u:%g", path, capture=True)
                assert result.stdout.strip() == "600:10001:10001"

        def verify_logs(secrets):
            result = command("logs", "--no-color", "gdeploy", capture=True)
            logs = result.stdout + result.stderr
            if any(value in logs for value in secrets):
                raise AssertionError("Container logs exposed a generated credential; output suppressed.")

        def verify_published_binding():
            container = command("ps", "--quiet", "gdeploy", capture=True).stdout.strip()
            assert_published_binding(container, bind_ip, port)

        def verify_host_route():
            # Use Docker's default bridge and its host gateway, rather than the
            # application's Compose network, to exercise the published host port.
            run(
                "docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "--name",
                probe_name,
                "--network",
                "bridge",
                "--add-host",
                "host.docker.internal:host-gateway",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                image,
                "python",
                "-c",
                HOST_ROUTE_PROBE,
                f"http://host.docker.internal:{port}",
            )

        try:
            command("config", "--quiet")
            command("up", "--detach", "--no-build", "--pull", "never")
            wait_healthy(url)
            verify_published_binding()
            verify_host_route()
            credentials = read_file("/data/bootstrap-credentials.txt")
            bootstrap = read_file("/data/bootstrap.json")
            saved = json.loads(bootstrap)
            username, password = credential_values(credentials)
            assert saved["admin_username"] == username
            assert (username, password) == ("admin", "admin")
            verify_private_files()
            chosen_credentials = verify_login_and_storage(url, credentials, write=True)
            private_values = (
                credential_values(chosen_credentials)[1],
                saved["secret_key"],
                saved["admin_password_hash"],
                *SMOKE_SENSOR_SECRETS,
            )
            assert all(private_values)
            verify_logs(private_values)

            # Replace the container while retaining only its named data volume.
            # Saved ESXi settings must still decrypt and the login must not rotate.
            command("down")
            command("up", "--detach", "--no-build", "--pull", "never")
            wait_healthy(url)
            verify_published_binding()
            assert read_file("/data/bootstrap.json") == bootstrap
            assert read_file("/data/bootstrap-credentials.txt") == credentials
            verify_private_files()
            verify_login_and_storage(url, chosen_credentials)
            verify_logs(private_values)
            mode = "blank .env.example" if blank_env else "no .env"
            layout = "source Compose" if source_compose else "release Compose"
            print(
                f"First-start, required setup, login, permissions and recreation passed ({layout}, {mode}, {bind_ip}:{port})."
            )
        except BaseException:
            # A failed first-start log check must not print the leaked secrets
            # into CI. Container state is useful and contains no credentials.
            subprocess.run([*compose, "ps", "--all"], env=env, cwd=directory, check=False)
            raise
        finally:
            # The unique project/volume names belong only to this smoke case.
            subprocess.run(["docker", "rm", "--force", probe_name], capture_output=True, check=False)
            subprocess.run([*compose, "down", "--volumes"], env=env, cwd=directory, capture_output=True, check=False)


def smoke(image, source_compose=False):
    smoke_configured(image)
    for blank_env in (False, True):
        smoke_automatic(image, source_compose=source_compose, blank_env=blank_env)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument(
        "--source-compose",
        action="store_true",
        help="Run first-start cases against the source Compose file using the prebuilt image.",
    )
    arguments = parser.parse_args()
    smoke(arguments.image, source_compose=arguments.source_compose)
