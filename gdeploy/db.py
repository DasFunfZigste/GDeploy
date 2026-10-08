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
from .ssh_keys import normalize_ssh_public_keys


class MediaStateError(ValueError):
    """A media mutation conflicted with a selection or deployment."""


class PackageStateError(MediaStateError):
    """A package mutation conflicted with a selection or deployment."""


class DeploymentVisibilityError(ValueError):
    """A deployment cannot be hidden while it may still change resources."""


class DeploymentStopError(ValueError):
    """A deployment is no longer eligible for a stop request."""


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
                CREATE TABLE IF NOT EXISTS deployment_defaults (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS esxi_certificates (endpoint TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ssh_public_keys (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS media_settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS media_files (id TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS splunk_package_settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS splunk_package_files (id TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fleetmanager_settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fleetmanager_files (id TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                    stage TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    spec TEXT NOT NULL, vms TEXT NOT NULL, resources TEXT NOT NULL,
                    secrets TEXT NOT NULL, error TEXT, parent_id TEXT, hidden_at TEXT
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
            # Existing volumes retain every deployment and its encrypted data.
            # Serialize the schema check with another process starting up.
            conn.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(deployments)")}
            if "hidden_at" not in columns:
                conn.execute("ALTER TABLE deployments ADD COLUMN hidden_at TEXT")
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

    def deployment_defaults(self):
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM deployment_defaults WHERE id=1").fetchone()
        return self.unseal(row[0]) if row else None

    def set_deployment_defaults(self, value):
        encrypted = self.seal(value)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT OR REPLACE INTO deployment_defaults VALUES(1,?)", (encrypted,))
            connection.execute("INSERT INTO audit VALUES(?,?)", (now(), "Deployment defaults updated"))

    def clear_deployment_defaults(self):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("DELETE FROM deployment_defaults WHERE id=1").rowcount:
                connection.execute("INSERT INTO audit VALUES(?,?)", (now(), "Deployment defaults cleared"))

    def ssh_public_keys(self):
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM ssh_public_keys WHERE id=1").fetchone()
        return self.unseal(row[0]) if row else []

    def set_ssh_public_keys(self, public_keys):
        """Validate all entries before atomically saving them and their audit event."""
        normalized = normalize_ssh_public_keys(public_keys)
        encrypted = self.seal(normalized)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT OR REPLACE INTO ssh_public_keys(id,value) VALUES(1,?)", (encrypted,))
            connection.execute(
                "INSERT INTO audit VALUES(?,?)",
                (now(), f"Administrator SSH public keys updated: {len(normalized)} saved for future deployments."),
            )
        return normalized

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
        """Save a verified selection, registering newly copied/uploaded media in the same transaction."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if uploaded:
                connection.execute("INSERT INTO media_files VALUES(?,?)", (value["id"], self.seal(value)))
            else:
                self._require_registered_media(connection, value)
            connection.execute("INSERT OR REPLACE INTO media_settings VALUES(1,?)", (self.seal(value),))
            connection.execute(
                "INSERT INTO audit VALUES(?,?)",
                (now(), f"OS ISO selected: {value['name']}; SHA-256 {value['sha256']}"),
            )

    def clear_media_settings(self):
        """Remove only the saved selection; queued jobs keep their media snapshots."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            removed = connection.execute("DELETE FROM media_settings WHERE id=1").rowcount
            if removed:
                connection.execute("INSERT INTO audit VALUES(?,?)", (now(), "Saved OS ISO selection cleared"))

    def _require_registered_media(self, connection, value):
        if not value:
            return
        saved = value
        if value.get("source") in {"upload", "esxi"}:
            row = connection.execute("SELECT value FROM media_files WHERE id=?", (value.get("id"),)).fetchone()
            saved = self.unseal(row[0]) if row else None
        path = Path(value.get("path", ""))
        if (
            not saved
            or any(saved.get(key) != value.get(key) for key in ("id", "path", "source"))
            or path.is_symlink()
            or not path.is_file()
        ):
            raise MediaStateError("The OS ISO was removed or changed while this action was running. Refresh and select it again.")

    def _active_media(self, connection):
        rows = connection.execute(
            "SELECT secrets FROM deployments WHERE status IN ('queued','running','stopping','cleaning')"
        ).fetchall()
        return [media for row in rows if (media := self.unseal(row[0]).get("os_media"))]

    def media_storage_state(self):
        """Read registered media, selection and active references from one database snapshot."""
        with self.connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute("SELECT value FROM media_files ORDER BY id").fetchall()
            selected = connection.execute("SELECT value FROM media_settings WHERE id=1").fetchone()
            return {
                "items": [self.unseal(row[0]) for row in rows],
                "selected": self.unseal(selected[0]) if selected else None,
                "references": self._active_media(connection),
            }

    @staticmethod
    def media_delete_reason(value, selected, references):
        def matches(other):
            return other and (
                value["id"] == other.get("id") or value["path"] == other.get("path")
            )

        if matches(selected):
            return "Clear the saved selection or choose another OS ISO before deleting this one."
        if any(matches(reference) for reference in references):
            return "Used by a queued, running, or cleaning deployment (including pending stop requests)."
        return None

    def delete_media(self, media_id, remove_file, *, fallback_selected=None):
        """Serialize deletion with selection/import and deployment insertion, including across processes."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT value FROM media_files WHERE id=?", (media_id,)).fetchone()
            if row is None:
                return False
            value = self.unseal(row[0])
            selected = connection.execute("SELECT value FROM media_settings WHERE id=1").fetchone()
            reason = self.media_delete_reason(
                value, self.unseal(selected[0]) if selected else fallback_selected, self._active_media(connection)
            )
            if reason:
                raise MediaStateError(reason)
            remove_file(value)
            connection.execute("DELETE FROM media_files WHERE id=?", (media_id,))
            connection.execute("INSERT INTO audit VALUES(?,?)", (now(), f"Managed OS ISO deleted: {value['name']}"))
            return True

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

    def splunk_package_settings(self):
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM splunk_package_settings WHERE id=1").fetchone()
        return self.unseal(row[0]) if row else None

    def splunk_package_files(self):
        with self.connect() as connection:
            rows = connection.execute("SELECT value FROM splunk_package_files ORDER BY id").fetchall()
        return [self.unseal(row[0]) for row in rows]

    def set_splunk_package_settings(self, value, *, uploaded=False):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if uploaded:
                connection.execute("INSERT INTO splunk_package_files VALUES(?,?)", (value["id"], self.seal(value)))
            else:
                self._require_registered_splunk_package(connection, value)
            connection.execute("INSERT OR REPLACE INTO splunk_package_settings VALUES(1,?)", (self.seal(value),))
            connection.execute(
                "INSERT INTO audit VALUES(?,?)", (now(), f"Splunk package selected: {value['name']}; SHA-256 {value['sha256']}"),
            )

    def clear_splunk_package_settings(self):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            removed = connection.execute("DELETE FROM splunk_package_settings WHERE id=1").rowcount
            if removed:
                connection.execute("INSERT INTO audit VALUES(?,?)", (now(), "Saved Splunk package selection cleared"))

    def _require_registered_splunk_package(self, connection, value):
        if not value:
            return
        saved = value
        if value.get("source") == "upload":
            row = connection.execute("SELECT value FROM splunk_package_files WHERE id=?", (value.get("id"),)).fetchone()
            saved = self.unseal(row[0]) if row else None
        path = Path(value.get("path", ""))
        if (
            not saved
            or any(saved.get(key) != value.get(key) for key in ("id", "path", "source", "sha256"))
            or path.is_symlink() or not path.is_file()
        ):
            raise PackageStateError("The Splunk package was removed or changed while this action was running. Refresh and select it again.")

    def _active_splunk_packages(self, connection):
        rows = connection.execute(
            "SELECT secrets FROM deployments WHERE status IN ('queued','running','stopping','cleaning')"
        ).fetchall()
        return [package for row in rows if (package := self.unseal(row[0]).get("splunk_package"))]

    def splunk_package_storage_state(self):
        with self.connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute("SELECT value FROM splunk_package_files ORDER BY id").fetchall()
            selected = connection.execute("SELECT value FROM splunk_package_settings WHERE id=1").fetchone()
            return {
                "items": [self.unseal(row[0]) for row in rows],
                "selected": self.unseal(selected[0]) if selected else None,
                "references": self._active_splunk_packages(connection),
            }

    @staticmethod
    def splunk_package_delete_reason(value, selected, references):
        def matches(other):
            return other and (value["id"] == other.get("id") or value["path"] == other.get("path"))

        if matches(selected):
            return "Clear the saved selection or choose another Splunk package before deleting this one."
        if any(matches(reference) for reference in references):
            return "Used by a queued, running, or cleaning deployment (including pending stop requests)."
        return None

    def delete_splunk_package(self, package_id, remove_file, *, fallback_selected=None):
        """Serialize file deletion with package selection and deployment creation."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT value FROM splunk_package_files WHERE id=?", (package_id,)).fetchone()
            if row is None:
                return False
            value = self.unseal(row[0])
            selected = connection.execute("SELECT value FROM splunk_package_settings WHERE id=1").fetchone()
            reason = self.splunk_package_delete_reason(
                value, self.unseal(selected[0]) if selected else fallback_selected, self._active_splunk_packages(connection)
            )
            if reason:
                raise PackageStateError(reason)
            remove_file(value)
            connection.execute("DELETE FROM splunk_package_files WHERE id=?", (package_id,))
            connection.execute("INSERT INTO audit VALUES(?,?)", (now(), f"Uploaded Splunk package deleted: {value['name']}"))
            return True

    def fleetmanager_settings(self):
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM fleetmanager_settings WHERE id=1").fetchone()
        return self.unseal(row[0]) if row else None

    def fleetmanager_files(self):
        with self.connect() as connection:
            rows = connection.execute("SELECT value FROM fleetmanager_files ORDER BY id").fetchall()
        return [self.unseal(row[0]) for row in rows]

    @staticmethod
    def fleetmanager_packages(settings):
        if not settings or settings.get("mode") != "offline":
            return []
        return ([settings["package"]] if settings.get("package") else []) + settings.get("dependencies", [])

    def register_fleetmanager_package(self, value):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO fleetmanager_files VALUES(?,?)", (value["id"], self.seal(value)))
            connection.execute("INSERT INTO audit VALUES(?,?)", (now(), "Fleet Manager Debian package uploaded"))

    def _require_registered_fleetmanager(self, connection, settings):
        for value in self.fleetmanager_packages(settings):
            saved = value
            if value.get("source") == "upload":
                row = connection.execute("SELECT value FROM fleetmanager_files WHERE id=?", (value.get("id"),)).fetchone()
                saved = self.unseal(row[0]) if row else None
            path = Path(value.get("path", ""))
            if (
                not saved
                or any(saved.get(key) != value.get(key) for key in ("id", "path", "source", "sha256"))
                or path.is_symlink() or path.parent.is_symlink() or not path.is_file()
            ):
                raise PackageStateError("A Fleet Manager package was removed or changed during this action. Refresh and select it again.")

    def set_fleetmanager_settings(self, value):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_registered_fleetmanager(connection, value)
            connection.execute("INSERT OR REPLACE INTO fleetmanager_settings VALUES(1,?)", (self.seal(value),))
            connection.execute("INSERT INTO audit VALUES(?,?)", (now(), "Fleet Manager configuration updated"))

    def clear_fleetmanager_settings(self):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("DELETE FROM fleetmanager_settings WHERE id=1").rowcount:
                connection.execute("INSERT INTO audit VALUES(?,?)", (now(), "Fleet Manager configuration cleared"))

    def _active_fleetmanager_packages(self, connection):
        rows = connection.execute("SELECT secrets FROM deployments WHERE status IN ('queued','running','stopping','cleaning')").fetchall()
        return [item for row in rows for item in self.fleetmanager_packages(self.unseal(row[0]).get("fleetmanager"))]

    def fleetmanager_storage_state(self):
        with self.connect() as connection:
            connection.execute("BEGIN")
            selected = connection.execute("SELECT value FROM fleetmanager_settings WHERE id=1").fetchone()
            return {
                "selected": self.unseal(selected[0]) if selected else None,
                "references": self._active_fleetmanager_packages(connection),
            }

    @classmethod
    def fleetmanager_delete_reason(cls, value, selected, references):
        def matches(other):
            return value["id"] == other.get("id") or value["path"] == other.get("path")

        if any(matches(item) for item in cls.fleetmanager_packages(selected)):
            return "Clear the saved Fleet Manager configuration or choose another package before deleting this one."
        if any(matches(item) for item in references):
            return "Used by a queued, running, or cleaning deployment (including pending stop requests)."
        return None

    def delete_fleetmanager_package(self, package_id, remove_file):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT value FROM fleetmanager_files WHERE id=?", (package_id,)).fetchone()
            if row is None:
                return False
            value = self.unseal(row[0])
            selected = connection.execute("SELECT value FROM fleetmanager_settings WHERE id=1").fetchone()
            reason = self.fleetmanager_delete_reason(
                value, self.unseal(selected[0]) if selected else None, self._active_fleetmanager_packages(connection)
            )
            if reason:
                raise PackageStateError(reason)
            remove_file(value)
            connection.execute("DELETE FROM fleetmanager_files WHERE id=?", (package_id,))
            connection.execute("INSERT INTO audit VALUES(?,?)", (now(), "Uploaded Fleet Manager Debian package deleted"))
            return True

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
            c.execute("BEGIN IMMEDIATE")
            # Preflight and credential generation run before this transaction. A
            # concurrent administrator may have removed a previously selected ISO.
            self._require_registered_media(c, secret_data.get("os_media"))
            self._require_registered_splunk_package(c, secret_data.get("splunk_package"))
            self._require_registered_fleetmanager(c, secret_data.get("fleetmanager"))
            c.execute(
                """INSERT INTO deployments
                   (id,name,status,stage,created_at,updated_at,spec,vms,resources,secrets,error,parent_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
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

    def list(self, include_hidden=False):
        with self.connect() as c:
            rows = c.execute(
                "SELECT * FROM deployments "
                + ("" if include_hidden else "WHERE hidden_at IS NULL ")
                + "ORDER BY created_at DESC LIMIT 500"
            ).fetchall()
        return [self._decode(row) for row in rows]

    def set_visibility(self, deployment_id, hidden):
        """Change history visibility only; retain credentials, ownership and resources."""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone()
            if row is None:
                return None
            if hidden and row["status"] not in {"completed", "failed", "stopped", "interrupted", "cleanup_failed", "reverted"}:
                raise DeploymentVisibilityError("Only finished or stopped deployments can be hidden. Wait for this deployment to stop.")
            if (row["hidden_at"] is not None) != hidden:
                stamp = now()
                c.execute(
                    "UPDATE deployments SET hidden_at=?,updated_at=? WHERE id=?",
                    (stamp if hidden else None, stamp, deployment_id),
                )
                message = (
                    "Deployment hidden from history; VMs, installation media, credentials and logs retained."
                    if hidden else "Deployment restored to history."
                )
                c.execute(
                    "INSERT INTO events(deployment_id,at,level,message) VALUES(?,?,?,?)",
                    (deployment_id, stamp, "audit", message),
                )
                c.execute("INSERT INTO audit VALUES(?,?)", (stamp, f"{deployment_id}: {message}"))
                row = c.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone()
            return self._decode(row)

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
        if values.get("status") in {"queued", "running", "stopping", "cleaning"}:
            # Starting recovery through a hidden record must make active work visible.
            encoded["hidden_at"] = None
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

    def deployment_status(self, deployment_id):
        with self.connect() as connection:
            row = connection.execute("SELECT status FROM deployments WHERE id=?", (deployment_id,)).fetchone()
        return row[0] if row else None

    def request_stop(self, deployment_id):
        """Serialize a stop with worker claims and completion; never change finished work."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone()
            if row is None:
                return None
            previous = row["status"]
            if previous in {"stopping", "stopped"}:
                return self._decode(row)
            if previous not in {"queued", "running"}:
                raise DeploymentStopError("Only queued or running deployments can be stopped.")
            status = "stopped" if previous == "queued" else "stopping"
            stamp = now()
            connection.execute(
                "UPDATE deployments SET status=?,stage=?,updated_at=?,hidden_at=NULL WHERE id=?",
                (status, status, stamp, deployment_id),
            )
            message = (
                "Administrator stopped the queued deployment before provisioning started."
                if previous == "queued" else
                "Administrator requested a stop. Waiting for the current operation to finish safely; "
                "existing VMs will be retained and left powered as they are."
            )
            connection.execute(
                "INSERT INTO events(deployment_id,at,level,message) VALUES(?,?,?,?)",
                (deployment_id, stamp, "audit", message),
            )
            connection.execute("INSERT INTO audit VALUES(?,?)", (stamp, f"{deployment_id}: {message}"))
            return self._decode(connection.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone())

    def progress_stage(self, deployment_id, stage, message):
        """Keep a pending stop visible even if the worker races its next stage update."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stamp = now()
            changed = connection.execute(
                "UPDATE deployments SET stage=?,updated_at=? WHERE id=? AND status='running'",
                (stage, stamp, deployment_id),
            ).rowcount
            if changed:
                connection.execute(
                    "INSERT INTO events(deployment_id,at,level,message) VALUES(?,?,?,?)",
                    (deployment_id, stamp, "info", message),
                )
            return bool(changed)

    def finish(self, deployment_id, status, *, vms, resources, secrets, error=None):
        """Acknowledge a stop only after the worker has left all active operations."""
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError("Invalid worker completion status")
        encrypted = self.seal(secrets)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone()
            if row is None or row["status"] not in {"running", "stopping"}:
                return self._decode(row)
            if row["status"] == "stopping":
                status = "stopped"
            stamp = now()
            connection.execute(
                "UPDATE deployments SET status=?,stage=?,updated_at=?,vms=?,resources=?,secrets=?,error=? WHERE id=?",
                (status, status, stamp, json.dumps(vms), json.dumps(resources), encrypted, error, deployment_id),
            )
            if status == "stopped":
                if error:
                    connection.execute(
                        "INSERT INTO events(deployment_id,at,level,message) VALUES(?,?,?,?)",
                        (deployment_id, stamp, "warning", "Current operation ended with an error while stopping: " + error),
                    )
                message = (
                    "Deployment stopped. GDeploy will start no further work. Existing VMs, installation media "
                    "and credentials are retained; a guest OS installation already started may continue."
                )
                level = "audit"
                connection.execute("INSERT INTO audit VALUES(?,?)", (stamp, f"{deployment_id}: {message}"))
            elif status == "completed":
                message = "Deployment completed. Open Credentials for VM and application sign-in details."
                level = "info"
            else:
                message = error or "Deployment interrupted. Existing resources retained."
                level = "error"
            connection.execute(
                "INSERT INTO events(deployment_id,at,level,message) VALUES(?,?,?,?)",
                (deployment_id, stamp, level, message),
            )
            return self._decode(connection.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone())

    def claim(self):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT id FROM deployments WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row:
                c.execute(
                    "UPDATE deployments SET status='running',stage='preflight',hidden_at=NULL,updated_at=? WHERE id=?", (now(), row[0])
                )
                return row[0]
        return None

    def recover(self):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute("SELECT id,status FROM deployments WHERE status IN ('running','stopping','cleaning')").fetchall()
            for row in rows:
                status = "cleanup_failed" if row["status"] == "cleaning" else "interrupted"
                message = (
                    "The service restarted while a stop was pending. GDeploy will not resume this deployment. "
                    "A guest operation may still be running; inspect the retained VM before further action."
                    if row["status"] == "stopping" else
                    "Deployment interrupted by service restart; resources retained for deliberate cleanup."
                )
                stamp = now()
                c.execute(
                    "UPDATE deployments SET status=?,stage=?,error=?,updated_at=? WHERE id=?",
                    (status, status, message, stamp, row["id"]),
                )
                c.execute(
                    "INSERT INTO events(deployment_id,at,level,message) VALUES(?,?,?,?)",
                    (row["id"], stamp, "warning", message),
                )
                if row["status"] == "stopping":
                    c.execute("INSERT INTO audit VALUES(?,?)", (stamp, f"{row['id']}: {message}"))
