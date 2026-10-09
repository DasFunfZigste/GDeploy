"""Encrypted defaults and per-deployment inputs for Corelight Software Sensor."""

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit


class CorelightSensorError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def pairing_token(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or not value.isascii() or not value.isprintable() or any(c.isspace() for c in value):
        raise CorelightSensorError("Enter the unique pairing token from a new sensor record in Fleet Manager (1–4096 ASCII characters without whitespace).")
    return value


def ssl_name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 253:
        raise CorelightSensorError("Enter the server SSL name supplied by Fleet Manager, without a scheme or port.")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value) or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in value.split(".")):
        raise CorelightSensorError("Enter the server SSL name supplied by Fleet Manager, without a scheme or port.")
    return value


def fleet_url(value):
    message = "Enter the HTTPS pairing URL supplied by Fleet Manager; its default port is 1443."
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or not value.isascii() or any(c.isspace() or ord(c) < 32 for c in value):
        raise CorelightSensorError(message)
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or "\\" in value:
            raise ValueError("invalid URL")
        ssl_name(host)
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid port")
        host = f"[{host}]" if ":" in host else host
        return urlunsplit(("https", f"{host}:{port or 1443}", parsed.path.rstrip("/"), "", ""))
    except (ValueError, CorelightSensorError):
        raise CorelightSensorError(message) from None


def validate_settings(value):
    if not isinstance(value, dict):
        raise CorelightSensorError("Configure Corelight Software Sensor in Setup before deployment.")
    token = value.get("repository_token")
    if not isinstance(token, str) or not 1 <= len(token) <= 4096 or not token.isascii() or not token.isprintable() or any(c.isspace() or c == ":" for c in token):
        raise CorelightSensorError("Enter the Software Sensor repository token (ASCII without whitespace or colons).")
    for key, title, limit in (("community_string", "Fleet Manager community string", 4096), ("license_key", "Corelight sensor license key", 65536)):
        item = value.get(key)
        if not isinstance(item, str) or not 1 <= len(item) <= limit or not item.isprintable():
            raise CorelightSensorError(f"Enter the required {title} as a single line, no more than {limit} characters.")
    try:
        raw_network = value.get("api_network", "0.0.0.0/0")
        if not isinstance(raw_network, str) or "/" not in raw_network:
            raise ValueError("CIDR required")
        network = str(ipaddress.IPv4Network(raw_network, strict=False))
    except (ValueError, TypeError):
        raise CorelightSensorError("Enter an IPv4 CIDR network allowed to reach the sensor API on TCP 443.") from None
    return {key: value[key] for key in ("repository_token", "community_string", "license_key")} | {
        "fleet_url": fleet_url(value.get("fleet_url")), "server_sslname": ssl_name(value.get("server_sslname")),
        "api_network": network,
    }


class CorelightSensorManager:
    def __init__(self, db):
        self.db = db

    def selected(self):
        return self.db.corelight_sensor_settings()

    def catalog(self):
        value = self.selected() or {}
        errors = []
        try:
            validate_settings(value)
        except CorelightSensorError as exc:
            errors.append(str(exc))
        return {
            "configured": bool(value), "ready": bool(value and not errors), "errors": errors,
            "fleet_url": value.get("fleet_url", ""), "server_sslname": value.get("server_sslname", ""),
            "api_network": value.get("api_network", "0.0.0.0/0"),
            **{key + "_configured": bool(value.get(key)) for key in ("repository_token", "community_string", "license_key")},
        }

    def save(self, payload):
        changes = payload.model_dump(exclude_unset=True) if hasattr(payload, "model_dump") else dict(payload)
        if changes.keys() - {"repository_token", "community_string", "license_key", "fleet_url", "server_sslname", "api_network"}:
            raise CorelightSensorError("Only shared sensor settings can be saved; enter a fresh pairing token for each deployment.")
        previous = self.selected() or {}
        snapshot = {key: changes.get(key) or previous.get(key, "") for key in ("repository_token", "community_string", "license_key")}
        snapshot.update({key: changes.get(key, previous.get(key, "")) for key in ("fleet_url", "server_sslname")})
        snapshot["api_network"] = changes.get("api_network", previous.get("api_network", "0.0.0.0/0"))
        self.db.set_corelight_sensor_settings(validate_settings(snapshot))
        return self.catalog()

    def clear(self):
        self.db.clear_corelight_sensor_settings()
        return self.catalog()

    def snapshot(self, token):
        return {**validate_settings(self.selected()), "pairing_token": pairing_token(token)}

    def validate_snapshot(self, snapshot):
        value = validate_settings(snapshot)
        return {**value, "pairing_token": pairing_token(snapshot.get("pairing_token"))}

    def require_unused_token(self, token, *, deployment_id=None):
        token = pairing_token(token)
        if self.db.sensor_pairing_token_used(token, deployment_id=deployment_id):
            raise CorelightSensorError("This sensor pairing token was already assigned to a deployment. Create a new sensor record in Fleet Manager and use its fresh token.")
