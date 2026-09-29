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


def run(*args, env=None, capture=False):
    return subprocess.run(args, check=True, env=env, capture_output=capture, text=capture)


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


def verify_login_and_storage(url, directory, write=False):
    password = next(
        line.removeprefix("Password: ")
        for line in (directory / "bootstrap-credentials.txt").read_text().splitlines()
        if line.startswith("Password: ")
    )
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

    session = request("/api/login", {"username": "admin", "password": password}, "POST")
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


def smoke(image):
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
            run(*compose, "config", "--quiet", env=env)
            run(*compose, "up", "--detach", "--pull", "never", env=env)
            wait_healthy(url)
            uid = run(*compose, "exec", "-T", "gdeploy", "id", "-u", env=env, capture=True).stdout.strip()
            assert uid == "10001"
            verify_login_and_storage(url, directory, write=True)
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
            verify_login_and_storage(url, directory)
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    smoke(parser.parse_args().image)
