"""Resolve explicit credentials or initialize a fresh persistent data directory."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet


STATE_NAME = "bootstrap.json"
CREDENTIALS_NAME = "bootstrap-credentials.txt"
LOCK_NAME = ".bootstrap.lock"
SCHEMA_VERSION = 1
LOGGER = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class Credentials:
    secret_key: str
    admin_password_hash: str
    admin_username: str


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return "scrypt$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def _decode_password_hash(encoded: str) -> tuple[bytes, bytes]:
    if not isinstance(encoded, str):
        raise ValueError("Invalid password hash.")
    algorithm, salt, digest = encoded.split("$")
    if algorithm != "scrypt":
        raise ValueError("Invalid password hash.")
    decoded_salt = base64.b64decode(salt, altchars=b"-_", validate=True)
    decoded_digest = base64.b64decode(digest, altchars=b"-_", validate=True)
    if not decoded_salt or len(decoded_digest) != 64:
        raise ValueError("Invalid password hash.")
    return decoded_salt, decoded_digest


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt, expected = _decode_password_hash(encoded)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError):
        return False


def validate_credentials(credentials: Credentials):
    try:
        if not isinstance(credentials.secret_key, str):
            raise ValueError("Invalid key type.")
        decoded_key = base64.b64decode(credentials.secret_key, altchars=b"-_", validate=True)
        if len(decoded_key) != 32:
            raise ValueError("Invalid key length.")
        Fernet(credentials.secret_key.encode())
    except (ValueError, TypeError):
        raise ValueError("GDEPLOY_SECRET_KEY must be a valid Fernet key. Restore the original encryption key.") from None
    try:
        _decode_password_hash(credentials.admin_password_hash)
    except (ValueError, TypeError):
        raise ValueError("GDEPLOY_ADMIN_PASSWORD_HASH must contain a complete valid scrypt password hash.") from None
    if (
        not isinstance(credentials.admin_username, str)
        or not credentials.admin_username.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in credentials.admin_username)
    ):
        raise ValueError("GDEPLOY_ADMIN_USERNAME must be a nonempty username without control characters.")


def _check_private_file(descriptor: int, path: Path):
    status = os.fstat(descriptor)
    if not stat.S_ISREG(status.st_mode) or status.st_uid != os.geteuid() or status.st_mode & 0o077:
        raise ValueError(f"{path.name} must be a regular file owned by the application user with owner-only permissions.")


def _no_duplicate_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate bootstrap field.")
        result[key] = value
    return result


def _load_state(path: Path) -> Credentials | None:
    if path.is_symlink():
        raise ValueError("bootstrap.json must not be a symlink. Restore the original bootstrap file.")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("Cannot read bootstrap.json. Restore the original file and its owner-only permissions.") from None
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            _check_private_file(stream.fileno(), path)
            # The format is small; refuse unexpected files instead of reading arbitrary volume content.
            content = stream.read(16385)
        if len(content) > 16384:
            raise ValueError("Bootstrap state is oversized.")
        state = json.loads(content, object_pairs_hook=_no_duplicate_fields)
        expected = {"schema_version", "secret_key", "admin_password_hash", "admin_username"}
        if not isinstance(state, dict) or set(state) != expected:
            raise ValueError("Bootstrap fields are invalid.")
        if type(state["schema_version"]) is not int or state["schema_version"] != SCHEMA_VERSION:
            raise ValueError("Unsupported bootstrap schema.")
        credentials = Credentials(state["secret_key"], state["admin_password_hash"], state["admin_username"])
        validate_credentials(credentials)
        return credentials
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ValueError(
            "bootstrap.json is invalid, unreadable, or not private. Restore its original contents and owner-only "
            "permissions; GDeploy will not replace the encryption key."
        ) from None


@contextmanager
def _bootstrap_lock(directory: Path):
    path = directory / LOCK_NAME
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.fchmod(descriptor, 0o600)
    except FileExistsError:
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        except OSError:
            raise ValueError("The bootstrap lock must be a private regular file, not a symlink.") from None
    try:
        _check_private_file(descriptor, path)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _write_new_private(path: Path, content: str):
    """Publish a complete owner-only file atomically, refusing to replace any prior file."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _check_saved_conflicts(saved: Credentials, explicit: Credentials | None, username: str | None):
    if explicit and (
        explicit.secret_key != saved.secret_key or explicit.admin_password_hash != saved.admin_password_hash
    ):
        raise ValueError(
            "Environment credentials conflict with bootstrap.json. Restore the matching key and password hash; "
            "GDeploy will not replace stored credentials."
        )
    if username is not None and username != saved.admin_username:
        raise ValueError("GDEPLOY_ADMIN_USERNAME conflicts with bootstrap.json. Keep the existing administrator username.")


def resolve_credentials(
    data_dir: Path, key: str, password_hash: str, username: str | None = None
) -> Credentials:
    has_key = bool(key.strip())
    has_hash = bool(password_hash.strip())
    if has_key != has_hash:
        raise ValueError(
            "Incomplete environment credentials: GDEPLOY_SECRET_KEY and GDEPLOY_ADMIN_PASSWORD_HASH must be supplied "
            "together. Restore the matching original key and password hash; GDeploy will not replace either secret."
        )
    if data_dir.is_symlink():
        raise ValueError("GDEPLOY_DATA_DIR must be a directory, not a symlink.")
    explicit = Credentials(key, password_hash, "admin" if username is None else username) if has_key else None
    if explicit:
        validate_credentials(explicit)
        saved = _load_state(data_dir / STATE_NAME)
        if saved:
            _check_saved_conflicts(saved, explicit, username)
            return saved
        # Legacy installations keep their explicitly supplied secrets; do not generate any files.
        return explicit

    try:
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _bootstrap_lock(data_dir):
            saved = _load_state(data_dir / STATE_NAME)
            if saved:
                _check_saved_conflicts(saved, None, username)
                return saved
            if any(path.name != LOCK_NAME for path in data_dir.iterdir()):
                raise ValueError(
                    "Existing data or an incomplete bootstrap was found, but bootstrap.json is missing. Restore "
                    "the original GDEPLOY_SECRET_KEY and GDEPLOY_ADMIN_PASSWORD_HASH, or the matching bootstrap.json. "
                    "GDeploy refuses to create a replacement encryption key."
                )
            password = secrets.token_urlsafe(24)
            generated = Credentials(
                Fernet.generate_key().decode(), hash_password(password), "admin" if username is None else username
            )
            validate_credentials(generated)
            credential_file = data_dir / CREDENTIALS_NAME
            _write_new_private(
                credential_file,
                "GDeploy web-app sign-in\nURL: http://localhost:8000\n"
                f"Username: {generated.admin_username}\nPassword: {password}\n\n"
                "Store this in your password manager, then remove this file.\n"
                "Keep bootstrap.json backed up with the data volume; it contains the persistent encryption key.\n"
                "Guest credentials are in each deployment's Credentials panel.\n",
            )
            state = {
                "schema_version": SCHEMA_VERSION,
                "secret_key": generated.secret_key,
                "admin_password_hash": generated.admin_password_hash,
                "admin_username": generated.admin_username,
            }
            # Commit authoritative state last. An interrupted first run must never regenerate credentials.
            _write_new_private(data_dir / STATE_NAME, json.dumps(state, indent=2) + "\n")
            LOGGER.info("Initial GDeploy administrator credentials are available in %s (owner access only).", credential_file)
            return generated
    except OSError:
        raise ValueError(
            "Could not finish GDeploy first-start configuration. The application user needs writable persistent "
            "storage at GDEPLOY_DATA_DIR. Preserve any existing bootstrap files and restore the matching "
            "configuration before retrying; no existing encryption key will be replaced."
        ) from None
