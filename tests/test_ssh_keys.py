import base64
import hashlib
import sqlite3

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from gdeploy.db import Database
from gdeploy.ssh_keys import MAX_SSH_KEY_BYTES, SSHKeyError, normalize_ssh_public_keys, ssh_public_key_details


def public_key(private_key):
    return private_key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ).decode()


ED25519 = public_key(ed25519.Ed25519PrivateKey.from_private_bytes(b"a" * 32))
SECOND_KEY = public_key(ed25519.Ed25519PrivateKey.from_private_bytes(b"b" * 32))


@pytest.fixture(scope="module")
def rsa_keys():
    return {bits: public_key(rsa.generate_private_key(public_exponent=65537, key_size=bits)) for bits in (1024, 2048)}


@pytest.mark.parametrize("curve", [ec.SECP256R1(), ec.SECP384R1(), ec.SECP521R1()])
def test_valid_ecdsa_curve_public_keys(curve):
    key = public_key(ec.generate_private_key(curve))
    assert normalize_ssh_public_keys([key]) == [key]
    assert ssh_public_key_details([key])[0]["type"] == key.split()[0]


def test_ed25519_normalization_deduplication_comments_and_fingerprint():
    kind, blob = ED25519.split()
    variants = [f"  {kind}\t  {blob}   operator@lab  ", ED25519 + " different comment", SECOND_KEY]
    details = ssh_public_key_details(variants)
    assert [item["public_key"] for item in details] == [ED25519 + " operator@lab", SECOND_KEY]
    assert details[0]["comment"] == "operator@lab"
    assert details[1]["comment"] == ""
    assert details[0]["type"] == "ssh-ed25519"
    expected = base64.b64encode(hashlib.sha256(base64.b64decode(blob)).digest()).decode().rstrip("=")
    assert details[0]["fingerprint"] == "SHA256:" + expected
    assert normalize_ssh_public_keys(variants) == [item["public_key"] for item in details]


def test_rsa_requires_at_least_2048_bits(rsa_keys):
    assert normalize_ssh_public_keys([rsa_keys[2048]]) == [rsa_keys[2048]]
    with pytest.raises(SSHKeyError, match="public key 2.*fewer than 2048") as error:
        normalize_ssh_public_keys([ED25519, rsa_keys[1024]])
    assert rsa_keys[1024] not in str(error.value)


@pytest.mark.parametrize("value", [
    "", "   ", "not-a-public-key sensitive-content", "ssh-ed25519", "ssh-ed25519 not-valid-base64",
    "ssh-rsa " + ED25519.split()[1], "ssh-ed25519 " + base64.b64encode(b"not-an-ssh-key").decode(),
    'command="touch /tmp/pwned" ' + ED25519, "restrict " + ED25519,
    "ssh-ed25519-cert-v01@openssh.com " + ED25519.split()[1],
    "sk-ssh-ed25519@openssh.com " + ED25519.split()[1], "ssh-dss AAAA test-dsa",
    ED25519 + "\n" + SECOND_KEY, ED25519 + "\rcomment", ED25519 + "\0comment",
    ED25519 + "\x1b[31mcomment", ED25519 + "\x7fcomment", ED25519 + "\vcomment",
    ED25519 + "\u2028comment", ED25519 + "\u2029comment", ED25519 + "\u202ecomment",
    ED25519 + "\ud800comment", ED25519 + " -----BEGIN PRIVATE KEY-----",
])
def test_unsafe_or_invalid_entries_are_indexed_without_echoing_input(value):
    with pytest.raises(SSHKeyError, match="SSH public key 2") as error:
        normalize_ssh_public_keys([SECOND_KEY, value])
    assert "sensitive-content" not in str(error.value)
    assert ED25519.split()[1] not in str(error.value)
    assert SECOND_KEY not in str(error.value)


def test_rejects_actual_private_key_instead_of_storing_it():
    private = ed25519.Ed25519PrivateKey.from_private_bytes(b"c" * 32).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
    ).decode()
    with pytest.raises(SSHKeyError) as error:
        normalize_ssh_public_keys([private])
    assert "OPENSSH PRIVATE KEY" not in str(error.value)
    assert private.splitlines()[1] not in str(error.value)


def test_limits_apply_to_input_count_and_encoded_bytes():
    assert normalize_ssh_public_keys([ED25519] * 50) == [ED25519]
    with pytest.raises(SSHKeyError, match="at most 50"):
        normalize_ssh_public_keys([ED25519] * 51)
    with pytest.raises(SSHKeyError, match="16 KiB"):
        normalize_ssh_public_keys([ED25519 + " " + "x" * MAX_SSH_KEY_BYTES])
    with pytest.raises(SSHKeyError, match="16 KiB"):
        normalize_ssh_public_keys([ED25519 + " " + "é" * (MAX_SSH_KEY_BYTES // 2)])
    max_size = ED25519 + " " + "x" * (MAX_SSH_KEY_BYTES - len(ED25519) - 1)
    assert normalize_ssh_public_keys([max_size]) == [max_size]


@pytest.mark.parametrize("value", [ED25519, None, {}, [None], [123], [False]])
def test_non_list_or_non_string_values_are_rejected(value):
    with pytest.raises(SSHKeyError):
        normalize_ssh_public_keys(value)


def test_safe_unicode_comments_are_preserved():
    assert normalize_ssh_public_keys([ED25519 + " Antonio’s laptop 🔑"]) == [ED25519 + " Antonio’s laptop 🔑"]


def test_failed_validation_preserves_encrypted_saved_keys_and_audit(config):
    db = Database(config.data_dir, config.secret_key)
    db.set_ssh_public_keys([ED25519 + " operator"])
    with db.connect() as connection:
        encrypted = connection.execute("SELECT value FROM ssh_public_keys").fetchone()[0]
        audit = connection.execute("SELECT * FROM audit").fetchall()
    with pytest.raises(SSHKeyError):
        db.set_ssh_public_keys([SECOND_KEY, "malformed-key secret-input"])
    with db.connect() as connection:
        assert connection.execute("SELECT value FROM ssh_public_keys").fetchone()[0] == encrypted
        assert connection.execute("SELECT * FROM audit").fetchall() == audit
    assert db.ssh_public_keys() == [ED25519 + " operator"]


def test_audit_failure_rolls_back_key_update(config):
    db = Database(config.data_dir, config.secret_key)
    db.set_ssh_public_keys([ED25519])
    with db.connect() as connection:
        connection.execute("""CREATE TRIGGER fail_ssh_key_audit BEFORE INSERT ON audit
            WHEN NEW.action LIKE 'Administrator SSH public keys updated:%'
            BEGIN SELECT RAISE(ABORT, 'test audit failure'); END""")
        encrypted = connection.execute("SELECT value FROM ssh_public_keys").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="test audit failure"):
        db.set_ssh_public_keys([SECOND_KEY])
    with db.connect() as connection:
        assert connection.execute("SELECT value FROM ssh_public_keys").fetchone()[0] == encrypted
    assert db.ssh_public_keys() == [ED25519]
