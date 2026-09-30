from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .bootstrap import Credentials, hash_password, resolve_credentials, validate_credentials, verify_password

__all__ = ["Config", "hash_password", "verify_password"]


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
        validate_credentials(Credentials(self.secret_key, self.admin_password_hash, self.admin_username))

    @classmethod
    def from_env(cls):
        data_dir = Path(os.getenv("GDEPLOY_DATA_DIR", "/data"))
        credentials = resolve_credentials(
            data_dir,
            os.getenv("GDEPLOY_SECRET_KEY", ""),
            os.getenv("GDEPLOY_ADMIN_PASSWORD_HASH", ""),
            os.getenv("GDEPLOY_ADMIN_USERNAME"),
        )
        return cls(
            data_dir=data_dir,
            secret_key=credentials.secret_key,
            admin_password_hash=credentials.admin_password_hash,
            admin_username=credentials.admin_username,
            cookie_secure=os.getenv("GDEPLOY_COOKIE_SECURE", "true").lower() == "true",
            ubuntu_iso=Path(os.getenv("GDEPLOY_OS_ISO") or os.getenv("GDEPLOY_UBUNTU_ISO", "/media/ubuntu.iso")),
            ubuntu_sha256=(os.getenv("GDEPLOY_OS_SHA256") or os.getenv("GDEPLOY_UBUNTU_SHA256", "")).strip().lower(),
            splunk_package=Path(os.getenv("GDEPLOY_SPLUNK_PACKAGE", "/media/splunk.tgz")),
            splunk_sha256=os.getenv("GDEPLOY_SPLUNK_SHA256", "").lower(),
            os_timeout=int(os.getenv("GDEPLOY_OS_TIMEOUT", "3600")),
        )
