import hashlib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from gdeploy.config import hash_password, verify_password
from gdeploy.db import Database


def test_administrator_is_seeded_once_and_only_first_initialization_revokes_sessions(config):
    db = Database(config.data_dir, config.secret_key)
    old_session, _ = db.new_session("admin", 1)
    default_hash = hash_password("admin")
    account = db.ensure_administrator("admin", default_hash)
    assert account == {"username": "admin", "password_hash": default_hash, "must_change_credentials": True}
    assert db.session(old_session) is None

    live_session, _ = db.new_session("admin", 1)
    assert db.ensure_administrator("different-user", hash_password("different-password")) == account
    assert db.administrator() == account
    assert db.session(live_session) is not None


def test_existing_database_migration_retains_nondefault_password_and_encrypted_settings(config):
    db = Database(config.data_dir, config.secret_key)
    db.set_settings({"password": "persisted-esxi-password"})
    old_session, _ = db.new_session("existing-admin", 1)
    # Emulate an earlier schema which did not have a persisted administrator table.
    with db.connect() as c:
        c.execute("DROP TABLE administrator")
    migrated = Database(config.data_dir, config.secret_key)
    account = migrated.ensure_administrator("existing-admin", config.admin_password_hash)
    assert account["username"] == "existing-admin"
    assert account["password_hash"] == config.admin_password_hash
    assert account["must_change_credentials"] is False
    assert migrated.settings() == {"password": "persisted-esxi-password"}
    assert migrated.session(old_session) is None


def test_setup_commits_credentials_and_revokes_all_sessions_and_failed_attempts(config):
    db = Database(config.data_dir, config.secret_key)
    db.ensure_administrator("admin", hash_password("admin"))
    first, _ = db.new_session("admin", 1)
    second, _ = db.new_session("admin", 1)
    db.failed_login("192.0.2.1")
    db.set_settings({"password": "unchanged-encrypted-secret"})
    before_ciphertext = None
    with db.connect() as c:
        before_ciphertext = c.execute("SELECT value FROM settings WHERE id=1").fetchone()[0]
    replacement_hash = hash_password("replacement-private-password")
    assert db.complete_initial_setup(first, "operator", replacement_hash) is True
    assert db.administrator() == {
        "username": "operator", "password_hash": replacement_hash, "must_change_credentials": False,
    }
    assert verify_password("replacement-private-password", db.administrator()["password_hash"])
    assert not verify_password("admin", db.administrator()["password_hash"])
    assert db.session(first) is None
    assert db.session(second) is None
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] == 0
        assert c.execute("SELECT value FROM settings WHERE id=1").fetchone()[0] == before_ciphertext
        actions = [row[0] for row in c.execute("SELECT action FROM audit")]
    assert actions[-1] == "Administrator completed initial credential setup"
    assert replacement_hash not in str(actions)
    assert "replacement-private-password" not in str(actions)
    assert db.settings()["password"] == "unchanged-encrypted-secret"


def test_changed_account_survives_restart_with_unchanged_bootstrap_credentials(config):
    original_hash = hash_password("admin")
    db = Database(config.data_dir, config.secret_key)
    db.ensure_administrator("admin", original_hash)
    token, _ = db.new_session("admin", 1)
    new_hash = hash_password("replacement-private-password")
    assert db.complete_initial_setup(token, "operator", new_hash)
    restarted = Database(config.data_dir, config.secret_key)
    account = restarted.ensure_administrator("admin", original_hash)
    assert account == {"username": "operator", "password_hash": new_hash, "must_change_credentials": False}
    assert restarted.administrator() == account


@pytest.mark.parametrize("state", ["missing", "expired", "wrong_username", "completed"])
def test_stale_or_unrelated_sessions_cannot_complete_setup(config, state):
    db = Database(config.data_dir, config.secret_key)
    old_hash = hash_password("admin")
    db.ensure_administrator("admin", old_hash)
    token, _ = db.new_session("somebody-else" if state == "wrong_username" else "admin", 1)
    if state == "missing":
        token = "nonexistent-session"
    elif state == "expired":
        with db.connect() as c:
            c.execute(
                "UPDATE sessions SET expires=? WHERE token=?",
                (time.time() - 1, hashlib.sha256(token.encode()).hexdigest()),
            )
    elif state == "completed":
        assert db.complete_initial_setup(token, "operator", hash_password("first-change-password"))
        token, _ = db.new_session("operator", 1)
    before = db.administrator()
    assert db.complete_initial_setup(token, "replacement-user", hash_password("replacement-password")) is False
    assert db.administrator() == before


def test_simultaneous_setup_requests_have_one_winner(config):
    db = Database(config.data_dir, config.secret_key)
    db.ensure_administrator("admin", hash_password("admin"))
    sessions = [db.new_session("admin", 1)[0] for _ in range(2)]
    hashes = [hash_password("first-replacement-password"), hash_password("second-replacement-password")]

    def setup(index):
        return db.complete_initial_setup(sessions[index], f"operator{index}", hashes[index])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(setup, range(2)))
    assert sorted(results) == [False, True]
    winner = results.index(True)
    assert db.administrator() == {
        "username": f"operator{winner}", "password_hash": hashes[winner], "must_change_credentials": False,
    }
    assert all(db.session(token) is None for token in sessions)


def test_account_change_rolls_back_if_session_revocation_fails(config):
    db = Database(config.data_dir, config.secret_key)
    before = db.ensure_administrator("admin", hash_password("admin"))
    token, _ = db.new_session("admin", 1)
    db.failed_login("192.0.2.1")
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_session_delete BEFORE DELETE ON sessions BEGIN SELECT RAISE(ABORT, 'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.complete_initial_setup(token, "operator", hash_password("replacement-private-password"))
    assert db.administrator() == before
    assert db.session(token) is not None
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0


def test_simultaneous_account_initialization_preserves_one_seed(config):
    db = Database(config.data_dir, config.secret_key)
    initial_hashes = [hash_password("admin"), hash_password("existing-private-password")]

    def initialize(index):
        return db.ensure_administrator(f"operator{index}", initial_hashes[index])

    with ThreadPoolExecutor(max_workers=2) as executor:
        accounts = list(executor.map(initialize, range(2)))
    assert accounts[0] == accounts[1] == db.administrator()
