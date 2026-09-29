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

    def audit(self, action):
        with self.connect() as c:
            c.execute("INSERT INTO audit VALUES(?,?)", (now(), action))

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
