"""Encrypted, named virtualization targets with a legacy ESXi compatibility view."""

import uuid

from .certificate_trust import CertificateTrust, certificate_endpoint


LEGACY_TARGET = "legacy-esxi"


class TargetHostError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def legacy_target(db):
    saved = db.settings()
    if not saved:
        return None
    defaults = db.deployment_defaults() or {}
    try:
        applicable = defaults.get("endpoint") == certificate_endpoint(saved["host"])[2]
    except RuntimeError:
        applicable = False
    return {**saved, "id": LEGACY_TARGET, "name": saved.get("name") or "ESXi · " + saved["host"], "hypervisor": "esxi",
            "auth_type": "password", "node": "", "iso_storage": "",
            "default_network": defaults.get("default_network", "") if applicable else ""}


def target_settings(db, host_id):
    if host_id == LEGACY_TARGET:
        result = legacy_target(db)
    else:
        result = db.target_host(host_id)
    if not result:
        raise TargetHostError("The selected virtualization host is no longer configured. Choose a target host in Setup.", 404)
    return result


def public_target(db, saved):
    return {key: saved.get(key, "") for key in ("id", "name", "hypervisor", "host", "username", "auth_type", "node", "iso_storage", "default_network")} | {
        "configured": bool(saved.get("password")),
        "certificate_trust": CertificateTrust(db).saved(saved["host"], saved["hypervisor"]),
    }


class TargetHosts:
    def __init__(self, db, service):
        self.db, self.service = db, service

    def catalog(self):
        targets = self.db.target_hosts()
        legacy = legacy_target(self.db)
        if legacy:
            targets.insert(0, legacy)
        return [public_target(self.db, value) for value in targets]

    def save(self, payload, host_id=None):
        with self.service.action_lock:
            previous = target_settings(self.db, host_id) if host_id else None
            value = payload.model_dump()
            if previous and previous["hypervisor"] != value["hypervisor"]:
                raise TargetHostError("A saved host's hypervisor type cannot change. Add a separate host instead.")
            if not value["password"]:
                same = previous and all(value.get(key) == previous.get(key) for key in ("hypervisor", "host", "username", "auth_type"))
                if not same:
                    raise TargetHostError("Supply the password or API token secret when setting or changing host authentication.")
                value["password"] = previous["password"]
            # Prevent two profiles for the same node from receiving independent
            # reservations. Aliases still require the administrator's care.
            identity = (certificate_endpoint(value["host"], value["hypervisor"])[2], value["hypervisor"], value["node"])
            all_hosts = self.db.target_hosts()
            legacy = legacy_target(self.db)
            if legacy:
                all_hosts.append(legacy)
            for item in all_hosts:
                if item["id"] == host_id:
                    continue
                try:
                    existing = (certificate_endpoint(item["host"], item["hypervisor"])[2], item["hypervisor"])
                except RuntimeError:
                    # Malformed legacy settings must remain replaceable.
                    continue
                if existing == identity[:2] and (not item.get("node") or not value["node"] or item["node"] == value["node"]):
                    raise TargetHostError("This virtualization endpoint/node is already configured. Edit its existing host entry.", 409)
            if host_id == LEGACY_TARGET:
                self.db.set_settings({key: value[key] for key in ("host", "username", "password")} | {"verify_tls": True, "name": value["name"]})
                if value["default_network"]:
                    self.db.set_deployment_defaults({"host": value["host"], "endpoint": identity[0], "default_network": value["default_network"]})
                else:
                    self.db.clear_deployment_defaults()
                saved = legacy_target(self.db)
            else:
                host_id = host_id or str(uuid.uuid4())
                self.db.set_target_host(host_id, value)
                saved = self.db.target_host(host_id)
            return public_target(self.db, saved)

    def remove(self, host_id):
        if host_id == LEGACY_TARGET:
            raise TargetHostError("The migrated ESXi connection remains available for legacy clients. Edit it or add another host.")
        with self.service.action_lock:
            target_settings(self.db, host_id)
            if any(item["spec"].get("target_host_id") == host_id and item["status"] in {"queued", "running", "stopping", "cleaning"}
                   for item in self.db.retained()):
                raise TargetHostError("This host has active deployments. Finish or stop them before removing its Setup entry.", 409)
            self.db.remove_target_host(host_id)
        return {"ok": True}
