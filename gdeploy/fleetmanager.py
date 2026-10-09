from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import stat
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from .models import FLEETMANAGER_ONLINE_ONLY, FLEETMANAGER_VERSION_PATTERN

MAX_UPLOAD_BYTES = 4 * 1024**3
MAX_LICENSE_BYTES = 64 * 1024
CHUNK_BYTES = 1024**2
logger = logging.getLogger(__name__)


class FleetManagerError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _filename(value, suffix=".deb"):
    if (
        not isinstance(value, str) or not 1 <= len(value) <= 200
        or not value.isprintable() or "/" in value or "\\" in value
        or value.startswith(".") or not value.lower().endswith(suffix)
    ):
        raise FleetManagerError(f"Choose a {suffix} file with a filename containing no directory paths.")
    return value


def _checksum(value):
    value = value.strip().lower() if isinstance(value, str) else ""
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise FleetManagerError("Enter a complete SHA-256 checksum (64 hexadecimal characters), or leave it blank.")
    return value


def validate_license(pem, name="corelight-fleetd.pem"):
    """Check the PEM pair locally; Corelight validates the product entitlement in the guest."""
    if not isinstance(pem, str) or not pem.strip():
        raise FleetManagerError("Provide the Corelight Fleet Manager product identity PEM license.")
    if len(pem.encode("utf-8")) > MAX_LICENSE_BYTES:
        raise FleetManagerError("The Fleet Manager license must be no larger than 64 KiB.", 413)
    name = _filename(name or "corelight-fleetd.pem", ".pem")
    encoded = pem.encode("utf-8")
    certificates = re.findall(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", encoded, re.S)
    keys = re.findall(rb"-----BEGIN ((?:RSA |EC )?PRIVATE KEY)-----.*?-----END \1-----", encoded, re.S)
    try:
        if not certificates or len(keys) != 1:
            raise ValueError("Expected a certificate and one unencrypted private key")
        key = serialization.load_pem_private_key(encoded, password=None)
        public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        parsed = [x509.load_pem_x509_certificate(value) for value in certificates]
        certificate = parsed[0]
        if certificate.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        ) != public:
            raise FleetManagerError(
                "The first certificate must be the Fleet product identity leaf matching the private key. "
                "Obtain the correctly ordered PEM from your license provider."
            )
    except (ValueError, TypeError, NotImplementedError, UnsupportedAlgorithm) as exc:
        raise FleetManagerError("The license must contain a valid certificate and its matching unencrypted private key.") from exc
    current = datetime.now(timezone.utc)
    if not certificate.not_valid_before_utc <= current < certificate.not_valid_after_utc:
        raise FleetManagerError("The Fleet Manager license certificate is expired or not yet valid.")
    return {"name": name, "sha256": hashlib.sha256(encoded).hexdigest(), "not_after": certificate.not_valid_after_utc.isoformat()}


def _deb_metadata(path, descriptor):
    """Read Debian control fields only. No package extraction or maintainer scripts run."""
    source = f"/proc/self/fd/{descriptor}" if Path("/proc/self/fd").is_dir() else str(path)
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                ["dpkg-deb", "--field", source, "Package", "Version", "Architecture"],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                pass_fds=(descriptor,), timeout=30, check=False,
            )
            output.seek(0)
            raw = output.read(16385)
        if result.returncode or len(raw) > 16384:
            raise ValueError("Invalid control data")
        fields = {}
        for line in raw.decode("utf-8").splitlines():
            key, separator, value = line.partition(":")
            if not separator or key in fields or key not in {"Package", "Version", "Architecture"}:
                raise ValueError("Unexpected control field")
            fields[key] = value.strip()
        package, version, architecture = fields["Package"], fields["Version"], fields["Architecture"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]{0,127}", package):
            raise ValueError("Invalid package name")
        if not re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]{0,199}", version):
            raise ValueError("Invalid package version")
        if architecture not in {"amd64", "all"}:
            raise ValueError("Unsupported architecture")
        return {"package": package, "version": version, "architecture": architecture}
    except FileNotFoundError as exc:
        raise FleetManagerError("Debian package validation requires dpkg-deb. Rebuild the current GDeploy Docker image.") from exc
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        raise FleetManagerError("Choose a valid Debian .deb package for amd64 or all architectures.") from exc


class FleetManager:
    def __init__(self, db, config):
        self.db = db
        self.server_dir = config.ubuntu_iso.parent.resolve()
        self.upload_dir = config.data_dir.resolve() / "fleetmanager-packages"
        self._server_cache = {}
        self._version_lookup_lock = threading.Lock()

    @staticmethod
    def _server_id(path):
        return "server_" + hashlib.sha256(str(path).encode()).hexdigest()

    def _path(self, snapshot):
        if not isinstance(snapshot, dict):
            raise FleetManagerError("Select the Fleet Manager package in Setup.")
        path = Path(snapshot.get("path", ""))
        if snapshot.get("source") == "server":
            valid = path.parent == self.server_dir and snapshot.get("id") == self._server_id(path)
        elif snapshot.get("source") == "upload":
            identity = snapshot.get("id", "")
            valid = bool(re.fullmatch(r"upload_[0-9a-f]{32}", identity)) and (
                path.parent == self.upload_dir and path.name == identity.removeprefix("upload_") + ".deb"
            )
        else:
            valid = False
        if (
            not valid or not path.is_absolute() or path.suffix.lower() != ".deb"
            or path.is_symlink() or path.parent.is_symlink() or path.parent.resolve() != path.parent
        ):
            raise FleetManagerError("The package is outside the managed directories or is a symbolic link.")
        return path

    def _inspect_package(self, path, expected=None):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise FleetManagerError("The Debian package must be a regular file.")
                if not 0 < before.st_size <= MAX_UPLOAD_BYTES:
                    raise FleetManagerError("Choose a nonempty Debian package no larger than 4 GiB.", 413)
                digest = hashlib.sha256()
                while chunk := handle.read(CHUNK_BYTES):
                    digest.update(chunk)
                checksum = digest.hexdigest()
                if expected and not hmac.compare_digest(checksum, expected):
                    raise FleetManagerError("The Debian package SHA-256 does not match the expected checksum.")
                handle.seek(0)
                metadata = _deb_metadata(path, handle.fileno())
                after = os.fstat(handle.fileno())
            current = path.stat(follow_symlinks=False)
            def identity(info):
                return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if identity(before) != identity(after) or identity(after) != identity(current):
                raise FleetManagerError("The Debian package changed during verification. Select it again.")
            return {**metadata, "sha256": checksum, "size_bytes": after.st_size, "mtime_ns": after.st_mtime_ns}
        except OSError as exc:
            raise FleetManagerError("The Debian package is missing or unreadable. Upload or select it in Setup.") from exc

    def _package(self, path, *, name=None, source="server", package_id=None, expected=None):
        snapshot = {"id": package_id or self._server_id(path), "path": str(path), "name": name or path.name, "source": source}
        self._path(snapshot)
        return {**snapshot, **self._inspect_package(path, expected)}

    def _files(self):
        candidates = self.db.fleetmanager_files()
        try:
            paths = list(self.server_dir.iterdir())
        except OSError:
            paths = []
        for path in paths:
            if path.suffix.lower() != ".deb" or path.name.startswith("."):
                continue
            try:
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    continue
                identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                cached = self._server_cache.get(path)
                if cached and cached[0] == identity:
                    candidate = cached[1]
                else:
                    candidate = self._package(path)
                    self._server_cache[path] = (identity, candidate)
                candidates.append(candidate)
            except (OSError, FleetManagerError):
                continue
        return sorted(candidates, key=lambda item: (item["name"].lower(), item["id"]))

    def _available(self, item):
        try:
            path = self._path(item)
            info = path.stat(follow_symlinks=False)
            return (
                stat.S_ISREG(info.st_mode) and os.access(path, os.R_OK)
                and info.st_size == item.get("size_bytes") and info.st_mtime_ns == item.get("mtime_ns")
                and 0 < info.st_size <= MAX_UPLOAD_BYTES
            )
        except (OSError, FleetManagerError):
            return False

    @staticmethod
    def _settings_valid(snapshot):
        if isinstance(snapshot, dict) and snapshot.get("mode") == "offline":
            raise FleetManagerError(FLEETMANAGER_ONLINE_ONLY)
        if not isinstance(snapshot, dict) or snapshot.get("mode") != "online":
            raise FleetManagerError("Configure Fleet Manager in Setup before deployment.")
        community = snapshot.get("community_string", "")
        if not isinstance(community, str) or not 1 <= len(community) <= 4096 or not community.isprintable() or any(char in community for char in "\"'"):
            raise FleetManagerError("Enter a Fleet Manager community string of 1–4096 printable characters without quotes.")
        token = snapshot.get("repository_token", "")
        if token and (not isinstance(token, str) or len(token) > 4096 or not token.isascii() or not token.isprintable() or any(char.isspace() or char == ":" for char in token)):
            raise FleetManagerError("The repository token must contain at most 4096 ASCII characters without whitespace, controls or colons.")
        if not token:
            raise FleetManagerError("Online Fleet Manager installation requires a Corelight repository token.")
        version = snapshot.get("online_version", "")
        if (
            not isinstance(version, str) or len(version) > 128
            or (version and not re.fullmatch(FLEETMANAGER_VERSION_PATTERN, version))
        ):
            raise FleetManagerError(
                "Enter an exact Debian package version of at most 128 characters without spaces or wildcards, "
                "or leave it blank for latest."
            )
        license_info = validate_license(snapshot.get("license_pem"), snapshot.get("license_name"))
        if snapshot.get("license_sha256") and snapshot["license_sha256"] != license_info["sha256"]:
            raise FleetManagerError("The saved Fleet Manager license checksum is inconsistent. Save the license again.")
        return license_info

    def selected(self):
        return self.db.fleetmanager_settings()

    def repository_versions(self, repository_token=""):
        from .fleet_repository import FleetRepositoryError, available_versions

        token = repository_token or (self.selected() or {}).get("repository_token", "")
        if not token:
            raise FleetManagerError("Enter a Fleet Manager repository token to load available versions. You can load versions before saving Setup.")
        if not isinstance(token, str) or len(token) > 4096 or not token.isascii() or not token.isprintable() or any(char.isspace() or char == ":" for char in token):
            raise FleetManagerError("The repository token must contain at most 4096 ASCII characters without whitespace, controls or colons.")
        if not self._version_lookup_lock.acquire(blocking=False):
            raise FleetManagerError("A Fleet Manager version lookup is already running. Wait for it to finish, then try again.", 409)
        try:
            return {"versions": available_versions(token)}
        except FleetRepositoryError as error:
            raise FleetManagerError(str(error), 502) from None
        finally:
            self._version_lookup_lock.release()

    def validate_snapshot(self, snapshot):
        if snapshot is None:
            return None
        self._settings_valid(snapshot)
        return {**snapshot, "package": None, "dependencies": []}

    def catalog(self, *, include_packages=True):
        state = self.db.fleetmanager_storage_state()
        saved = state["selected"]
        errors = []
        license_info = None
        if saved:
            try:
                license_info = self._settings_valid(saved)
            except FleetManagerError as exc:
                errors.append(str(exc))
                if saved.get("mode") == "offline":
                    # Retired settings remain encrypted and editable. Expose
                    # existing license metadata so online setup can reuse it.
                    try:
                        license_info = validate_license(saved.get("license_pem"), saved.get("license_name"))
                    except FleetManagerError as license_error:
                        errors.append(str(license_error))
        else:
            errors.append("Configure Fleet Manager before selecting it for deployment.")
        packages = []
        for item in self._files() if include_packages else []:
            reason = self.db.fleetmanager_delete_reason(item, saved, state["references"])
            if item["source"] != "upload":
                reason = "Server-mounted packages must be managed on the Docker host."
            try:
                self._path(item)
            except FleetManagerError:
                reason = "This package is unsafe to delete; check the GDeploy data volume."
            packages.append({
                **{key: item[key] for key in ("id", "name", "source", "sha256", "size_bytes", "package", "version", "architecture")},
                "available": self._available(item), "can_delete": reason is None, "delete_reason": reason,
            })
        return {
            "mode": saved["mode"] if saved else "online", "ready": bool(saved and not errors),
            "legacy_offline": bool(saved and saved.get("mode") == "offline"),
            "online_version": saved.get("online_version", "") if saved and saved["mode"] == "online" else "",
            "community_string_configured": bool(saved and saved.get("community_string")),
            "repository_token_configured": bool(saved and saved.get("repository_token")),
            "license": license_info, "package_id": (saved.get("package") or {}).get("id") if saved else None,
            "dependency_ids": [item["id"] for item in saved.get("dependencies", [])] if saved else [],
            "packages": packages, "errors": errors,
            "max_upload_bytes": MAX_UPLOAD_BYTES, "max_license_bytes": MAX_LICENSE_BYTES,
        }

    def save(self, payload):
        value = payload.model_dump(exclude_unset=True) if hasattr(payload, "model_dump") else dict(payload)
        if value.get("mode", "online") == "offline":
            raise FleetManagerError(FLEETMANAGER_ONLINE_ONLY)
        if "package_id" in value or "dependency_ids" in value:
            raise FleetManagerError("Debian package selection has been removed. Configure the FleetManager online repository instead.")
        previous = self.selected() or {}
        snapshot = {
            "mode": value.get("mode", "online"),
            **{key: value.get(key) or previous.get(key, "") for key in ("community_string", "repository_token", "license_pem")},
            "license_name": previous.get("license_name", "corelight-fleetd.pem"),
        }
        snapshot["online_version"] = value.get("online_version", previous.get("online_version", "") if previous.get("mode") == "online" else "")
        if value.get("license_pem"):
            snapshot["license_name"] = value.get("license_name") or "corelight-fleetd.pem"
        snapshot["package"] = None
        snapshot["dependencies"] = []
        license_info = self._settings_valid(snapshot)
        snapshot["license_sha256"] = license_info["sha256"]
        self.db.set_fleetmanager_settings(snapshot)
        return self.catalog()

    def clear(self):
        self.db.clear_fleetmanager_settings()
        return self.catalog()

    def delete(self, package_id):
        if not re.fullmatch(r"upload_[0-9a-f]{32}", package_id):
            raise FleetManagerError("Only Debian packages uploaded to GDeploy can be deleted here.", 404)

        def remove_file(item):
            path = self._path(item)
            try:
                directory = os.open(self.upload_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        raise FleetManagerError("This package is not a regular file; check the data volume.", 409)
                    os.unlink(path.name, dir_fd=directory)
                finally:
                    os.close(directory)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise FleetManagerError("Unable to delete the Debian package. Check the data volume's permissions.", 507) from exc

        if not self.db.delete_fleetmanager_package(package_id, remove_file):
            raise FleetManagerError("That uploaded Debian package no longer exists. Refresh the list.", 404)
        return self.catalog()

    async def upload(self, request, filename, sha256=None):
        # Keep the authenticated legacy endpoint actionable without consuming
        # upload bodies or creating files after offline installation retirement.
        raise FleetManagerError(
            "FleetManager Debian package uploads have been removed. Configure online repository access in Setup instead.",
            410,
        )

    def recover_uploads(self):
        if self.upload_dir.is_symlink() or self.upload_dir.resolve() != self.upload_dir:
            return
        protected = {Path(item["path"]) for item in self.db.fleetmanager_files()}
        try:
            candidates = list(self.upload_dir.iterdir())
        except FileNotFoundError:
            return
        except OSError:
            logger.warning("Unable to inspect incomplete Fleet Manager uploads; check data volume permissions")
            return
        for path in candidates:
            if path in protected or not re.fullmatch(r"[0-9a-f]{32}\.(partial|deb)", path.name):
                continue
            try:
                if stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                    path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("Unable to remove incomplete Fleet Manager upload %s", path.name)
