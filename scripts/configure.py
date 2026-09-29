#!/usr/bin/env python3
"""Create local bootstrap configuration without third-party dependencies."""

import base64
import hashlib
import os
import secrets
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    destination = root / ".env"
    credential_file = root / "bootstrap-credentials.txt"
    if destination.exists() or credential_file.exists():
        raise SystemExit("Configuration already exists. Keep your existing encryption key; edit .env directly.")
    os.umask(0o077)
    password = secrets.token_urlsafe(24)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    encoded = "scrypt$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()
    encryption_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    media = root / "media"
    media.mkdir(exist_ok=True)
    media.chmod(0o755)

    def checksum(name):
        path = media / name
        if not path.is_file():
            return ""
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    content = "\n".join(
        [
            "# Keep this file private and backed up with the data volume.",
            f"GDEPLOY_SECRET_KEY='{encryption_key}'",
            "GDEPLOY_ADMIN_USERNAME=admin",
            f"GDEPLOY_ADMIN_PASSWORD_HASH='{encoded}'",
            "GDEPLOY_COOKIE_SECURE=false",
            "GDEPLOY_DATA_DIR=/data",
            "GDEPLOY_UBUNTU_ISO=/media/ubuntu.iso",
            f"GDEPLOY_UBUNTU_SHA256={checksum('ubuntu.iso')}",
            "GDEPLOY_SPLUNK_PACKAGE=/media/splunk.tgz",
            f"GDEPLOY_SPLUNK_SHA256={checksum('splunk.tgz')}",
            "GDEPLOY_OS_TIMEOUT=3600",
            "",
        ]
    )
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(content)
    with credential_file.open("x", encoding="utf-8") as stream:
        stream.write(
            "GDeploy web-app sign-in\nURL: http://localhost:8000\nUsername: admin\nPassword: "
            + password
            + "\n\nStore this in your password manager, then remove this file.\nGuest credentials are in each deployment's Credentials panel.\n"
        )
    print("Created .env and bootstrap-credentials.txt (owner access only).")
    print("Web-app username: admin")
    print("Find the generated password in bootstrap-credentials.txt.")
    print("Verify media checksums against their publishers before running docker compose up --build -d.")


if __name__ == "__main__":
    main()
