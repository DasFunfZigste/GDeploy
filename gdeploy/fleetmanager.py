from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

MAX_UPLOAD_BYTES = 4 * 1024**3
MAX_LICENSE_BYTES = 64 * 1024
MAX_DEPENDENCIES = 128
FREE_SPACE_RESERVE = 64 * 1024**2
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
        if not isinstance(snapshot, dict) or snapshot.get("mode") not in {"online", "offline"}:
            raise FleetManagerError("Configure Fleet Manager in Setup before deployment.")
        community = snapshot.get("community_string", "")
        if not isinstance(community, str) or not 1 <= len(community) <= 4096 or not community.isprintable() or any(char in community for char in "\"'"):
            raise FleetManagerError("Enter a Fleet Manager community string of 1–4096 printable characters without quotes.")
        token = snapshot.get("repository_token", "")
        if token and (not isinstance(token, str) or len(token) > 4096 or not token.isascii() or not token.isprintable() or any(char.isspace() or char == ":" for char in token)):
            raise FleetManagerError("The repository token must contain at most 4096 ASCII characters without whitespace, controls or colons.")
        if snapshot["mode"] == "online" and not token:
            raise FleetManagerError("Online Fleet Manager installation requires a Corelight repository token.")
        license_info = validate_license(snapshot.get("license_pem"), snapshot.get("license_name"))
        if snapshot.get("license_sha256") and snapshot["license_sha256"] != license_info["sha256"]:
            raise FleetManagerError("The saved Fleet Manager license checksum is inconsistent. Save the license again.")
        package = snapshot.get("package") if snapshot["mode"] == "offline" else None
        if snapshot["mode"] == "offline" and not package:
            raise FleetManagerError("Offline Fleet Manager installation requires a corelight-fleet amd64 .deb package.")
        if package and (package.get("package") != "corelight-fleet" or package.get("architecture") != "amd64"):
            raise FleetManagerError("Select a corelight-fleet package for amd64 as the main Fleet Manager package.")
        dependencies = snapshot.get("dependencies", []) if snapshot["mode"] == "offline" else []
        if not isinstance(dependencies, list) or len(dependencies) > MAX_DEPENDENCIES:
            raise FleetManagerError("Select no more than 128 dependency packages.")
        names = {package["package"]} if package else set()
        for item in dependencies:
            if item.get("package") == "corelight-fleet" or item.get("architecture") not in {"amd64", "all"}:
                raise FleetManagerError("Dependency packages must be amd64 or all and must not be the main corelight-fleet package.")
            if item.get("package") in names:
                raise FleetManagerError("Select only one version of each dependency package.")
            names.add(item.get("package"))
        return license_info

    def selected(self):
        return self.db.fleetmanager_settings()

    def validate_snapshot(self, snapshot):
        if snapshot is None:
            return None
        self._settings_valid(snapshot)
        if snapshot["mode"] == "online":
            snapshot = {**snapshot, "package": None, "dependencies": []}
        for item in self.db.fleetmanager_packages(snapshot):
            path = self._path(item)
            checked = self._inspect_package(path, _checksum(item.get("sha256")))
            if any(checked[key] != item.get(key) for key in ("package", "version", "architecture")):
                raise FleetManagerError("The selected Debian package metadata changed. Select it again.")
        return snapshot

    def catalog(self, *, include_packages=True):
        state = self.db.fleetmanager_storage_state()
        saved = state["selected"]
        errors = []
        license_info = None
        if saved:
            try:
                license_info = self._settings_valid(saved)
                if any(not self._available(item) for item in self.db.fleetmanager_packages(saved)):
                    errors.append("A selected Fleet Manager package is missing or changed. Select it again.")
            except FleetManagerError as exc:
                errors.append(str(exc))
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
            "community_string_configured": bool(saved and saved.get("community_string")),
            "repository_token_configured": bool(saved and saved.get("repository_token")),
            "license": license_info, "package_id": (saved.get("package") or {}).get("id") if saved else None,
            "dependency_ids": [item["id"] for item in saved.get("dependencies", [])] if saved else [],
            "packages": packages, "errors": errors,
            "max_upload_bytes": MAX_UPLOAD_BYTES, "max_license_bytes": MAX_LICENSE_BYTES,
        }

    def save(self, payload):
        value = payload.model_dump(exclude_unset=True) if hasattr(payload, "model_dump") else dict(payload)
        previous = self.selected() or {}
        snapshot = {
            "mode": value.get("mode", previous.get("mode", "online")),
            **{key: value.get(key) or previous.get(key, "") for key in ("community_string", "repository_token", "license_pem")},
            "license_name": previous.get("license_name", "corelight-fleetd.pem"),
        }
        if value.get("license_pem"):
            snapshot["license_name"] = value.get("license_name") or "corelight-fleetd.pem"
        candidates = {item["id"]: item for item in self._files()} if snapshot["mode"] == "offline" else {}

        def select(package_id):
            if not package_id:
                return None
            item = candidates.get(package_id)
            if item is None:
                raise FleetManagerError("A selected Debian package is no longer available. Refresh the package list.", 404)
            return self._package(
                self._path(item), name=item["name"], source=item["source"], package_id=item["id"],
                expected=_checksum(item["sha256"]),
            )

        package_id = value.get("package_id", (previous.get("package") or {}).get("id")) if snapshot["mode"] == "offline" else None
        dependency_ids = value.get("dependency_ids", [item["id"] for item in previous.get("dependencies", [])]) if snapshot["mode"] == "offline" else []
        if not isinstance(dependency_ids, list) or len(dependency_ids) > MAX_DEPENDENCIES or len(set(dependency_ids)) != len(dependency_ids):
            raise FleetManagerError("Choose up to 128 distinct dependency packages.")
        snapshot["package"] = select(package_id)
        snapshot["dependencies"] = [select(identity) for identity in dependency_ids]
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

    def _prepare_upload(self, length):
        if self.upload_dir.is_symlink():
            raise FleetManagerError("The package upload directory must not be a symbolic link.")
        self.upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.upload_dir.resolve() != self.upload_dir:
            raise FleetManagerError("The package upload directory must not be a symbolic link.")
        if shutil.disk_usage(self.upload_dir).free < (length or 0) + FREE_SPACE_RESERVE:
            raise FleetManagerError("Not enough disk space for the Debian package. Free space in the GDeploy data volume.", 507)

    async def upload(self, request, filename, sha256=None):
        filename = _filename(filename)
        expected = _checksum(sha256) if sha256 else None
        try:
            raw_length = request.headers.get("content-length")
            length = int(raw_length) if raw_length is not None else None
        except ValueError as exc:
            raise FleetManagerError("Invalid upload Content-Length.") from exc
        if length is not None and not 0 < length <= MAX_UPLOAD_BYTES:
            raise FleetManagerError("Choose a nonempty Debian package no larger than 4 GiB.", 413)
        try:
            await run_in_threadpool(self._prepare_upload, length)
        except OSError as exc:
            raise FleetManagerError("Unable to prepare uploads. Check the data volume's free space and permissions.", 507) from exc
        token = uuid.uuid4().hex
        partial, final = self.upload_dir / (token + ".partial"), self.upload_dir / (token + ".deb")
        committed = owns_partial = owns_final = False
        count = 0

        def write_chunk(handle, chunk):
            if shutil.disk_usage(self.upload_dir).free < len(chunk) + FREE_SPACE_RESERVE:
                raise FleetManagerError("Not enough disk space for the Debian package.", 507)
            handle.write(chunk)

        def finish(handle):
            handle.flush()
            os.fsync(handle.fileno())
            return self._inspect_package(partial, expected)

        try:
            descriptor = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            owns_partial = True
            with os.fdopen(descriptor, "w+b") as handle:
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > MAX_UPLOAD_BYTES:
                        raise FleetManagerError("Debian package uploads must be no larger than 4 GiB.", 413)
                    if length is not None and count > length:
                        raise FleetManagerError("The upload size differs from its declared Content-Length.")
                    for offset in range(0, len(chunk), CHUNK_BYTES):
                        await run_in_threadpool(write_chunk, handle, chunk[offset:offset + CHUNK_BYTES])
                if length is not None and count != length:
                    raise FleetManagerError("The upload was incomplete. Upload the Debian package again.")
                metadata = await run_in_threadpool(finish, handle)
            os.link(partial, final, follow_symlinks=False)
            owns_final = True
            partial.unlink()
            owns_partial = False
            snapshot = {"id": "upload_" + token, "path": str(final), "name": filename, "source": "upload", **metadata}
            self.db.register_fleetmanager_package(snapshot)
            committed = True
            return {**await run_in_threadpool(self.catalog), "uploaded_package_id": snapshot["id"]}
        except ClientDisconnect as exc:
            raise FleetManagerError("The upload was interrupted. Upload the Debian package again.") from exc
        except OSError as exc:
            raise FleetManagerError("Unable to save the Debian package. Check the data volume's free space and permissions.", 507) from exc
        finally:
            if owns_partial:
                partial.unlink(missing_ok=True)
            if owns_final and not committed:
                final.unlink(missing_ok=True)

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
