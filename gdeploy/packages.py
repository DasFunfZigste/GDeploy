from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import shutil
import stat
import uuid
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .guest import GuestError, _validate_splunk_archive
from .os_profiles import ALMA10, PROFILES
from .splunk_native import FORMATS, NativePackageError, compatible, validate_native


MAX_UPLOAD_BYTES = 4 * 1024**3
FREE_SPACE_RESERVE = 64 * 1024**2
CHUNK_BYTES = 1024**2
PROFILE = "splunk-enterprise-linux-x86_64"
logger = logging.getLogger(__name__)


class PackageError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _checksum(value, algorithm="sha256"):
    value = value.strip().lower() if isinstance(value, str) else ""
    length = 128 if algorithm == "sha512" else 64
    if not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        label = "SHA-512" if algorithm == "sha512" else "SHA-256"
        raise PackageError(f"Enter Splunk's complete publisher {label} checksum ({length} hexadecimal characters).")
    return value


def _publisher_checksum(sha256, sha512):
    if (sha256 is None) == (sha512 is None):
        raise PackageError("Provide one publisher checksum: SHA-512 (128 hexadecimal characters) or SHA-256 (64).")
    algorithm = "sha512" if sha512 is not None else "sha256"
    return algorithm, _checksum(sha512 if sha512 is not None else sha256, algorithm)


def _filename(value):
    if (
        not isinstance(value, str)
        or not 5 <= len(value) <= 200
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or "/" in value
        or "\\" in value
        or value.startswith(".")
        or Path(value).suffix.lower() not in FORMATS
    ):
        raise PackageError("Choose a Splunk Enterprise .tgz, .deb or .rpm package with a filename containing no directory paths.")
    return value


class SplunkPackageManager:
    """Select verified Splunk archives without accepting client-provided filesystem paths."""

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self.server_dir = config.splunk_package.parent.resolve()
        self.upload_dir = config.data_dir.resolve() / "packages"

    def recover_uploads(self):
        """Clean abandoned writes at startup; retain registered packages and unknown files."""
        if self.upload_dir.is_symlink() or self.upload_dir.resolve() != self.upload_dir:
            return
        protected = {Path(item["path"]) for item in self.db.splunk_package_files()}
        selected = self.db.splunk_package_settings()
        if selected:
            protected.add(Path(selected["path"]))
        try:
            candidates = list(self.upload_dir.iterdir())
        except FileNotFoundError:
            return
        except OSError:
            logger.warning("Unable to inspect incomplete Splunk uploads; check the GDeploy packages directory permissions")
            return
        for path in candidates:
            if not re.fullmatch(r"[0-9a-f]{32}\.(partial|tgz|deb|rpm)", path.name) or path in protected:
                continue
            try:
                if stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                    path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                logger.warning("Unable to remove incomplete Splunk package upload %s", path.name)

    @staticmethod
    def _server_id(path):
        return "server_" + hashlib.sha256(str(path).encode()).hexdigest()

    def _snapshot(self, path, *, name=None, source="server", sha256="", sha512="", package_id=None):
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise PackageError("The selected Splunk package must be a regular file, not a symbolic link.")
        return {
            "id": package_id or self._server_id(path),
            "path": str(path), "name": name or path.name, "source": source,
            "sha256": sha256, "profile": PROFILE,
            "format": FORMATS[path.suffix.lower()],
            **({"sha512": sha512} if sha512 else {}),
            "size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
        }

    def _path(self, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("profile") != PROFILE:
            raise PackageError("Select a Splunk Enterprise Linux x86_64 .tgz, .deb or .rpm package in Setup → Software packages.")
        path = Path(snapshot.get("path", ""))
        source = snapshot.get("source")
        if source == "server":
            valid = path.parent == self.server_dir and snapshot.get("id") == self._server_id(path)
        elif source == "upload":
            valid = (
                path.parent == self.upload_dir
                and re.fullmatch(r"upload_[0-9a-f]{32}", snapshot.get("id", ""))
                and path.name == snapshot["id"].split("_", 1)[1] + path.suffix.lower()
            )
        else:
            valid = False
        if (
            not valid or not path.is_absolute() or path.suffix.lower() not in FORMATS
            or snapshot.get("format", FORMATS.get(path.suffix.lower())) != FORMATS.get(path.suffix.lower())
            or path.is_symlink() or path.parent.is_symlink() or path.parent.resolve() != path.parent
        ):
            raise PackageError("The Splunk package is outside the managed package directories or is a symbolic link.")
        return path

    def validate_snapshot(self, snapshot, os_profile=None):
        path = self._path(snapshot)
        if os_profile is not None and not compatible(FORMATS[path.suffix.lower()], os_profile):
            raise PackageError("The saved Splunk package does not match this VM's OS. Select .deb for Ubuntu, .rpm for AlmaLinux, or .tgz for either in Setup → Software packages.")
        self._verify_package(path, _checksum(snapshot.get("sha256")))
        return path

    @staticmethod
    def _verify_package(path, expected, *, algorithm="sha256", package_format=None):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise PackageError("The selected Splunk package must be a regular file.")
                if before.st_size <= 0 or before.st_size > MAX_UPLOAD_BYTES:
                    raise PackageError("Choose a nonempty Splunk package no larger than 4 GiB.", 413)
                # Compute the publisher hash and the deployment SHA-256 from the
                # same bytes without loading multi-gigabyte archives into memory.
                hashers = {name: hashlib.new(name) for name in {"sha256", algorithm}}
                while chunk := handle.read(CHUNK_BYTES):
                    for hasher in hashers.values():
                        hasher.update(chunk)
                digests = {name: hasher.hexdigest() for name, hasher in hashers.items()}
                after = os.fstat(handle.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise PackageError("The Splunk package changed during verification. Select it again.")
                if not hmac.compare_digest(digests[algorithm], expected):
                    label = "SHA-512" if algorithm == "sha512" else "SHA-256"
                    raise PackageError(f"Splunk package {label} does not match the publisher checksum. Download it again or check the checksum.")
                handle.seek(0)
                package_format = package_format or FORMATS.get(path.suffix.lower(), "tgz")
                if package_format == "tgz":
                    _validate_splunk_archive(path, fileobj=handle)
                else:
                    validate_native(handle, package_format)
                validated = os.fstat(handle.fileno())
                if (after.st_size, after.st_mtime_ns) != (validated.st_size, validated.st_mtime_ns):
                    raise PackageError("The Splunk package changed during verification. Select it again.")
            current = path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise PackageError("The Splunk package changed during verification. Select it again.")
            return digests
        except (GuestError, NativePackageError) as exc:
            raise PackageError(str(exc)) from exc
        except EOFError as exc:
            raise PackageError("The Splunk .tgz package is incomplete. Download it again from Splunk.") from exc
        except OSError as exc:
            raise PackageError("The Splunk package is missing or unreadable. Select or upload it in Setup → Software packages.") from exc

    def selected(self, os_profile=None):
        if os_profile is None:
            return self.db.splunk_package_settings() or self.legacy()
        return self._resolve(os_profile, self.db.splunk_package_format_settings(), self.legacy())

    @staticmethod
    def _resolve(os_profile, selections, legacy):
        native = "rpm" if os_profile == ALMA10 else "deb"
        if native in selections:
            return selections[native]
        if legacy and Path(legacy["path"]).suffix.lower() == "." + native:
            return legacy
        return selections.get("tgz") or (legacy if legacy and Path(legacy["path"]).suffix.lower() == ".tgz" else None)

    @staticmethod
    def _require_profile(package_format, os_profile):
        if os_profile is not None and (os_profile not in PROFILES or not compatible(package_format, os_profile)):
            raise PackageError("Select .deb for Ubuntu, .rpm for AlmaLinux, or .tgz for either operating system.")

    def legacy(self):
        """Keep explicit environment configuration and pre-selection deployments working."""
        path = self.server_dir / self.config.splunk_package.name
        try:
            return self._snapshot(path, sha256=self.config.splunk_sha256)
        except (OSError, PackageError, KeyError):
            return None

    def _available(self, snapshot):
        try:
            path = self._path(snapshot)
            info = path.stat(follow_symlinks=False)
            _checksum(snapshot.get("sha256"))
            return (
                stat.S_ISREG(info.st_mode) and os.access(path, os.R_OK)
                and 0 < info.st_size <= MAX_UPLOAD_BYTES
                and info.st_size == snapshot.get("size_bytes") and info.st_mtime_ns == snapshot.get("mtime_ns")
            )
        except (OSError, PackageError):
            return False

    def _files(self):
        candidates = []
        try:
            for path in self.server_dir.iterdir():
                if path.suffix.lower() in FORMATS and not path.name.startswith("."):
                    try:
                        candidates.append(self._snapshot(path))
                    except (OSError, PackageError):
                        continue
        except OSError:
            pass
        # Include stale upload registrations so an administrator can remove them.
        candidates.extend(self.db.splunk_package_files())
        return sorted(candidates, key=lambda item: (item["name"].lower(), item["id"]))

    def catalog(self, os_profile=None):
        state = self.db.splunk_package_storage_state()
        saved = state["selected"]
        legacy = self.legacy()
        selections = state["format_selections"]
        selected = (saved or legacy) if os_profile is None else self._resolve(os_profile, selections, legacy)
        profiles = {}
        for profile in PROFILES:
            choice = self._resolve(profile, selections, legacy)
            profiles[profile] = {"ready": bool(choice and self._available(choice)),
                                 "selected": {key: choice.get(key) for key in ("id", "name", "format")} if choice else None}
        items = []
        for item in self._files():
            chosen = bool(selected and item["id"] == selected["id"])
            reason = self.db.splunk_package_delete_reason(item, selected, state["references"], selections=selections.values())
            if item["source"] != "upload":
                reason = "Server-mounted packages must be managed on the Docker host."
            available = False
            try:
                path = self._path(item)
                info = path.stat(follow_symlinks=False)
                available = stat.S_ISREG(info.st_mode) and os.access(path, os.R_OK)
                if not stat.S_ISREG(info.st_mode):
                    reason = "This package is not a regular file; check the GDeploy data volume."
            except FileNotFoundError:
                pass
            except (PackageError, OSError):
                reason = "This package is unreadable or unsafe to delete; check the GDeploy data volume."
            items.append({
                **{key: item[key] for key in ("id", "name", "size_bytes", "source")},
                "format": item.get("format", "tgz"),
                "sha256": selected.get("sha256", "") if chosen else item["sha256"],
                "sha512": selected.get("sha512", "") if chosen else item.get("sha512", ""),
                "selected": chosen, "available": available,
                "saved_default": any(choice["id"] == item["id"] for choice in selections.values()),
                "can_delete": reason is None, "delete_reason": reason,
            })
        return {
            "items": items, "selected": selected, "ready": bool(selected and self._available(selected)),
            "max_upload_bytes": MAX_UPLOAD_BYTES, "profile": PROFILE, "has_saved_selection": bool(selections),
            "os_profile": os_profile, "profile_selections": profiles,
        }

    def select(self, package_id, sha256=None, *, sha512=None, os_profile=None):
        algorithm, checksum = _publisher_checksum(sha256, sha512)
        candidate = next((item for item in self._files() if item["id"] == package_id), None)
        if candidate is None:
            raise PackageError("That Splunk package is no longer available. Refresh and select it again.", 404)
        path = self._path(candidate)
        self._require_profile(FORMATS[path.suffix.lower()], os_profile)
        digests = self._verify_package(path, checksum, algorithm=algorithm)
        candidate = self._snapshot(
            path, name=candidate["name"], source=candidate["source"], package_id=candidate["id"], **digests
        )
        self.db.set_splunk_package_settings(candidate)
        return self.catalog(os_profile)

    def clear_selection(self, package_format=None):
        self.db.clear_splunk_package_settings(package_format)
        return self.catalog()

    def delete(self, package_id):
        if not re.fullmatch(r"upload_[0-9a-f]{32}", package_id):
            raise PackageError("Only Splunk packages uploaded to GDeploy can be deleted here.", 404)

        def remove_file(snapshot):
            if snapshot.get("source") != "upload":
                raise PackageError("Only Splunk packages uploaded to GDeploy can be deleted here.", 409)
            path = self._path(snapshot)
            try:
                directory = os.open(self.upload_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        raise PackageError("This package is not a regular file; check the GDeploy data volume.", 409)
                    os.unlink(path.name, dir_fd=directory)
                finally:
                    os.close(directory)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise PackageError("Unable to delete the Splunk package. Check the GDeploy data volume's permissions.", 507) from exc

        if not self.db.delete_splunk_package(package_id, remove_file, fallback_selected=self.legacy()):
            raise PackageError("That uploaded Splunk package no longer exists. Refresh the package list.", 404)
        return self.catalog()

    def _prepare_upload(self, length):
        if self.upload_dir.is_symlink():
            raise PackageError("The package upload directory must not be a symbolic link.")
        self.upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.upload_dir.resolve() != self.upload_dir:
            raise PackageError("The package upload directory must not be a symbolic link.")
        if shutil.disk_usage(self.upload_dir).free < (length or 0) + FREE_SPACE_RESERVE:
            raise PackageError("Not enough disk space for this Splunk package. Free space in the GDeploy data volume.", 507)

    async def upload(self, request, filename, sha256=None, *, sha512=None, os_profile=None):
        filename = _filename(filename)
        self._require_profile(FORMATS[Path(filename).suffix.lower()], os_profile)
        algorithm, checksum = _publisher_checksum(sha256, sha512)
        raw_length = request.headers.get("content-length")
        try:
            length = int(raw_length) if raw_length is not None else None
        except ValueError as exc:
            raise PackageError("Invalid upload Content-Length.") from exc
        if length is not None and not 0 < length <= MAX_UPLOAD_BYTES:
            raise PackageError("Choose a nonempty Splunk package no larger than 4 GiB.", 413)
        try:
            await run_in_threadpool(self._prepare_upload, length)
        except OSError as exc:
            raise PackageError("Unable to prepare uploads. Check the GDeploy data volume's free space and permissions.", 507) from exc
        token = uuid.uuid4().hex
        partial = self.upload_dir / (token + ".partial")
        final = self.upload_dir / (token + Path(filename).suffix.lower())
        committed = owns_partial = owns_final = False
        count = 0

        def write_chunk(handle, chunk):
            if shutil.disk_usage(self.upload_dir).free < len(chunk) + FREE_SPACE_RESERVE:
                raise PackageError("Not enough disk space for this Splunk package. Free space in the GDeploy data volume.", 507)
            handle.write(chunk)

        def finish(handle):
            handle.flush()
            os.fsync(handle.fileno())
            return self._verify_package(partial, checksum, algorithm=algorithm, package_format=FORMATS[Path(filename).suffix.lower()])

        try:
            descriptor = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            owns_partial = True
            with os.fdopen(descriptor, "w+b") as handle:
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > MAX_UPLOAD_BYTES:
                        raise PackageError("Splunk package uploads must be no larger than 4 GiB.", 413)
                    if length is not None and count > length:
                        raise PackageError("The upload size differs from its declared Content-Length.")
                    for offset in range(0, len(chunk), CHUNK_BYTES):
                        await run_in_threadpool(write_chunk, handle, chunk[offset:offset + CHUNK_BYTES])
                if length is not None and count != length:
                    raise PackageError("The upload was incomplete. Try uploading the Splunk package again.")
                digests = await run_in_threadpool(finish, handle)
            # Linking exclusively prevents UUID collisions from replacing existing packages.
            os.link(partial, final, follow_symlinks=False)
            owns_final = True
            partial.unlink()
            owns_partial = False
            snapshot = self._snapshot(final, name=filename, source="upload", package_id="upload_" + token, **digests)
            self.db.set_splunk_package_settings(snapshot, uploaded=True)
            committed = True
            return self.catalog(os_profile)
        except ClientDisconnect as exc:
            raise PackageError("The upload was interrupted. Try uploading the Splunk package again.") from exc
        except OSError as exc:
            raise PackageError("Unable to save the Splunk package. Check the GDeploy data volume's free space and permissions.", 507) from exc
        finally:
            if owns_partial:
                partial.unlink(missing_ok=True)
            if owns_final and not committed:
                final.unlink(missing_ok=True)
