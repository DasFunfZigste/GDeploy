import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from gdeploy.db import Database
from gdeploy.main import create_app


PUBLIC_KEY = ed25519.Ed25519PrivateKey.from_private_bytes(b"a" * 32).public_key().public_bytes(
    serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
).decode()
KEY_PATH = "/api/settings/ssh-keys"


def test_public_key_settings_require_authentication(client):
    assert client.get(KEY_PATH).status_code == 401
    assert client.put(KEY_PATH, json={"public_keys": [PUBLIC_KEY]}).status_code == 401
    assert client.app.state.db.ssh_public_keys() == []


def test_settings_default_empty_without_esxi_or_media(signed_in):
    assert signed_in.get(KEY_PATH).json() == {"keys": [], "count": 0}
    assert signed_in.get("/api/settings").json()["ssh_key_count"] == 0
    assert signed_in.app.state.db.settings() is None


def test_save_normalizes_deduplicates_and_clear_is_explicit(signed_in):
    key = PUBLIC_KEY + " operator@desktop"
    response = signed_in.put(KEY_PATH, json={"public_keys": ["  " + key + "  ", PUBLIC_KEY + " alternate"]})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["keys"][0]["public_key"] == key
    assert data["keys"][0]["comment"] == "operator@desktop"
    assert data["keys"][0]["type"] == "ssh-ed25519"
    assert data["keys"][0]["fingerprint"].startswith("SHA256:")
    assert signed_in.get(KEY_PATH).json() == data
    assert signed_in.get("/api/settings").json()["ssh_key_count"] == 1
    assert signed_in.put(KEY_PATH, json={}).status_code == 422
    assert signed_in.app.state.db.ssh_public_keys() == [key]
    cleared = signed_in.put(KEY_PATH, json={"public_keys": []})
    assert cleared.status_code == 200 and cleared.json() == {"keys": [], "count": 0}
    assert signed_in.get("/api/settings").json()["ssh_key_count"] == 0


def test_keys_are_encrypted_persist_after_restart_and_not_written_to_audit(signed_in, config):
    key = PUBLIC_KEY + " unique-operator-comment"
    assert signed_in.put(KEY_PATH, json={"public_keys": [key]}).status_code == 200
    db = signed_in.app.state.db
    with db.connect() as connection:
        encrypted = connection.execute("SELECT value FROM ssh_public_keys").fetchone()[0]
        audit = "\n".join(row[0] for row in connection.execute("SELECT action FROM audit"))
    assert PUBLIC_KEY.split()[1] not in encrypted and "unique-operator-comment" not in encrypted
    assert db.unseal(encrypted) == [key]
    assert PUBLIC_KEY not in audit and "unique-operator-comment" not in audit
    assert "1 saved for future deployments" in audit
    with TestClient(create_app(config, start_worker=False)) as restarted:
        assert restarted.post("/api/login", json={"username": "admin", "password": "test-admin-passphrase"}).status_code == 200
        response = restarted.get(KEY_PATH)
        assert response.json()["keys"][0]["public_key"] == key
        assert response.headers["Cache-Control"] == "no-store"
        assert restarted.get("/api/settings").json()["ssh_key_count"] == 1


def test_upgrade_adds_ssh_keys_without_altering_existing_configuration(signed_in, config, spec):
    db = signed_in.app.state.db
    db.set_settings({"host": "esxi.example.test", "username": "root", "password": "saved-private-password"})
    deployment_id = str(uuid.uuid4())
    db.create(deployment_id, spec, {"vm_credentials": {"saved": "existing-private-data"}})
    original = db.get(deployment_id, private=True)
    administrator = db.administrator()
    with db.connect() as connection:
        connection.execute("DROP TABLE ssh_public_keys")
        settings_ciphertext = connection.execute("SELECT value FROM settings").fetchone()[0]
    upgraded = Database(config.data_dir, config.secret_key)
    assert upgraded.ssh_public_keys() == []
    assert upgraded.get(deployment_id, private=True) == original
    assert upgraded.administrator() == administrator
    with upgraded.connect() as connection:
        assert connection.execute("SELECT value FROM settings").fetchone()[0] == settings_ciphertext
    upgraded.set_ssh_public_keys([PUBLIC_KEY])
    assert upgraded.ssh_public_keys() == [PUBLIC_KEY]


def test_mutations_require_csrf_and_leave_saved_keys_unchanged(signed_in):
    signed_in.app.state.db.set_ssh_public_keys([PUBLIC_KEY])
    signed_in.headers.pop("X-CSRF-Token")
    assert signed_in.put(KEY_PATH, json={"public_keys": []}).status_code == 403
    assert signed_in.put(KEY_PATH, json={"public_keys": []}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert signed_in.app.state.db.ssh_public_keys() == [PUBLIC_KEY]


@pytest.mark.parametrize("keys", [
    [PUBLIC_KEY, "ssh-ed25519 invalid-key-private-marker"],
    ["-----BEGIN OPENSSH PRIVATE KEY-----\nprivate-body-marker\n-----END OPENSSH PRIVATE KEY-----"],
    [PUBLIC_KEY + "\n" + PUBLIC_KEY],
    [PUBLIC_KEY + "\x00unsafe-comment-marker"],
    ['command="private-command-marker" ' + PUBLIC_KEY],
    [PUBLIC_KEY] * 51, ["ssh-ed25519 " + "x" * 16384], [123], [None], "private-string-marker", None,
])
def test_rejected_payloads_do_not_echo_input_or_replace_previous_settings(signed_in, keys):
    db = signed_in.app.state.db
    db.set_ssh_public_keys([PUBLIC_KEY])
    with db.connect() as connection:
        encrypted = connection.execute("SELECT value FROM ssh_public_keys").fetchone()[0]
        audit = connection.execute("SELECT * FROM audit").fetchall()
    response = signed_in.put(KEY_PATH, json={"public_keys": keys})
    assert response.status_code == 422
    for content in (PUBLIC_KEY, "invalid-key-private-marker", "private-body-marker", "unsafe-comment-marker", "private-command-marker", "private-string-marker"):
        assert content not in response.text
    with db.connect() as connection:
        assert connection.execute("SELECT value FROM ssh_public_keys").fetchone()[0] == encrypted
        assert connection.execute("SELECT * FROM audit").fetchall() == audit
    assert db.ssh_public_keys() == [PUBLIC_KEY]


def test_unknown_payload_fields_are_rejected(signed_in):
    response = signed_in.put(KEY_PATH, json={"public_keys": [PUBLIC_KEY], "private_key": "private-input-marker"})
    assert response.status_code == 422
    assert "private-input-marker" not in response.text
    assert signed_in.app.state.db.ssh_public_keys() == []
