from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from .bootstrap import verify_password


def now():
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, directory: Path, key: str):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "gdeploy.sqlite3"
        self.cipher = Fernet(key.encode())
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS esxi_certificates (endpoint TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS media_settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS media_files (id TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                    stage TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    spec TEXT NOT NULL, vms TEXT NOT NULL, resources TEXT NOT NULL,
                    secrets TEXT NOT NULL, error TEXT, parent_id TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, deployment_id TEXT NOT NULL,
                    at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_deployment ON events(deployment_id, id);
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, csrf TEXT NOT NULL, username TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT NOT NULL, at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (at TEXT NOT NULL, action TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS administrator (
                    id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, password_hash TEXT NOT NULL,
                    must_change_credentials INTEGER NOT NULL CHECK(must_change_credentials IN (0,1))
                );
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def seal(self, value):
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def unseal(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def settings(self):
        with self.connect() as c:
            row = c.execute("SELECT value FROM settings WHERE id=1").fetchone()
        return self.unseal(row[0]) if row else None

    def set_settings(self, value):
        with self.connect() as c:
            c.execute("INSERT OR REPLACE INTO settings VALUES(1,?)", (self.seal(value),))
        self.audit("ESXi connection settings updated")

    def esxi_certificate(self, endpoint):
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM esxi_certificates WHERE endpoint=?", (endpoint,)).fetchone()
        return self.unseal(row[0]) if row else None

    def media_settings(self):
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM media_settings WHERE id=1").fetchone()
        return self.unseal(row[0]) if row else None

    def media_files(self):
        with self.connect() as connection:
            rows = connection.execute("SELECT value FROM media_files ORDER BY id").fetchall()
        return [self.unseal(row[0]) for row in rows]

    def set_media_settings(self, value, *, uploaded=False):
        """Save a verified selection, registering newly uploaded media in the same transaction."""
        with self.connect() as connection:
            if uploaded:
                connection.execute("INSERT INTO media_files VALUES(?,?)", (value["id"], self.seal(value)))
            connection.execute("INSERT OR REPLACE INTO media_settings VALUES(1,?)", (self.seal(value),))
            connection.execute(
                "INSERT INTO audit VALUES(?,?)",
                (now(), f"OS ISO selected: {value['name']}; SHA-256 {value['sha256']}"),
            )

    def trust_esxi_certificate(self, endpoint, pem, fingerprint):
        value = {"pem": pem, "trusted_at": now()}
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO esxi_certificates VALUES(?,?)", (endpoint, self.seal(value))
            )
            connection.execute(
                "INSERT INTO audit VALUES(?,?)",
                (now(), f"ESXi certificate trusted for {endpoint}; SHA-256 {fingerprint}"),
            )
        return value

    def remove_esxi_certificate(self, endpoint):
        with self.connect() as connection:
            connection.execute("DELETE FROM esxi_certificates WHERE endpoint=?", (endpoint,))
            connection.execute("INSERT INTO audit VALUES(?,?)", (now(), f"ESXi certificate trust removed for {endpoint}"))

    def audit(self, action):
        with self.connect() as c:
            c.execute("INSERT INTO audit VALUES(?,?)", (now(), action))

    @staticmethod
    def _administrator(row):
        if row is None:
            raise RuntimeError("The administrator account has not been initialized.")
        return {
            "username": row["username"],
            "password_hash": row["password_hash"],
            "must_change_credentials": bool(row["must_change_credentials"]),
        }

    def ensure_administrator(self, username: str, password_hash: str):
        """Seed the application account once; later startup configuration cannot reset it."""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT username,password_hash,must_change_credentials FROM administrator WHERE id=1").fetchone()
            if row is None:
                must_change = verify_password("admin", password_hash)
                c.execute("INSERT INTO administrator VALUES(1,?,?,?)", (username, password_hash, int(must_change)))
                # Sessions issued before persistent account initialization must authenticate again.
                c.execute("DELETE FROM sessions")
                row = c.execute("SELECT username,password_hash,must_change_credentials FROM administrator WHERE id=1").fetchone()
            return self._administrator(row)

    def administrator(self):
        with self.connect() as c:
            row = c.execute("SELECT username,password_hash,must_change_credentials FROM administrator WHERE id=1").fetchone()
        return self._administrator(row)

    def complete_initial_setup(self, session_token: str, username: str, password_hash: str) -> bool:
        """Replace initial credentials and revoke every session in a single transaction."""
        if not session_token:
            return False
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute(
                "SELECT administrator.username FROM administrator JOIN sessions "
                "ON sessions.username=administrator.username "
                "WHERE administrator.id=1 AND administrator.must_change_credentials=1 "
                "AND sessions.token=? AND sessions.expires>?",
                (hashlib.sha256(session_token.encode()).hexdigest(), time.time()),
            ).fetchone()
            if current is None:
                return False
            c.execute(
                "UPDATE administrator SET username=?,password_hash=?,must_change_credentials=0 WHERE id=1",
                (username, password_hash),
            )
            c.execute("DELETE FROM sessions")
            c.execute("DELETE FROM login_attempts")
            c.execute("INSERT INTO audit VALUES(?,?)", (now(), "Administrator completed initial credential setup"))
            return True

    def login_allowed(self, ip):
        with self.connect() as c:
            c.execute("DELETE FROM login_attempts WHERE at < ?", (time.time() - 900,))
            count = c.execute("SELECT COUNT(*) FROM login_attempts WHERE ip=?", (ip,)).fetchone()[0]
            return count < 5

    def failed_login(self, ip):
        with self.connect() as c:
            c.execute("INSERT INTO login_attempts VALUES(?,?)", (ip, time.time()))

    def new_session(self, username, hours):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connect() as c:
            c.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            c.execute(
                "INSERT INTO sessions VALUES(?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), csrf, username, time.time() + hours * 3600),
            )
        return token, csrf

    def session(self, token):
        if not token:
            return None
        with self.connect() as c:
            row = c.execute(
                "SELECT csrf,username FROM sessions WHERE token=? AND expires>?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
        return dict(row) if row else None

    def delete_session(self, token):
        with self.connect() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def create(self, deployment_id, spec, secret_data, parent_id=None):
        stamp = now()
        vms = [dict(vm, status="pending", services=[]) for vm in spec["vms"]]
        with self.connect() as c:
            c.execute(
                "INSERT INTO deployments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    deployment_id,
                    spec["name"],
                    "queued",
                    "queued",
                    stamp,
                    stamp,
                    json.dumps(spec),
                    json.dumps(vms),
                    "[]",
                    self.seal(secret_data),
                    None,
                    parent_id,
                ),
            )
        self.event(deployment_id, "Deployment queued")

    def _decode(self, row, private=False):
        if row is None:
            return None
        result = dict(row)
        for key in ("spec", "vms", "resources"):
            result[key] = json.loads(result[key])
        encrypted = result.pop("secrets")
        if private:
            result["secrets"] = self.unseal(encrypted)
        else:
            result.pop("resources")
        return result

    def get(self, deployment_id, private=False):
        with self.connect() as c:
            row = c.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone()
        return self._decode(row, private)

    def list(self):
        with self.connect() as c:
            rows = c.execute("SELECT * FROM deployments ORDER BY created_at DESC LIMIT 500").fetchall()
        return [self._decode(row) for row in rows]

    def retained(self):
        """All live/reserved resources, independent of the history display limit."""
        with self.connect() as c:
            rows = c.execute("SELECT * FROM deployments WHERE status != 'reverted'").fetchall()
        return [self._decode(row) for row in rows]

    def update(self, deployment_id, **values):
        allowed = {"status", "stage", "vms", "resources", "secrets", "error"}
        if not values.keys() <= allowed:
            raise ValueError("Invalid deployment update")
        encoded = {
            k: self.seal(v) if k == "secrets" else json.dumps(v) if k in {"vms", "resources"} else v
            for k, v in values.items()
        }
        encoded["updated_at"] = now()
        with self.connect() as c:
            c.execute(
                "UPDATE deployments SET " + ",".join(k + "=?" for k in encoded) + " WHERE id=?",
                (*encoded.values(), deployment_id),
            )

    def event(self, deployment_id, message, level="info"):
        with self.connect() as c:
            c.execute(
                "INSERT INTO events(deployment_id,at,level,message) VALUES(?,?,?,?)",
                (deployment_id, now(), level, message),
            )

    def events(self, deployment_id):
        with self.connect() as c:
            return [
                dict(row)
                for row in c.execute(
                    "SELECT id,at,level,message FROM events WHERE deployment_id=? ORDER BY id", (deployment_id,)
                )
            ]

    def claim(self):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT id FROM deployments WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row:
                c.execute(
                    "UPDATE deployments SET status='running',stage='preflight',updated_at=? WHERE id=?", (now(), row[0])
                )
                return row[0]
        return None

    def recover(self):
        with self.connect() as c:
            rows = c.execute("SELECT id,status FROM deployments WHERE status IN ('running','cleaning')").fetchall()
        for row in rows:
            status = "cleanup_failed" if row["status"] == "cleaning" else "interrupted"
            self.update(
                row["id"],
                status=status,
                stage=status,
                error="The service stopped during this deployment. Review logs and use delete & redeploy to recover.",
            )
            self.event(
                row["id"],
                "Deployment interrupted by service restart; resources retained for deliberate cleanup.",
                "warning",
            )
