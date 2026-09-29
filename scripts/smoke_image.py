#!/usr/bin/env python3
"""Test the published-image setup recipe without connecting to ESXi."""

import argparse
import http.cookiejar
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


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


def credential_values(credentials):
    fields = dict(line.split(": ", 1) for line in credentials.splitlines() if ": " in line)
    return fields["Username"], fields["Password"]


def verify_login_and_storage(url, credentials, write=False):
    username, password = credential_values(credentials)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, payload=None, method="GET", csrf=None):
        headers = {"Content-Type": "application/json"}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        req = urllib.request.Request(
            url + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers=headers,
        )
        with opener.open(req, timeout=10) as response:
            return json.load(response)

    session = request("/api/login", {"username": username, "password": password}, "POST")
    assert session["authenticated"] is True
    if write:
        request(
            "/api/settings",
            {"host": "esxi.example.invalid", "username": "test", "password": "smoke-only"},
            "PUT",
            session["csrf_token"],
        )
    settings = request("/api/settings")
    assert settings["host"] == "esxi.example.invalid" and settings["configured"] is True
    assert "password" not in settings
    request("/api/logout", {}, "POST", session["csrf_token"])


def smoke_configured(image):
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="gdeploy-release-smoke-") as folder:
        directory = Path(folder)
        unique = "gdeploy-smoke-" + directory.name.rsplit("-", 1)[-1]
        volume = unique + "-data"
        port = "18080"
        url = f"http://127.0.0.1:{port}"
        env = dict(os.environ, GDEPLOY_IMAGE=image, GDEPLOY_DATA_VOLUME=volume, GDEPLOY_PORT=port)
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
            uid = run(*compose, "exec", "-T", "gdeploy", "id", "-u", env=env, capture=True).stdout.strip()
            assert uid == "10001"
            verify_login_and_storage(url, credentials, write=True)
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
            verify_login_and_storage(url, credentials)
            print("Image bootstrap, Compose, non-root operation, login, persistence and docker run checks passed.")
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
        port = "8000" if source_compose else "18081"
        url = f"http://127.0.0.1:{port}"
        # Isolate this first-installation check from the caller's own settings.
        env = {key: value for key, value in os.environ.items() if not key.startswith("GDEPLOY_")}
        env.update(GDEPLOY_IMAGE=image, GDEPLOY_DATA_VOLUME=volume, GDEPLOY_PORT=port)
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

        try:
            command("config", "--quiet")
            command("up", "--detach", "--no-build", "--pull", "never")
            wait_healthy(url)
            credentials = read_file("/data/bootstrap-credentials.txt")
            bootstrap = read_file("/data/bootstrap.json")
            saved = json.loads(bootstrap)
            username, password = credential_values(credentials)
            assert saved["admin_username"] == username
            secrets = (password, saved["secret_key"], saved["admin_password_hash"])
            assert all(secrets)
            verify_private_files()
            verify_login_and_storage(url, credentials, write=True)
            verify_logs(secrets)

            # Replace the container while retaining only its named data volume.
            # Saved ESXi settings must still decrypt and the login must not rotate.
            command("down")
            command("up", "--detach", "--no-build", "--pull", "never")
            wait_healthy(url)
            assert read_file("/data/bootstrap.json") == bootstrap
            assert read_file("/data/bootstrap-credentials.txt") == credentials
            verify_private_files()
            verify_login_and_storage(url, credentials)
            verify_logs(secrets)
            mode = "blank .env.example" if blank_env else "no .env"
            layout = "source Compose" if source_compose else "release Compose"
            print(f"Automatic first-start, login, permissions, logs and recreation passed ({layout}, {mode}).")
        except BaseException:
            # A failed first-start log check must not print the leaked secrets
            # into CI. Container state is useful and contains no credentials.
            subprocess.run([*compose, "ps", "--all"], env=env, cwd=directory, check=False)
            raise
        finally:
            # The unique project/volume names belong only to this smoke case.
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
