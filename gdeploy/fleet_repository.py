"""Read bounded repository metadata for choosing a Fleet Manager version.

The guest's signed APT installation remains authoritative for package integrity.
"""

from __future__ import annotations

import gzip
import io
import lzma
import re
import time
import zlib
from functools import cmp_to_key
from itertools import chain

from .fleet_guest import FleetInstallError, download_repository_file
from .models import FLEETMANAGER_VERSION_PATTERN

INDEX_PATH = "any/dists/any/main/binary-amd64/Packages"
MAX_COMPRESSED_BYTES = 8 * 1024**2
MAX_METADATA_BYTES = 32 * 1024**2
MAX_RECORDS = 50000
MAX_VERSIONS = 10000
LOOKUP_TIMEOUT = 60


class FleetRepositoryError(Exception):
    """A safe repository lookup error without request URLs or credentials."""


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FleetRepositoryError("Fleet Manager version lookup timed out. Check repository access from the GDeploy server and try again.")
    return remaining


def _metadata_bytes(data, suffix):
    maximum = MAX_COMPRESSED_BYTES if suffix else MAX_METADATA_BYTES
    if len(data) > maximum:
        raise FleetRepositoryError("The Fleet Manager repository package list exceeds the supported download size.")
    try:
        if suffix == ".gz":
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as source:
                result = source.read(MAX_METADATA_BYTES + 1)
        elif suffix == ".xz":
            source = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=64 * 1024**2)
            result = source.decompress(data, max_length=MAX_METADATA_BYTES + 1)
            if len(result) <= MAX_METADATA_BYTES and (
                not source.eof or len(source.unused_data) % 4 or any(source.unused_data)
            ):
                raise ValueError("Incomplete XZ metadata")
        else:
            result = data
    except (OSError, EOFError, ValueError, lzma.LZMAError, zlib.error):
        raise FleetRepositoryError("The Fleet Manager repository returned unreadable package metadata. Try refreshing or contact Corelight support.") from None
    if len(result) > MAX_METADATA_BYTES:
        raise FleetRepositoryError("The expanded Fleet Manager repository package list exceeds the supported size.")
    return result


def _part_compare(left, right):
    """Compare the upstream/revision parts using Debian's verrevcmp ordering."""
    first = second = 0

    def order(character):
        if character == "~":
            return -1
        if not character or character.isdigit():
            return 0
        return ord(character) if character.isalpha() else ord(character) + 256

    while first < len(left) or second < len(right):
        while (first < len(left) and not left[first].isdigit()) or (second < len(right) and not right[second].isdigit()):
            a = left[first] if first < len(left) else ""
            b = right[second] if second < len(right) else ""
            if order(a) != order(b):
                return (order(a) > order(b)) - (order(a) < order(b))
            first += bool(a)
            second += bool(b)
        while first < len(left) and left[first] == "0":
            first += 1
        while second < len(right) and right[second] == "0":
            second += 1
        first_end, second_end = first, second
        while first_end < len(left) and left[first_end].isdigit():
            first_end += 1
        while second_end < len(right) and right[second_end].isdigit():
            second_end += 1
        a, b = left[first:first_end], right[second:second_end]
        if len(a) != len(b):
            return (len(a) > len(b)) - (len(a) < len(b))
        if a != b:
            return (a > b) - (a < b)
        first, second = first_end, second_end
    return 0


def compare_debian_versions(left, right):
    def components(version):
        epoch, separator, value = version.partition(":")
        epoch, value = (int(epoch), value) if separator else (0, version)
        upstream, separator, revision = value.rpartition("-")
        return epoch, upstream if separator else value, revision if separator else "0"

    first, second = components(left), components(right)
    if first[0] != second[0]:
        return (first[0] > second[0]) - (first[0] < second[0])
    return _part_compare(first[1], second[1]) or _part_compare(first[2], second[2])


def _parse_versions(data, deadline):
    malformed = "The Fleet Manager repository returned malformed package metadata. Try refreshing or contact Corelight support."
    fields, previous, versions = {}, None, set()
    records = 0
    for number, raw_line in enumerate(chain(io.BytesIO(data), [b""])):
        if not number % 1024:
            _remaining(deadline)
        if len(raw_line) > 16386:
            raise FleetRepositoryError(malformed)
        try:
            line = raw_line.decode("utf-8").removesuffix("\n").removesuffix("\r")
        except UnicodeError:
            raise FleetRepositoryError(malformed) from None
        if any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in line):
            raise FleetRepositoryError(malformed)
        if not line:
            if fields:
                records += 1
                if records > MAX_RECORDS:
                    raise FleetRepositoryError("The Fleet Manager repository contains too many package records. Contact Corelight support.")
                if not all(fields.get(key) for key in ("package", "version", "architecture")):
                    raise FleetRepositoryError(malformed)
                if fields["package"] == "corelight-fleet" and fields["architecture"] in {"amd64", "all"}:
                    version = fields["version"]
                    if len(version) > 128 or not re.fullmatch(FLEETMANAGER_VERSION_PATTERN, version):
                        raise FleetRepositoryError(malformed)
                    versions.add(version)
                    if len(versions) > MAX_VERSIONS:
                        raise FleetRepositoryError("The Fleet Manager repository contains too many versions. Contact Corelight support.")
            fields, previous = {}, None
        elif line[0] in " \t":
            if previous is None or previous in {"package", "version", "architecture"}:
                raise FleetRepositoryError(malformed)
        else:
            name, separator, value = line.partition(":")
            name = name.lower()
            if not separator or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", name) or name in fields:
                raise FleetRepositoryError(malformed)
            fields[name], previous = value.strip(), name
    _remaining(deadline)
    if not versions:
        raise FleetRepositoryError("No Fleet Manager versions for amd64 were listed by the repository. Check the repository token's Fleet Manager entitlement or try again later.")
    # Start lexically for deterministic ordering of Debian-equivalent spellings.
    result = sorted(sorted(versions), key=cmp_to_key(compare_debian_versions), reverse=True)
    _remaining(deadline)
    return result


def available_versions(token):
    deadline = time.monotonic() + LOOKUP_TIMEOUT
    for suffix in (".gz", ".xz", ""):
        try:
            body = download_repository_file(
                token, INDEX_PATH + suffix,
                max_bytes=MAX_COMPRESSED_BYTES if suffix else MAX_METADATA_BYTES,
                timeout=_remaining(deadline), resource="package metadata",
            )
        except FleetInstallError as error:
            if getattr(error, "status_code", None) == 404:
                continue
            raise FleetRepositoryError(str(error)) from None
        _remaining(deadline)
        return _parse_versions(_metadata_bytes(body, suffix), deadline)
    raise FleetRepositoryError("The Fleet Manager repository package list was not found. Check the token's repository entitlement or contact Corelight support.")
