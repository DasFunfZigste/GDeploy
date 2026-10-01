"""Validate administrator public keys without accepting authorized_keys options."""

from __future__ import annotations

import base64
import binascii
import hashlib
import unicodedata

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

MAX_SSH_KEYS = 50
MAX_SSH_KEY_BYTES = 16 * 1024
SUPPORTED_KEY_TYPES = {
    "ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
}


class SSHKeyError(ValueError):
    """Safe, indexed validation diagnostics; never includes submitted key material."""


def _parse_public_key(value: str, index: int) -> dict:
    label = f"SSH public key {index}"
    if not isinstance(value, str):
        raise SSHKeyError(f"{label} must be a single-line public key.")
    if len(value) > MAX_SSH_KEY_BYTES:
        raise SSHKeyError(f"{label} exceeds the 16 KiB limit.")
    # OpenSSH permits horizontal tab separators; canonical output contains spaces.
    value = value.replace("\t", " ")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value):
        raise SSHKeyError(f"{label} must be a single line without control characters.")
    if len(value.encode("utf-8")) > MAX_SSH_KEY_BYTES:
        raise SSHKeyError(f"{label} exceeds the 16 KiB limit.")
    parts = value.strip().split(maxsplit=2)
    if len(parts) < 2 or parts[0] not in SUPPORTED_KEY_TYPES:
        raise SSHKeyError(
            f"{label} must be an Ed25519, RSA, or ECDSA public key without options or certificates."
        )
    if "-----BEGIN" in value or "-----END" in value:
        raise SSHKeyError(f"{label} must contain only a public key, never private key material.")
    try:
        base64.b64decode(parts[1], validate=True)
        parsed = serialization.load_ssh_public_key(f"{parts[0]} {parts[1]}".encode("ascii"))
        canonical = parsed.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode("ascii")
    except (ValueError, TypeError, UnicodeError, binascii.Error, UnsupportedAlgorithm):
        raise SSHKeyError(f"{label} is not a valid supported public key.") from None
    if isinstance(parsed, rsa.RSAPublicKey) and parsed.key_size < 2048:
        raise SSHKeyError(f"{label} uses RSA with fewer than 2048 bits. Generate a stronger key.")
    key_type, blob = canonical.split()
    # Comments are presentation only; identity and fingerprints use actual key bytes.
    comment = parts[2].strip() if len(parts) == 3 else ""
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(blob)).digest()).decode().rstrip("=")
    return {
        "public_key": canonical + (" " + comment if comment else ""),
        "type": key_type,
        "fingerprint": fingerprint,
        "comment": comment,
    }


def ssh_public_key_details(public_keys: list[str]) -> list[dict]:
    """Normalize keys and deduplicate their identities, retaining the first comment."""
    if not isinstance(public_keys, list):
        raise SSHKeyError("SSH public keys must be provided as a list of single-line public keys.")
    if len(public_keys) > MAX_SSH_KEYS:
        raise SSHKeyError(f"Save at most {MAX_SSH_KEYS} SSH public keys.")
    result = []
    identities = set()
    for index, value in enumerate(public_keys, start=1):
        parsed = _parse_public_key(value, index)
        identity = " ".join(parsed["public_key"].split()[:2])
        if identity not in identities:
            identities.add(identity)
            result.append(parsed)
    return result


def normalize_ssh_public_keys(public_keys: list[str]) -> list[str]:
    """Return canonical one-line keys; raise SSHKeyError without echoing input."""
    return [key["public_key"] for key in ssh_public_key_details(public_keys)]
