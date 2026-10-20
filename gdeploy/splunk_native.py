"""Read native package identity without extracting files or running maintainer scripts.

The publisher checksum approves the complete package bytes. These bounded readers
check container structure and package identity; dpkg/RPM validate and install the
payload only inside the target guest.
"""
from __future__ import annotations

import io
import bz2
import gzip
import lzma
import struct
import tarfile

import zstandard

from .os_profiles import ALMA10


FORMATS = {".tgz": "tgz", ".deb": "deb", ".rpm": "rpm"}
METADATA_LIMIT = 16 * 1024**2


class NativePackageError(ValueError):
    pass


def compatible(package_format, os_profile):
    return package_format == "tgz" or package_format == ("rpm" if os_profile == ALMA10 else "deb")


def _read(handle, length):
    if length < 0 or length > METADATA_LIMIT:
        raise NativePackageError("Splunk package metadata exceeds the supported size.")
    value = handle.read(length)
    if len(value) != length:
        raise NativePackageError("The Splunk package is truncated.")
    return value


def _control_bytes(name, payload):
    if name.endswith(".gz"):
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
            result = stream.read(METADATA_LIMIT + 1)
    elif name.endswith(".xz"):
        result = lzma.LZMADecompressor(memlimit=64 * 1024**2).decompress(payload, max_length=METADATA_LIMIT + 1)
    elif name.endswith(".bz2"):
        result = bz2.BZ2Decompressor().decompress(payload, max_length=METADATA_LIMIT + 1)
    elif name.endswith(".zst"):
        with zstandard.ZstdDecompressor(max_window_size=64 * 1024).stream_reader(io.BytesIO(payload)) as stream:
            result = stream.read(METADATA_LIMIT + 1)
    else:
        result = payload
    if len(result) > METADATA_LIMIT:
        raise NativePackageError("Debian control metadata exceeds the supported size.")
    return result


def _deb(handle):
    if _read(handle, 8) != b"!<arch>\n":
        raise NativePackageError("This file is not a Debian .deb package.")
    handle.seek(0, 2)
    total = handle.tell()
    handle.seek(8)
    members = set()
    fields = None
    while handle.tell() < total:
        header = _read(handle, 60)
        if header[58:] != b"`\n":
            raise NativePackageError("Invalid Debian package archive header.")
        name = header[:16].decode("ascii").strip().removesuffix("/")
        size = int(header[48:58])
        if size < 0 or handle.tell() + size + (size % 2) > total or name in members:
            raise NativePackageError("Invalid or truncated Debian package member.")
        members.add(name)
        start = handle.tell()
        if name == "debian-binary":
            if _read(handle, size) != b"2.0\n":
                raise NativePackageError("Unsupported Debian package version.")
        elif name in ("control.tar", "control.tar.gz", "control.tar.xz", "control.tar.bz2", "control.tar.zst"):
            # Read only the small control archive, never the multi-GiB payload.
            with tarfile.open(fileobj=io.BytesIO(_control_bytes(name, _read(handle, size))), mode="r:") as archive:
                for index, member in enumerate(archive):
                    if index > 4096:
                        raise NativePackageError("Too many Debian control entries.")
                    if member.name.removeprefix("./") == "control":
                        if fields is not None or not member.isfile() or not 0 < member.size <= 65536:
                            raise NativePackageError("Invalid Debian control metadata.")
                        reader = archive.extractfile(member)
                        text = reader.read(65537).decode("utf-8")
                        fields = {}
                        for line in text.splitlines():
                            if not line or line[0].isspace():
                                continue
                            key, separator, value = line.partition(":")
                            if not separator or key in fields:
                                raise NativePackageError("Invalid Debian control metadata.")
                            fields[key] = value.strip()
        handle.seek(start + size + (size % 2))
    data = members.intersection({"data.tar", "data.tar.gz", "data.tar.xz", "data.tar.bz2", "data.tar.zst"})
    if "debian-binary" not in members or len(data) != 1 or fields is None:
        raise NativePackageError("Missing Debian package metadata or payload; use a standard Splunk .deb download.")
    if fields.get("Package") != "splunk" or fields.get("Architecture") != "amd64":
        raise NativePackageError("Choose Splunk Enterprise .deb for Linux amd64, not a forwarder or ARM package.")


def _rpm_header(handle, *, identity=True):
    header = _read(handle, 16)
    if header[:8] != b"\x8e\xad\xe8\x01\0\0\0\0":
        raise NativePackageError("Invalid RPM package header.")
    count, size = struct.unpack(">II", header[8:])
    if count > 65536:
        raise NativePackageError("Too many RPM metadata entries.")
    index = _read(handle, count * 16)
    store = _read(handle, size)
    values = {}
    for tag, kind, offset, length in struct.iter_unpack(">IIII", index):
        if offset >= size:
            raise NativePackageError("Invalid RPM metadata offset.")
        if identity and tag in (1000, 1021, 1022, 1124):  # NAME, OS, ARCH, PAYLOADFORMAT
            if tag in values or kind != 6 or length != 1:
                raise NativePackageError("Invalid RPM package identity metadata.")
            end = store.find(b"\0", offset, min(size, offset + 256))
            if end < 0:
                raise NativePackageError("Invalid RPM package identity string.")
            values[tag] = store[offset:end].decode("ascii")
    return values


def _rpm(handle):
    lead = _read(handle, 96)
    if lead[:6] != b"\xed\xab\xee\xdb\x03\x00" or lead[6:8] != b"\0\0":
        raise NativePackageError("Choose a binary RPM package, not an RPM source package.")
    _rpm_header(handle, identity=False)  # Signature header; signatures are not executed or trusted here.
    handle.seek((-handle.tell()) % 8, 1)
    values = _rpm_header(handle)
    if (values.get(1000), values.get(1021), values.get(1022), values.get(1124)) != ("splunk", "linux", "x86_64", "cpio"):
        raise NativePackageError("Choose Splunk Enterprise .rpm for Linux x86_64, not a forwarder or ARM package.")
    if not handle.read(1):
        raise NativePackageError("The Splunk RPM package has no payload.")


def validate_native(handle, package_format):
    handle.seek(0)
    try:
        {"deb": _deb, "rpm": _rpm}[package_format](handle)
    except NativePackageError:
        raise
    except (ValueError, OSError, EOFError, tarfile.TarError, struct.error, lzma.LZMAError, zstandard.ZstdError) as exc:
        raise NativePackageError(f"Could not read the Splunk .{package_format} package; download the complete installer again.") from exc
