from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath

from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect


MAX_UPLOAD_BYTES = 16 * 1024**3
FREE_SPACE_RESERVE = 64 * 1024**2
CHUNK_BYTES = 1024**2
PROFILE = "ubuntu-autoinstall"
CHECKSUM = re.compile(r"[0-9a-f]{64}\Z")
logger = logging.getLogger(__name__)


class MediaError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _checksum(value):
    value = value.strip().lower() if isinstance(value, str) else ""
    if not CHECKSUM.fullmatch(value):
        raise MediaError("Enter the OS publisher's complete SHA-256 checksum (64 hexadecimal characters).")
    return value


def _filename(value):
    if (
        not isinstance(value, str)
        or not 5 <= len(value) <= 200
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or "/" in value
        or "\\" in value
        or value.startswith(".")
        or not value.lower().endswith(".iso")
    ):
        raise MediaError("Choose an .iso file with a filename containing no directory paths.")
    return value


class MediaManager:
    """Manage approved installer files without accepting filesystem paths from API clients."""

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self.server_dir = config.ubuntu_iso.parent.resolve()
        self.upload_dir = config.data_dir.resolve() / "media"

    def recover_uploads(self):
        """Remove abandoned upload files once at startup, before accepting requests."""
        if self.upload_dir.is_symlink() or self.upload_dir.resolve() != self.upload_dir:
            return
        protected = {Path(item["path"]) for item in self.db.media_files()}
        selected = self.db.media_settings()
        if selected:
            protected.add(Path(selected["path"]))
        try:
            candidates = list(self.upload_dir.iterdir())
        except FileNotFoundError:
            return
        except OSError:
            logger.warning("Unable to inspect incomplete uploads; check the GDeploy media directory permissions")
            return
        for path in candidates:
            if not re.fullmatch(r"[0-9a-f]{32}\.(partial|iso)", path.name) or path in protected:
                continue
            try:
                if stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                    path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                logger.warning("Unable to remove incomplete OS ISO upload %s", path.name)

    @staticmethod
    def _server_id(path):
        return "server_" + hashlib.sha256(str(path).encode()).hexdigest()

    def _snapshot(self, path, *, name=None, source="server", sha256="", media_id=None):
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise MediaError("The selected OS ISO must be a regular file, not a symbolic link.")
        return {
            "id": media_id or self._server_id(path),
            "path": str(path),
            "name": name or path.name,
            "sha256": sha256,
            "profile": PROFILE,
            "size_bytes": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "source": source,
        }

    def _path(self, snapshot):
        """Validate paths again when queued jobs use their saved media snapshot."""
        if not isinstance(snapshot, dict) or snapshot.get("profile") != PROFILE:
            raise MediaError("Select a supported OS ISO in Setup before deploying.")
        path = Path(snapshot.get("path", ""))
        source = snapshot.get("source")
        if source == "server":
            valid = path.parent == self.server_dir and snapshot.get("id") == self._server_id(path)
        elif source in {"upload", "esxi"}:
            valid = (
                path.parent == self.upload_dir
                and re.fullmatch(source + r"_[0-9a-f]{32}", snapshot.get("id", ""))
                and path.name == snapshot["id"].split("_", 1)[1] + ".iso"
            )
        else:
            valid = False
        if (
            not valid
            or not path.is_absolute()
            or path.suffix.lower() != ".iso"
            or path.is_symlink()
            or path.parent.is_symlink()
            or path.parent.resolve() != path.parent
        ):
            raise MediaError("The selected OS ISO is outside the managed media directories or is a symbolic link.")
        return path

    @staticmethod
    def _iso_signature(handle):
        handle.seek(16 * 2048)
        header = handle.read(7)
        handle.seek(0)
        if len(header) != 7 or header[1:6] != b"CD001" or header[6] != 1:
            raise MediaError("The file is not a valid ISO image. Select the original OS installer .iso file.")

    def validate_snapshot(self, snapshot):
        """Hash the selected bytes; metadata alone never approves a deployment's media."""
        path = self._path(snapshot)
        expected = _checksum(snapshot.get("sha256"))
        self._verify_iso(path, expected)
        return path

    def _verify_iso(self, path, expected):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise MediaError("The selected OS ISO must be a regular file.")
                self._iso_signature(handle)
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
                after = os.fstat(handle.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise MediaError("The OS ISO changed while it was being verified. Try selecting it again.")
                if not hmac.compare_digest(digest, expected):
                    raise MediaError("OS ISO SHA-256 does not match the publisher checksum. Download it again or check the checksum.")
            # Replacing the directory entry during verification must not approve new bytes.
            current = path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise MediaError("The OS ISO changed while it was being verified. Try selecting it again.")
        except OSError as exc:
            raise MediaError("The OS ISO is missing or unreadable. Check the media mount or upload it again.") from exc

    def selected(self):
        saved = self.db.media_settings()
        if saved:
            return saved
        return self.legacy()

    def legacy(self):
        """Return environment-configured media for deployments queued before UI selection existed."""
        path = self.server_dir / self.config.ubuntu_iso.name
        try:
            return self._snapshot(path, sha256=self.config.ubuntu_sha256)
        except (OSError, MediaError):
            return None

    def _available(self, snapshot):
        try:
            path = self._path(snapshot)
            info = path.stat(follow_symlinks=False)
            _checksum(snapshot.get("sha256"))
            return (
                stat.S_ISREG(info.st_mode)
                and os.access(path, os.R_OK)
                and info.st_size == snapshot.get("size_bytes")
                and info.st_mtime_ns == snapshot.get("mtime_ns")
            )
        except (OSError, MediaError):
            return False

    def _files(self):
        candidates = []
        try:
            for path in self.server_dir.iterdir():
                if path.suffix.lower() == ".iso" and not path.name.startswith("."):
                    try:
                        candidates.append(self._snapshot(path))
                    except (OSError, MediaError):
                        continue
        except OSError:
            pass
        for snapshot in self.db.media_files():
            try:
                path = self._path(snapshot)
                current = self._snapshot(
                    path, name=snapshot["name"], source=snapshot["source"], media_id=snapshot["id"], sha256=snapshot["sha256"]
                )
                if "origin" in snapshot:
                    current["origin"] = snapshot["origin"]
                candidates.append(current)
            except (OSError, MediaError):
                continue
        return sorted(candidates, key=lambda item: (item["name"].lower(), item["id"]))

    def catalog(self):
        saved = self.db.media_settings()
        selected = saved or self.legacy()
        ready = bool(selected and self._available(selected))
        items = []
        for item in self._files():
            chosen = bool(selected and item["id"] == selected["id"])
            checksum = selected.get("sha256", "") if chosen else item["sha256"]
            entry = {
                key: item[key] for key in ("id", "name", "size_bytes", "source")
            } | {"sha256": checksum, "selected": chosen}
            if "origin" in item:
                entry["origin"] = item["origin"]
            items.append(entry)
        return {
            "items": items, "selected": selected, "ready": ready,
            "max_upload_bytes": MAX_UPLOAD_BYTES, "profile": PROFILE,
            "has_saved_selection": saved is not None,
        }

    def clear_selection(self):
        self.db.clear_media_settings()
        return self.catalog()

    @staticmethod
    def _directory_bytes(directory, suffix=None):
        """Measure visible regular-file sizes without following links into other storage."""
        if directory.is_symlink():
            return 0
        total = 0
        for root, _, files in os.walk(directory, followlinks=False):
            for name in files:
                if suffix and not name.endswith(suffix):
                    continue
                try:
                    info = (Path(root) / name).stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode):
                        total += info.st_size
                except OSError:
                    # A worker/upload may remove a temporary file during measurement.
                    continue
        return total

    def storage(self):
        """Report the filesystem backing GDeploy data, not the inaccessible host root."""
        try:
            usage = shutil.disk_usage(self.config.data_dir)
        except OSError as exc:
            raise MediaError("Unable to measure the GDeploy data filesystem. Check its mount and permissions.", 507) from exc
        state = self.db.media_storage_state()
        selected = state["selected"] or self.legacy()
        items = []
        for saved in state["items"]:
            reason = self.db.media_delete_reason(saved, selected, state["references"])
            size = 0
            available = False
            try:
                path = self._managed_path(saved)
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise MediaError("This managed ISO is not a regular file; check the data volume.", 409)
                size = info.st_size
                available = True
            except FileNotFoundError:
                # An unselected stale registry entry can safely be removed.
                pass
            except (MediaError, OSError):
                reason = "This managed ISO is unreadable or unsafe to delete; check the data volume."
            entry = {key: saved[key] for key in ("id", "name", "source")}
            entry.update({
                "size_bytes": size, "available": available,
                "selected": bool(selected and (
                    selected.get("id") == saved["id"] or selected.get("path") == saved["path"]
                )),
                "can_delete": reason is None, "delete_reason": reason,
            })
            if "origin" in saved:
                entry["origin"] = saved["origin"]
            items.append(entry)
        return {
            "data_filesystem": {
                "path": str(self.config.data_dir),
                "label": "GDeploy data filesystem (container-visible)",
                "total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free,
            },
            "managed_iso_bytes": sum(item["size_bytes"] for item in items),
            "workspace_bytes": self._directory_bytes(self.config.data_dir / "artifacts")
            + self._directory_bytes(self.upload_dir, suffix=".partial"),
            "items": sorted(items, key=lambda item: (item["name"].lower(), item["id"])),
        }

    def _managed_path(self, snapshot):
        if snapshot.get("source") not in {"upload", "esxi"}:
            raise MediaError("Only ISO copies stored by GDeploy can be deleted here.", 409)
        return self._path(snapshot)

    def delete(self, media_id):
        if not re.fullmatch(r"(?:upload|esxi)_[0-9a-f]{32}", media_id):
            raise MediaError("Only ISO copies stored by GDeploy can be deleted here.", 404)

        def remove_file(snapshot):
            path = self._managed_path(snapshot)
            try:
                # Use an opened directory rather than resolving an arbitrary path
                # at unlink time. Never follow a replaced media directory or file.
                directory = os.open(self.upload_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        raise MediaError("This managed ISO is not a regular file; check the data volume.", 409)
                    os.unlink(path.name, dir_fd=directory)
                finally:
                    os.close(directory)
            except FileNotFoundError:
                # Remove a stale registration without touching any other file.
                pass
            except OSError as exc:
                raise MediaError("Unable to delete the managed OS ISO. Check the GDeploy data volume's permissions.", 507) from exc

        if not self.db.delete_media(media_id, remove_file, fallback_selected=self.legacy()):
            raise MediaError("That managed OS ISO no longer exists. Refresh the storage list.", 404)
        return self.storage()

    def select(self, media_id, sha256):
        checksum = _checksum(sha256)
        candidate = next((item for item in self._files() if item["id"] == media_id), None)
        if candidate is None:
            raise MediaError("That OS ISO is no longer available. Refresh the media list and select it again.", 404)
        candidate["sha256"] = checksum
        self.validate_snapshot(candidate)
        self.db.set_media_settings(candidate)
        return self.catalog()

    def import_esxi(self, client, host, datastore, remote_path, sha256):
        """Copy a shared datastore ISO locally; never mutate the source on ESXi."""
        checksum = _checksum(sha256)
        folder = str(PurePosixPath(remote_path).parent)
        listing = client.browse_iso_media(datastore, "" if folder == "." else folder)
        candidate = next((item for item in listing["files"] if item["path"] == remote_path), None)
        if candidate is None:
            raise MediaError("The selected ESXi ISO is no longer available. Refresh the folder and select it again.", 404)
        name = _filename(candidate["name"])
        size = candidate["size_bytes"]
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_UPLOAD_BYTES:
            raise MediaError("Choose a nonempty ESXi ISO no larger than 16 GiB.", 413)
        token = uuid.uuid4().hex
        partial = self.upload_dir / (token + ".partial")
        final = self.upload_dir / (token + ".iso")
        committed = False
        owns_partial = False
        owns_final = False
        try:
            self._prepare_upload(name, checksum, size)
            client.download_iso(datastore, remote_path, partial, max_bytes=MAX_UPLOAD_BYTES, expected_size=size)
            owns_partial = True
            self._verify_iso(partial, checksum)
            # An exclusive link avoids replacing any existing managed source.
            os.link(partial, final, follow_symlinks=False)
            owns_final = True
            partial.unlink()
            owns_partial = False
            snapshot = self._snapshot(final, name=name, source="esxi", sha256=checksum, media_id="esxi_" + token)
            snapshot["origin"] = {"host": host, "datastore": datastore, "path": remote_path}
            self.db.set_media_settings(snapshot, uploaded=True)
            committed = True
            return self.catalog()
        except OSError as exc:
            raise MediaError("Unable to save the ESXi ISO copy. Check the GDeploy data volume's free space and permissions.", 507) from exc
        finally:
            if owns_partial:
                partial.unlink(missing_ok=True)
            if owns_final and not committed:
                final.unlink(missing_ok=True)

    def _prepare_upload(self, filename, checksum, length):
        _filename(filename)
        _checksum(checksum)
        if self.upload_dir.is_symlink():
            raise MediaError("The upload directory must not be a symbolic link.")
        self.upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.upload_dir.resolve() != self.upload_dir:
            raise MediaError("The upload directory must not be a symbolic link.")
        if shutil.disk_usage(self.upload_dir).free < (length or 0) + FREE_SPACE_RESERVE:
            raise MediaError("Not enough disk space for this OS ISO. Free space in the GDeploy data volume.", 507)

    async def upload(self, request, filename, sha256):
        filename = _filename(filename)
        checksum = _checksum(sha256)
        raw_length = request.headers.get("content-length")
        try:
            length = int(raw_length) if raw_length is not None else None
        except ValueError as exc:
            raise MediaError("Invalid upload Content-Length.") from exc
        if length is not None and (length < 0 or length > MAX_UPLOAD_BYTES):
            raise MediaError("OS ISO uploads must be no larger than 16 GiB.", 413)
        try:
            await run_in_threadpool(self._prepare_upload, filename, checksum, length)
        except OSError as exc:
            raise MediaError("Unable to prepare uploads. Check the GDeploy data volume's free space and permissions.", 507) from exc
        token = uuid.uuid4().hex
        partial = self.upload_dir / (token + ".partial")
        final = self.upload_dir / (token + ".iso")
        committed = False
        count = 0
        digest = hashlib.sha256()

        def write_chunk(handle, chunk):
            if shutil.disk_usage(self.upload_dir).free < len(chunk) + FREE_SPACE_RESERVE:
                raise MediaError("Not enough disk space for this OS ISO. Free space in the GDeploy data volume.", 507)
            handle.write(chunk)
            digest.update(chunk)

        def finish(handle):
            handle.flush()
            os.fsync(handle.fileno())
            self._iso_signature(handle)
            if not hmac.compare_digest(digest.hexdigest(), checksum):
                raise MediaError("OS ISO SHA-256 does not match the publisher checksum. The upload was discarded.")

        try:
            descriptor = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w+b") as handle:
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > MAX_UPLOAD_BYTES:
                        raise MediaError("OS ISO uploads must be no larger than 16 GiB.", 413)
                    if length is not None and count > length:
                        raise MediaError("The upload size differs from its declared Content-Length.")
                    for offset in range(0, len(chunk), CHUNK_BYTES):
                        await run_in_threadpool(write_chunk, handle, chunk[offset:offset + CHUNK_BYTES])
                if length is not None and count != length:
                    raise MediaError("The upload was incomplete. Try uploading the OS ISO again.")
                await run_in_threadpool(finish, handle)
            partial.rename(final)
            snapshot = self._snapshot(final, name=filename, source="upload", sha256=checksum, media_id="upload_" + token)
            self.db.set_media_settings(snapshot, uploaded=True)
            committed = True
            return self.catalog()
        except ClientDisconnect as exc:
            raise MediaError("The upload was interrupted. Try uploading the OS ISO again.") from exc
        except OSError as exc:
            raise MediaError("Unable to save the OS ISO. Check the GDeploy data volume's free space and permissions.", 507) from exc
        finally:
            partial.unlink(missing_ok=True)
            if not committed:
                final.unlink(missing_ok=True)
