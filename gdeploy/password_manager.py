"""Encrypted, independently editable connection credentials. No remote operations."""
from __future__ import annotations

import ipaddress
import re
import uuid
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

FIELDS = ("title", "application", "username", "password", "host", "port", "connection_type", "notes", "private_key")
SECRET_FIELDS = {"password", "private_key", "notes"}


class CredentialError(Exception):
    def __init__(self, message, status_code=409):
        super().__init__(message)
        self.status_code = status_code


class CredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    title: str = Field(min_length=1, max_length=160)
    application: str = Field(default="", max_length=160)
    username: str = Field(default="", max_length=256)
    password: str = Field(default="", max_length=8192)
    host: str = Field(default="", max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535, strict=True)
    connection_type: Literal["SSH", "HTTPS", "HTTP", "RDP", "VNC", "API", "OTHER"] = "SSH"
    notes: str = Field(default="", max_length=4000)
    private_key: str = Field(default="", max_length=65536)

    @field_validator("title", "application", "host", "username")
    @classmethod
    def plain_text(cls, value):
        value = value.strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Credential labels and addresses cannot contain control characters.")
        return value

    @field_validator("title")
    @classmethod
    def title_required(cls, value):
        if not value:
            raise ValueError("Enter a name for this credential.")
        return value

    @field_validator("host")
    @classmethod
    def host_only(cls, value):
        if value:
            try:
                ipaddress.ip_address(value)
            except ValueError:
                if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                           for part in value.rstrip(".").split(".")):
                    raise ValueError("Enter an IP address or hostname, without a URL, port or path.")
        return value


class CredentialUpdate(CredentialInput):
    revision: int = Field(ge=1, strict=True)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def provisioned_entries(deployment):
    """Only login material, not repository tokens, licenses or encryption keys."""
    secrets = deployment.get("secrets", {})
    credentials = secrets.get("vm_credentials", {})
    software = secrets.get("software", {})
    vms = deployment.get("vms", [])
    elastic_host = (next((vm.get("ip") or vm.get("address") for vm in vms if vm.get("role") == "elasticsearch"), "") or "").split("/", 1)[0]
    for vm in vms:
        name = vm.get("name", "")
        credential = credentials.get(name, {})
        host = (vm.get("ip") or vm.get("address") or "").split("/", 1)[0]
        origin = {"deployment_id": deployment["id"], "deployment_name": deployment.get("name", ""),
                  "vm_name": name, "vm_role": vm.get("role", ""), "source": "deployment"}

        def entry(kind, **values):
            identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, f"gdeploy:{deployment['id']}:{name}:{kind}"))
            return identifier, {**CredentialInput(title=name).model_dump(), **origin, **values}

        if credential.get("password") or credential.get("private_key"):
            yield entry("os", title=f"{name} · OS", application="Operating system", username=credential.get("username", "gdeploy"),
                        password=credential.get("password", ""), private_key=credential.get("private_key", ""), host=host,
                        port=22, connection_type="SSH")
        services = list(credential.get("services", []))
        defaults = {"elasticsearch": ("Elasticsearch", "elastic", "elastic_password", 9200),
                    "kibana": ("Kibana", "elastic", "elastic_password", 5601),
                    "splunk": ("Splunk", "admin", "splunk_password", 8000)}
        default = defaults.get(vm.get("role"))
        if default and software.get(default[2]) and not any(s.get("username") and s.get("password") for s in services):
            services.append({"name": default[0], "username": default[1], "password": software[default[2]],
                             "url": f"https://{host or 'unassigned'}:{default[3]}"})
        for service_index, service in enumerate(services):
            if not service.get("password") and not service.get("community_string"):
                continue
            url = urlsplit(service.get("url", ""))
            try:
                port = url.port or (443 if url.scheme == "https" else 80 if url.scheme == "http" else None)
            except ValueError:
                port = None
            app = service.get("name", vm.get("role", "Application"))
            yield entry(f"service:{service_index}", title=f"{name} · {app}", application=app,
                        username=service.get("username", ""), password=service.get("password", service.get("community_string", "")),
                        host=host, port=port, connection_type="API" if not service.get("password") else
                        "HTTPS" if url.scheme == "https" else "HTTP" if url.scheme == "http" else "API",
                        notes="Community string stored in the password field." if not service.get("password") else
                              "Change the application administrator password at first sign-in." if service.get("password_change_required") else "")
            if service.get("password") and service.get("community_string"):
                yield entry(f"community:{service_index}", title=f"{name} · {app} community string", application=f"{app} API",
                            password=service["community_string"], host=host, port=port, connection_type="API",
                            notes="Community string stored in the password field.")
        if vm.get("role") == "elasticsearch" and secrets.get("elastic", {}).get("service_token"):
            yield entry("kibana-token", title=f"{name} · Kibana service account", application="Kibana → Elasticsearch",
                        username="elastic/kibana", password=secrets["elastic"]["service_token"], host=elastic_host,
                        port=9200, connection_type="API", notes="Service account token; stored entry edits do not reconfigure Kibana.")


def sync_deployment(db, connection, deployment):
    """Merge discovered values, keeping user overrides and deletion tombstones."""
    for identifier, source in provisioned_entries(deployment):
        row = connection.execute("SELECT * FROM credential_entries WHERE source_key=?", (identifier,)).fetchone()
        if row and row["deleted"]:
            continue
        if row:
            previous, saved = db.unseal(row["source_value"]), db.unseal(row["value"])
            merged = {**saved, **{key: value for key, value in source.items()
                                 if key not in FIELDS or saved.get(key) == previous.get(key)}}
            if merged != saved or source != previous:
                merged["updated_at"] = stamp()
                connection.execute("UPDATE credential_entries SET value=?,source_value=?,revision=revision+1 WHERE id=?",
                                   (db.seal(merged), db.seal(source), row["id"]))
        else:
            created = stamp()
            connection.execute("INSERT INTO credential_entries VALUES(?,?,?,?,1,0)",
                               (identifier, identifier, db.seal({**source, "created_at": created, "updated_at": created}), db.seal(source)))


class PasswordManager:
    def __init__(self, db):
        self.db = db

    def _require(self, c, actor, write=False):
        primary = c.execute("SELECT username,must_change_credentials FROM administrator WHERE id=1").fetchone()
        if primary and primary[0] == actor and not primary[1]:
            return
        row = c.execute("SELECT role,enabled,must_change_credentials FROM gui_users WHERE username=?", (actor,)).fetchone()
        if not row or not row[1] or row[2] or (write and row[0] == "credential_viewer"):
            raise CredentialError("Your role does not permit this action.", 403)

    def _public(self, c, row):
        value = self.db.unseal(row["value"])
        result = {key: val for key, val in value.items() if key not in SECRET_FIELDS}
        result.update(id=row["id"], revision=row["revision"], has_password=bool(value.get("password")), has_private_key=bool(value.get("private_key")))
        if value.get("deployment_id"):
            origin = c.execute("SELECT status FROM deployments WHERE id=?", (value["deployment_id"],)).fetchone()
            result["deployment_status"] = origin[0] if origin else "unknown"
        return result

    def list(self, actor):
        with self.db.connect() as c:
            self._require(c, actor)
            return [self._public(c, row) for row in c.execute("SELECT * FROM credential_entries WHERE deleted=0 ORDER BY rowid DESC").fetchall()]

    def reveal(self, identifier, actor):
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require(c, actor)
            row = self._get(c, identifier)
            c.execute("INSERT INTO audit VALUES(?,?)", (stamp(), f"Password Manager entry revealed: {identifier}; actor={actor}"))
            return {**self.db.unseal(row["value"]), "id": identifier, "revision": row["revision"]}

    @staticmethod
    def _get(c, identifier):
        row = c.execute("SELECT * FROM credential_entries WHERE id=? AND deleted=0", (identifier,)).fetchone()
        if row is None:
            raise CredentialError("Credential entry not found.", 404)
        return row

    def save(self, payload, actor, identifier=None):
        value = payload.model_dump(exclude={"revision"})
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require(c, actor, write=True)
            if identifier:
                row = self._get(c, identifier)
                if row["revision"] != payload.revision:
                    raise CredentialError("This entry changed while you were editing. Close and reopen it to review the latest values.")
                saved = self.db.unseal(row["value"])
                c.execute("UPDATE credential_entries SET value=?,revision=revision+1 WHERE id=?",
                          (self.db.seal({**saved, **value, "updated_at": stamp()}), identifier))
            else:
                identifier = str(uuid.uuid4())
                created = stamp()
                c.execute("INSERT INTO credential_entries VALUES(?,NULL,?,NULL,1,0)",
                          (identifier, self.db.seal({**value, "source": "manual", "created_at": created, "updated_at": created})))
            c.execute("INSERT INTO audit VALUES(?,?)", (stamp(), f"Password Manager entry {'updated' if identifier and hasattr(payload, 'revision') else 'created'}: {identifier}; actor={actor}"))
            return self._public(c, self._get(c, identifier))

    def delete(self, identifier, revision, actor):
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require(c, actor, write=True)
            row = self._get(c, identifier)
            if row["revision"] != revision:
                raise CredentialError("This entry changed. Refresh before deleting it.")
            if row["source_key"]:
                c.execute("UPDATE credential_entries SET value=?,source_value=?,deleted=1,revision=revision+1 WHERE id=?",
                          (self.db.seal({}), self.db.seal({}), identifier))
            else:
                c.execute("DELETE FROM credential_entries WHERE id=?", (identifier,))
            c.execute("INSERT INTO audit VALUES(?,?)", (stamp(), f"Password Manager entry deleted: {identifier}; actor={actor}"))
