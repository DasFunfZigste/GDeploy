"""Additional GUI accounts; the original instance administrator stays intact."""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import hash_password, verify_password

Role = Literal["administrator", "credential_editor", "credential_viewer"]


class AccountError(Exception):
    def __init__(self, message, status_code=409):
        super().__init__(message)
        self.status_code = status_code


class UserInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    username: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    role: Role = "credential_viewer"
    password: str = Field(default="", max_length=1024)
    enabled: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def valid_password(self):
        if self.password and (len(self.password) < 12 or not self.password.strip()):
            raise ValueError("Use a password of at least 12 characters, containing more than whitespace.")
        return self


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    current_password: str = Field(min_length=1, max_length=1024)
    password: str = Field(min_length=12, max_length=1024)
    password_confirm: str = Field(min_length=12, max_length=1024)

    @model_validator(mode="after")
    def confirmed(self):
        if not self.password.strip() or self.password != self.password_confirm:
            raise ValueError("Enter a nonblank password and matching confirmation.")
        return self


class Accounts:
    def __init__(self, db):
        self.db = db

    def account(self, username):
        primary = self.db.administrator()
        if primary["username"].casefold() == username.casefold():
            return {**primary, "id": "primary", "role": "administrator", "enabled": True}
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM gui_users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def public(value):
        return {**{key: value[key] for key in ("id", "username", "role")},
                "enabled": bool(value["enabled"]), "must_change_credentials": bool(value["must_change_credentials"])}

    def list(self):
        primary = self.public(self.account(self.db.administrator()["username"]))
        with self.db.connect() as c:
            rows = c.execute("SELECT * FROM gui_users ORDER BY username COLLATE NOCASE").fetchall()
        return [primary, *(self.public(dict(row)) for row in rows)]

    def save(self, payload, actor, user_id=None):
        value = payload.model_dump()
        password = value.pop("password")
        password_hash = hash_password(password) if password else None
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_admin(c, actor)
            primary = c.execute("SELECT username FROM administrator WHERE id=1").fetchone()[0]
            if value["username"].casefold() == primary.casefold() or value["username"].casefold() == "admin":
                raise AccountError("That username is reserved for the instance administrator.")
            existing = c.execute("SELECT * FROM gui_users WHERE id=?", (user_id,)).fetchone() if user_id else None
            if user_id and existing is None:
                raise AccountError("User not found.", 404)
            if existing and existing["username"] == actor:
                raise AccountError("Use Change password for your own account; another administrator must change your role or access.")
            if existing is None and not password_hash:
                raise AccountError("Enter a temporary password of at least 12 characters.", 422)
            identifier = user_id or str(uuid.uuid4())
            try:
                if existing:
                    c.execute("UPDATE gui_users SET username=?,role=?,enabled=?,password_hash=?,must_change_credentials=? WHERE id=?", (
                        value["username"], value["role"], int(value["enabled"]), password_hash or existing["password_hash"],
                        1 if password_hash else existing["must_change_credentials"], identifier))
                    c.execute("DELETE FROM sessions WHERE username=?", (existing["username"],))
                else:
                    c.execute("INSERT INTO gui_users VALUES(?,?,?,?,?,1)", (identifier, value["username"], password_hash,
                                                                           value["role"], int(value["enabled"])))
            except sqlite3.IntegrityError:
                raise AccountError("That username is already in use.") from None
            c.execute("INSERT INTO audit VALUES(datetime('now'),?)", (f"GUI account {'updated' if existing else 'created'}: {identifier}; actor={actor}",))
        return identifier

    def delete(self, user_id, actor):
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_admin(c, actor)
            row = c.execute("SELECT username FROM gui_users WHERE id=?", (user_id,)).fetchone()
            if row is None:
                raise AccountError("User not found.", 404)
            if row[0] == actor:
                raise AccountError("You cannot delete your own account.")
            c.execute("DELETE FROM sessions WHERE username=?", (row[0],))
            c.execute("DELETE FROM gui_users WHERE id=?", (user_id,))
            c.execute("INSERT INTO audit VALUES(datetime('now'),?)", (f"GUI account deleted: {user_id}; actor={actor}",))

    @staticmethod
    def _require_admin(c, actor):
        primary = c.execute("SELECT username,must_change_credentials FROM administrator WHERE id=1").fetchone()
        if primary and primary[0] == actor and not primary[1]:
            return
        row = c.execute("SELECT role,enabled,must_change_credentials FROM gui_users WHERE username=?", (actor,)).fetchone()
        if not row or row[0] != "administrator" or not row[1] or row[2]:
            raise AccountError("Administrator access is required.", 403)

    def complete_setup(self, session_token, username, password):
        password_hash = hash_password(password)
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT u.id,u.password_hash FROM gui_users u JOIN sessions s ON s.username=u.username "
                            "WHERE s.token=? AND s.expires>? AND u.username=? AND u.enabled=1 AND u.must_change_credentials=1",
                            (hashlib.sha256(session_token.encode()).hexdigest(), time.time(), username)).fetchone()
            if row is None:
                return False
            if verify_password(password, row["password_hash"]):
                raise AccountError("Choose a new password different from your temporary password.", 422)
            c.execute("UPDATE gui_users SET password_hash=?,must_change_credentials=0 WHERE id=?", (password_hash, row[0]))
            c.execute("DELETE FROM sessions WHERE username=?", (username,))
            c.execute("INSERT INTO audit VALUES(datetime('now'),?)", (f"GUI account completed password setup: {row[0]}",))
            return True

    def change_password(self, actor, payload):
        new_hash = hash_password(payload.password)
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            primary = c.execute("SELECT * FROM administrator WHERE username=?", (actor,)).fetchone()
            row = primary or c.execute("SELECT * FROM gui_users WHERE username=? AND enabled=1", (actor,)).fetchone()
            if row is None or not verify_password(payload.current_password, row["password_hash"]):
                raise AccountError("Current password is incorrect.", 403)
            if hmac.compare_digest(payload.current_password.encode(), payload.password.encode()):
                raise AccountError("Choose a different password.", 422)
            table = "administrator" if primary else "gui_users"
            c.execute(f"UPDATE {table} SET password_hash=? WHERE username=?", (new_hash, actor))
            c.execute("DELETE FROM sessions WHERE username=?", (actor,))
            c.execute("INSERT INTO audit VALUES(datetime('now'),?)", (f"GUI account changed password; actor={actor}",))
