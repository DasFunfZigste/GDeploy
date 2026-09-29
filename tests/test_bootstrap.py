import json
import logging
import os
import re
import stat
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from gdeploy import bootstrap
from gdeploy.config import Config, hash_password, verify_password
from gdeploy.db import Database
from gdeploy.main import create_app


@pytest.fixture
def bootstrap_data(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("GDEPLOY_"):
            monkeypatch.delenv(name)
    directory = tmp_path / "data"
    monkeypatch.setenv("GDEPLOY_DATA_DIR", str(directory))
    return directory


def password_from_file(directory):
    text = (directory / bootstrap.CREDENTIALS_NAME).read_text()
    return re.search(r"^Password: (.+)$", text, re.MULTILINE).group(1)


def test_fresh_start_generates_private_credentials_without_logging_secrets(bootstrap_data, caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        config = Config.from_env()
    password = password_from_file(bootstrap_data)
    assert verify_password(password, config.admin_password_hash)
    assert config.admin_username == "admin"
    assert config.cookie_secure is True
    assert config.ubuntu_sha256 == ""
    assert config.splunk_sha256 == ""
    state = json.loads((bootstrap_data / bootstrap.STATE_NAME).read_text())
    assert state == {
        "schema_version": 1,
        "secret_key": config.secret_key,
        "admin_password_hash": config.admin_password_hash,
        "admin_username": "admin",
    }
    assert password not in json.dumps(state)
    for name in (bootstrap.STATE_NAME, bootstrap.CREDENTIALS_NAME, bootstrap.LOCK_NAME):
        status = (bootstrap_data / name).stat()
        assert stat.S_IMODE(status.st_mode) == 0o600
        assert status.st_uid == os.geteuid()
    assert str(bootstrap_data / bootstrap.CREDENTIALS_NAME) in caplog.text
    for secret in (password, config.secret_key, config.admin_password_hash):
        assert secret not in caplog.text
    assert not (bootstrap_data / "gdeploy.sqlite3").exists()


def test_generated_credentials_allow_first_api_login(bootstrap_data, monkeypatch):
    monkeypatch.setenv("GDEPLOY_COOKIE_SECURE", "false")
    with TestClient(create_app(start_worker=False)) as client:
        assert client.get("/api/health").status_code == 200
        response = client.post("/api/login", json={"username": "admin", "password": password_from_file(bootstrap_data)})
        assert response.status_code == 200
        assert client.get("/api/deployments").status_code == 200
        assert "Secure" not in response.headers["set-cookie"]


def test_restart_preserves_secrets_and_decryption_after_plaintext_file_removal(bootstrap_data, spec):
    first = Config.from_env()
    saved_state = (bootstrap_data / bootstrap.STATE_NAME).read_bytes()
    password = password_from_file(bootstrap_data)
    db = Database(first.data_dir, first.secret_key)
    settings = {"host": "esxi.invalid", "username": "root", "password": "existing-esxi-password"}
    db.set_settings(settings)
    deployment = str(uuid.uuid4())
    db.create(deployment, spec, {"guest_password": "existing-vm-password"})
    (bootstrap_data / bootstrap.CREDENTIALS_NAME).unlink()

    second = Config.from_env()
    assert second == first
    assert (bootstrap_data / bootstrap.STATE_NAME).read_bytes() == saved_state
    assert not (bootstrap_data / bootstrap.CREDENTIALS_NAME).exists()
    assert verify_password(password, second.admin_password_hash)
    restarted = Database(second.data_dir, second.secret_key)
    assert restarted.settings() == settings
    assert restarted.get(deployment, private=True)["secrets"] == {"guest_password": "existing-vm-password"}


def test_full_environment_preserves_legacy_behavior_without_creating_files(bootstrap_data, monkeypatch):
    key = Fernet.generate_key().decode()
    encoded = hash_password("existing-admin-password")
    monkeypatch.setenv("GDEPLOY_SECRET_KEY", key)
    monkeypatch.setenv("GDEPLOY_ADMIN_PASSWORD_HASH", encoded)
    monkeypatch.setenv("GDEPLOY_ADMIN_USERNAME", "existing-admin")
    monkeypatch.setenv("GDEPLOY_UBUNTU_SHA256", "a" * 64)
    monkeypatch.setenv("GDEPLOY_COOKIE_SECURE", "false")
    config = Config.from_env()
    assert config.secret_key == key
    assert config.admin_password_hash == encoded
    assert config.admin_username == "existing-admin"
    assert config.ubuntu_sha256 == "a" * 64
    assert config.cookie_secure is False
    assert not bootstrap_data.exists()


def test_complete_legacy_environment_can_open_existing_data_without_bootstrap_files(bootstrap_data, monkeypatch):
    key = Fernet.generate_key().decode()
    encoded = hash_password("existing-admin-password")
    db = Database(bootstrap_data, key)
    db.set_settings({"password": "keep-existing-data"})
    before = {path.name: path.read_bytes() for path in bootstrap_data.iterdir()}
    monkeypatch.setenv("GDEPLOY_SECRET_KEY", key)
    monkeypatch.setenv("GDEPLOY_ADMIN_PASSWORD_HASH", encoded)
    config = Config.from_env()
    assert config.secret_key == key
    assert before == {path.name: path.read_bytes() for path in bootstrap_data.iterdir()}


@pytest.mark.parametrize("value", ["", " "])
def test_blank_environment_pair_allows_new_bootstrap(bootstrap_data, monkeypatch, value):
    monkeypatch.setenv("GDEPLOY_SECRET_KEY", value)
    monkeypatch.setenv("GDEPLOY_ADMIN_PASSWORD_HASH", value)
    assert Config.from_env().secret_key.strip()
    assert (bootstrap_data / bootstrap.STATE_NAME).is_file()


@pytest.mark.parametrize("name", ["GDEPLOY_SECRET_KEY", "GDEPLOY_ADMIN_PASSWORD_HASH"])
def test_partial_environment_is_rejected_without_writes_or_secret_disclosure(bootstrap_data, monkeypatch, name):
    value = Fernet.generate_key().decode() if name.endswith("SECRET_KEY") else hash_password("existing-password")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="Incomplete environment credentials") as error:
        Config.from_env()
    assert "Restore the matching original key and password hash" in str(error.value)
    assert value not in str(error.value)
    assert not bootstrap_data.exists()


@pytest.mark.parametrize(
    "existing",
    ["gdeploy.sqlite3", "gdeploy.sqlite3-wal", "gdeploy.sqlite3-shm", "artifacts", "bootstrap-credentials.txt", ".bootstrap.json.interrupted.tmp"],
)
def test_existing_data_without_original_key_is_preserved_and_rejected(bootstrap_data, existing):
    bootstrap_data.mkdir()
    path = bootstrap_data / existing
    if existing == "artifacts":
        path.mkdir()
        (path / "original-artifact").write_text("existing deployment media")
    else:
        path.write_bytes(b"original data")
    with pytest.raises(ValueError, match="refuses to create a replacement encryption key"):
        Config.from_env()
    assert not (bootstrap_data / bootstrap.STATE_NAME).exists()
    if existing == "artifacts":
        assert (path / "original-artifact").read_text() == "existing deployment media"
    else:
        assert path.read_bytes() == b"original data"


@pytest.mark.parametrize("field", ["secret_key", "admin_password_hash", "admin_username"])
def test_conflicting_environment_does_not_replace_saved_bootstrap(bootstrap_data, monkeypatch, field):
    original = Config.from_env()
    before = {path.name: path.read_bytes() for path in bootstrap_data.iterdir()}
    monkeypatch.setenv("GDEPLOY_SECRET_KEY", original.secret_key)
    monkeypatch.setenv("GDEPLOY_ADMIN_PASSWORD_HASH", original.admin_password_hash)
    if field == "secret_key":
        monkeypatch.setenv("GDEPLOY_SECRET_KEY", Fernet.generate_key().decode())
    elif field == "admin_password_hash":
        monkeypatch.setenv("GDEPLOY_ADMIN_PASSWORD_HASH", hash_password("different-password"))
    else:
        monkeypatch.setenv("GDEPLOY_ADMIN_USERNAME", "different-admin")
    with pytest.raises(ValueError, match="conflict"):
        Config.from_env()
    assert before == {path.name: path.read_bytes() for path in bootstrap_data.iterdir()}


def test_matching_explicit_credentials_reuse_saved_custom_username(bootstrap_data, monkeypatch):
    monkeypatch.setenv("GDEPLOY_ADMIN_USERNAME", "operator")
    original = Config.from_env()
    assert "Username: operator\n" in (bootstrap_data / bootstrap.CREDENTIALS_NAME).read_text()
    monkeypatch.delenv("GDEPLOY_ADMIN_USERNAME")
    monkeypatch.setenv("GDEPLOY_SECRET_KEY", original.secret_key)
    monkeypatch.setenv("GDEPLOY_ADMIN_PASSWORD_HASH", original.admin_password_hash)
    assert Config.from_env() == original


@pytest.mark.parametrize("mutation", ["invalid_json", "invalid_key", "invalid_hash", "missing_field", "schema", "duplicate"])
def test_corrupt_bootstrap_is_never_replaced(bootstrap_data, mutation):
    Config.from_env()
    path = bootstrap_data / bootstrap.STATE_NAME
    state = json.loads(path.read_text())
    if mutation == "invalid_json":
        content = "broken JSON with confidential material"
    elif mutation == "duplicate":
        content = path.read_text().replace('"schema_version": 1,', '"schema_version": 1, "schema_version": 1,')
    else:
        if mutation == "invalid_key":
            state["secret_key"] = "confidential-but-invalid-key"
        elif mutation == "invalid_hash":
            state["admin_password_hash"] = "scrypt$incomplete"
        elif mutation == "missing_field":
            del state["secret_key"]
        else:
            state["schema_version"] = 99
        content = json.dumps(state)
    path.write_text(content)
    credentials_before = (bootstrap_data / bootstrap.CREDENTIALS_NAME).read_bytes()
    with pytest.raises(ValueError, match="Restore its original contents") as error:
        Config.from_env()
    assert content not in str(error.value)
    assert "confidential" not in str(error.value)
    assert path.read_text() == content
    assert (bootstrap_data / bootstrap.CREDENTIALS_NAME).read_bytes() == credentials_before


@pytest.mark.parametrize("name", [bootstrap.STATE_NAME, bootstrap.LOCK_NAME, bootstrap.CREDENTIALS_NAME])
def test_symlink_bootstrap_inputs_are_never_followed(bootstrap_data, tmp_path, name):
    bootstrap_data.mkdir()
    target = tmp_path / "private-original"
    target.write_text("do not modify")
    (bootstrap_data / name).symlink_to(target)
    with pytest.raises(ValueError):
        Config.from_env()
    assert target.read_text() == "do not modify"
    assert (bootstrap_data / name).is_symlink()


def test_nonprivate_bootstrap_state_is_rejected(bootstrap_data):
    Config.from_env()
    state = bootstrap_data / bootstrap.STATE_NAME
    original = state.read_bytes()
    state.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        Config.from_env()
    assert state.read_bytes() == original


def test_simultaneous_first_starts_share_one_committed_configuration(bootstrap_data):
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: Config.from_env(), range(4)))
    assert all(config == results[0] for config in results)
    assert verify_password(password_from_file(bootstrap_data), results[0].admin_password_hash)
    assert {path.name for path in bootstrap_data.iterdir()} == {
        bootstrap.STATE_NAME, bootstrap.CREDENTIALS_NAME, bootstrap.LOCK_NAME,
    }


def test_interrupted_state_write_preserves_credentials_and_fails_closed(bootstrap_data, monkeypatch):
    write = bootstrap._write_new_private

    def interrupt_state(path, content):
        if path.name == bootstrap.STATE_NAME:
            raise OSError("simulated storage failure")
        write(path, content)

    monkeypatch.setattr(bootstrap, "_write_new_private", interrupt_state)
    with pytest.raises(ValueError, match="Could not finish GDeploy first-start"):
        Config.from_env()
    credentials = (bootstrap_data / bootstrap.CREDENTIALS_NAME).read_bytes()
    assert not (bootstrap_data / bootstrap.STATE_NAME).exists()
    assert not (bootstrap_data / "gdeploy.sqlite3").exists()
    monkeypatch.setattr(bootstrap, "_write_new_private", write)
    with pytest.raises(ValueError, match="incomplete bootstrap"):
        Config.from_env()
    assert (bootstrap_data / bootstrap.CREDENTIALS_NAME).read_bytes() == credentials
    assert not (bootstrap_data / bootstrap.STATE_NAME).exists()


@pytest.mark.parametrize("kind", ["key", "hash"])
def test_invalid_explicit_credentials_fail_without_values_in_error(bootstrap_data, monkeypatch, kind):
    key = Fernet.generate_key().decode()
    encoded = hash_password("existing-password")
    if kind == "key":
        key = "confidential-invalid-key"
    else:
        encoded = "scrypt$confidential$invalid"
    monkeypatch.setenv("GDEPLOY_SECRET_KEY", key)
    monkeypatch.setenv("GDEPLOY_ADMIN_PASSWORD_HASH", encoded)
    with pytest.raises(ValueError) as error:
        Config.from_env()
    assert "confidential" not in str(error.value)
    assert not bootstrap_data.exists()
