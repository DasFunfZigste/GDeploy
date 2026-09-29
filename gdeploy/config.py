from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return "scrypt$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=base64.urlsafe_b64decode(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Config:
    data_dir: Path
    secret_key: str
    admin_password_hash: str
    admin_username: str = "admin"
    cookie_secure: bool = True
    ubuntu_iso: Path = Path("/media/ubuntu.iso")
    ubuntu_sha256: str = ""
    splunk_package: Path = Path("/media/splunk.tgz")
    splunk_sha256: str = ""
    session_hours: int = 8
    os_timeout: int = 3600

    def __post_init__(self):
        Fernet(self.secret_key.encode())
        if not self.admin_password_hash.startswith("scrypt$"):
            raise ValueError("GDEPLOY_ADMIN_PASSWORD_HASH is required; run scripts/configure.py first.")

    @classmethod
    def from_env(cls):
        key = os.environ.get("GDEPLOY_SECRET_KEY", "")
        if not key:
            raise ValueError("GDEPLOY_SECRET_KEY is required; run scripts/configure.py first.")
        return cls(
            data_dir=Path(os.getenv("GDEPLOY_DATA_DIR", "/data")),
            secret_key=key,
            admin_password_hash=os.getenv("GDEPLOY_ADMIN_PASSWORD_HASH", ""),
            admin_username=os.getenv("GDEPLOY_ADMIN_USERNAME", "admin"),
            cookie_secure=os.getenv("GDEPLOY_COOKIE_SECURE", "true").lower() == "true",
            ubuntu_iso=Path(os.getenv("GDEPLOY_UBUNTU_ISO", "/media/ubuntu.iso")),
            ubuntu_sha256=os.getenv("GDEPLOY_UBUNTU_SHA256", "").lower(),
            splunk_package=Path(os.getenv("GDEPLOY_SPLUNK_PACKAGE", "/media/splunk.tgz")),
            splunk_sha256=os.getenv("GDEPLOY_SPLUNK_SHA256", "").lower(),
            os_timeout=int(os.getenv("GDEPLOY_OS_TIMEOUT", "3600")),
        )
