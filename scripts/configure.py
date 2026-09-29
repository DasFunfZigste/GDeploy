#!/usr/bin/env python3
"""Create local bootstrap configuration without third-party dependencies."""

import argparse
import base64
import hashlib
import os
import re
import secrets
import tempfile
from pathlib import Path


EXTRA_ENV_KEYS = {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "FORWARDED_ALLOW_IPS"}


def _dotenv_value(raw: str, line_number: int) -> str:
    """Read a single-line dotenv literal without evaluating or interpolating it."""
    untrimmed = raw
    raw = raw.strip()
    quote = raw[:1]
    if quote in {"'", '"'}:
        result = []
        position = 1
        while position < len(raw):
            character = raw[position]
            if character == quote:
                tail = raw[position + 1:]
                if tail and not (tail[0].isspace() and (not tail.strip() or tail.lstrip().startswith("#"))):
                    raise ValueError(f"Unsupported text after quoted value on line {line_number}.")
                value = "".join(result)
                break
            if character == "\\" and position + 1 < len(raw):
                following = raw[position + 1]
                if quote == "'" and following == "'":
                    result.append("'")
                    position += 2
                    continue
                if quote == '"':
                    escapes = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}
                    if following not in escapes:
                        raise ValueError(f"Unsupported escape in double-quoted value on line {line_number}.")
                    result.append(escapes[following])
                    position += 2
                    continue
            result.append(character)
            position += 1
        else:
            raise ValueError(f"Unclosed or multiline quoted value on line {line_number}.")
    else:
        # A hash starts a dotenv comment only after whitespace; URL fragments stay literal.
        value = re.split(r"\s+#", untrimmed, maxsplit=1)[0].strip()
    if quote != "'" and "$" in value:
        raise ValueError(
            f"Unsupported interpolation on line {line_number}. "
            "Use literal values, single-quoting any value containing a dollar sign."
        )
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise ValueError(f"Value on line {line_number} cannot be represented in a Docker env file.")
    return value


def read_compose_env(path: Path) -> dict[str, str]:
    """Export GDeploy settings from the supported literal subset of Compose dotenv."""
    values = {}
    seen = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)", line)
        if not match:
            raise ValueError(f"Expected KEY=value on line {line_number}; multiline values are unsupported.")
        key, raw = match.groups()
        if key in seen:
            raise ValueError(f"Duplicate setting on line {line_number}; keep one value per key.")
        seen.add(key)
        if key.startswith("GDEPLOY_") or key in EXTRA_ENV_KEYS:
            values[key] = _dotenv_value(raw, line_number)
    for key in ("GDEPLOY_SECRET_KEY", "GDEPLOY_ADMIN_PASSWORD_HASH"):
        if not values.get(key):
            raise ValueError(f"{key} is missing or empty in .env; keep your existing credentials and encryption key.")
    return values


def docker_env_content(values: dict[str, str]) -> str:
    # Docker's --env-file preserves quotes literally; this file must not use shell quoting.
    return "# Generated from .env. Re-export after editing .env; keep both files private.\n" + "".join(
        f"{key}={value}\n" for key, value in values.items()
    )


def _write_new_private(path: Path, content: str):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)


def _replace_private(path: Path, content: str):
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory", type=Path, default=Path(__file__).resolve().parents[1],
        help="Setup directory (default: the repository containing this script).",
    )
    parser.add_argument(
        "--export-docker-env", action="store_true",
        help="Refresh docker.env from existing .env without changing credentials or encryption keys.",
    )
    arguments = parser.parse_args(argv)
    root = arguments.directory.expanduser().resolve()
    destination = root / ".env"
    docker_destination = root / "docker.env"
    credential_file = root / "bootstrap-credentials.txt"
    if arguments.export_docker_env:
        try:
            values = read_compose_env(destination)
            _replace_private(docker_destination, docker_env_content(values))
        except (OSError, ValueError) as error:
            raise SystemExit(f"Could not export docker.env: {error}") from error
        print(f"Updated {docker_destination} (owner access only).")
        print("Existing .env, credentials, and encryption key were preserved.")
        return
    if any(path.exists() or path.is_symlink() for path in (destination, docker_destination, credential_file)):
        raise SystemExit(
            "Configuration already exists. Keep your existing encryption key; edit .env directly. "
            "Use --export-docker-env to refresh docker.env from .env."
        )
    root.mkdir(parents=True, exist_ok=True)
    password = "admin"
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

    values = {
        "GDEPLOY_SECRET_KEY": encryption_key,
        "GDEPLOY_ADMIN_USERNAME": "admin",
        "GDEPLOY_ADMIN_PASSWORD_HASH": encoded,
        "GDEPLOY_COOKIE_SECURE": "false",
        "GDEPLOY_DATA_DIR": "/data",
        "GDEPLOY_UBUNTU_ISO": "/media/ubuntu.iso",
        "GDEPLOY_UBUNTU_SHA256": checksum("ubuntu.iso"),
        "GDEPLOY_SPLUNK_PACKAGE": "/media/splunk.tgz",
        "GDEPLOY_SPLUNK_SHA256": checksum("splunk.tgz"),
        "GDEPLOY_OS_TIMEOUT": "3600",
    }
    content = "# Keep this file private and backed up with the data volume.\n" + "".join(
        f"{key}='{value}'\n" for key, value in values.items()
    )
    _write_new_private(destination, content)
    _write_new_private(docker_destination, docker_env_content(values))
    _write_new_private(
        credential_file,
        "GDeploy web-app sign-in\nURL: http://localhost:8000\nUsername: admin\nPassword: "
        + password
        + "\n\nThese are initial credentials. GDeploy requires replacement credentials at the first sign-in.\n"
        "After setup, use your new username and password; these initial credentials no longer work.\n"
        "Guest credentials are in each deployment's Credentials panel.\n",
    )
    print(f"Created {destination} and {docker_destination} (owner access only).")
    print(f"Find the web-app sign-in credentials in {credential_file} (owner access only).")
    print("Use .env for Docker Compose, or docker.env with docker run --env-file.")
    print("Verify media checksums against their publishers before starting GDeploy.")


if __name__ == "__main__":
    main()
