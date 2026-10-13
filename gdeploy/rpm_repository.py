"""Bounded Fleet Manager RPM version discovery for the experimental Alma guest.

Catalog downloads use verified HTTPS and repomd checksums. The guest's DNF
repo_gpgcheck verifies the repository signature before installing packages.
"""

from __future__ import annotations

import hashlib
import io
import re
import time
import xml.etree.ElementTree as ET
from functools import cmp_to_key

from .alma_corelight import RPM_VERSION_PATTERN
from .fleet_guest import FleetInstallError, download_repository_file
from .fleet_repository import FleetRepositoryError, _metadata_bytes

REPOSITORY_PATH = "rpm_any/rpm_any/x86_64/"
INDEX_PATH = REPOSITORY_PATH + "repodata/repomd.xml"
MAX_INDEX_BYTES = 1024**2
MAX_COMPRESSED_BYTES = 8 * 1024**2
MAX_METADATA_BYTES = 32 * 1024**2
MAX_RECORDS = 50000
MAX_VERSIONS = 10000
MAX_XML_ELEMENTS = 1000000
LOOKUP_TIMEOUT = 60
REPO_NAMESPACE = "http://linux.duke.edu/metadata/repo"
PACKAGE_NAMESPACE = "http://linux.duke.edu/metadata/common"


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FleetRepositoryError("Fleet Manager RPM version lookup timed out. Check repository access and try again.")
    return remaining


def _safe_xml(data):
    if not isinstance(data, bytes) or not data or len(data) > MAX_METADATA_BYTES or b"\x00" in data or re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.I):
        raise FleetRepositoryError("The Fleet Manager RPM repository returned invalid XML metadata.")
    return data


def _checksum(element, data):
    if element is None or element.get("type") not in {"sha256", "sha512"}:
        raise FleetRepositoryError("Fleet Manager RPM metadata requires a SHA-256 or SHA-512 checksum.")
    expected = element.text or ""
    actual = hashlib.new(element.get("type"), data).hexdigest()
    if not re.fullmatch(r"[0-9a-fA-F]+", expected) or expected.lower() != actual:
        raise FleetRepositoryError("Fleet Manager RPM package metadata does not match its repository checksum.")


def _primary_reference(index):
    malformed = "The Fleet Manager RPM repository returned malformed index metadata."
    if len(index) > MAX_INDEX_BYTES:
        raise FleetRepositoryError("The Fleet Manager RPM metadata index exceeds its allowed size.")
    try:
        root = ET.fromstring(_safe_xml(index))
        if root.tag != f"{{{REPO_NAMESPACE}}}repomd":
            raise ValueError
        matches = [row for row in root.findall(f"{{{REPO_NAMESPACE}}}data") if row.get("type") == "primary"]
        if len(matches) != 1:
            raise ValueError
        primary = matches[0]
        locations = primary.findall(f"{{{REPO_NAMESPACE}}}location")
        if len(locations) != 1:
            raise ValueError
        path = locations[0].get("href", "")
        if not re.fullmatch(r"repodata/[A-Za-z0-9_~-][A-Za-z0-9._~-]*\.(?:xml(?:\.gz|\.xz)?)", path):
            raise ValueError
        for name, maximum in (("size", MAX_COMPRESSED_BYTES if not path.endswith(".xml") else MAX_METADATA_BYTES), ("open-size", MAX_METADATA_BYTES)):
            size = primary.find(f"{{{REPO_NAMESPACE}}}{name}")
            if size is not None and (not re.fullmatch(r"[0-9]+", size.text or "") or not 0 < int(size.text) <= maximum):
                raise ValueError
        checksums = primary.findall(f"{{{REPO_NAMESPACE}}}checksum")
        open_checksums = primary.findall(f"{{{REPO_NAMESPACE}}}open-checksum")
        if len(checksums) != 1 or len(open_checksums) > 1:
            raise ValueError
    except (ET.ParseError, ValueError, TypeError):
        raise FleetRepositoryError(malformed) from None
    return path, checksums[0], open_checksums[0] if open_checksums else None


def _rpm_part_compare(left, right):
    """RPM segment ordering, including tilde prereleases and caret snapshots."""
    first = second = 0
    while first < len(left) or second < len(right):
        while first < len(left) and not left[first].isalnum() and left[first] not in "~^":
            first += 1
        while second < len(right) and not right[second].isalnum() and right[second] not in "~^":
            second += 1
        a = left[first] if first < len(left) else ""
        b = right[second] if second < len(right) else ""
        if a == "~" or b == "~":
            if a != b:
                return -1 if a == "~" else 1
            first += 1
            second += 1
            continue
        if a == "^" or b == "^":
            if not a or not b:
                return -1 if not a else 1
            if a != b:
                return -1 if a == "^" else 1
            first += 1
            second += 1
            continue
        if not a or not b:
            return (bool(a) > bool(b)) - (bool(a) < bool(b))
        numeric = a.isdigit()
        if numeric != b.isdigit():
            return 1 if numeric else -1
        first_end, second_end = first, second
        while first_end < len(left) and (left[first_end].isdigit() if numeric else left[first_end].isalpha()):
            first_end += 1
        while second_end < len(right) and (right[second_end].isdigit() if numeric else right[second_end].isalpha()):
            second_end += 1
        value_a, value_b = left[first:first_end], right[second:second_end]
        if numeric:
            value_a, value_b = value_a.lstrip("0"), value_b.lstrip("0")
            if len(value_a) != len(value_b):
                return 1 if len(value_a) > len(value_b) else -1
        if value_a != value_b:
            return 1 if value_a > value_b else -1
        first, second = first_end, second_end
    return 0


def compare_rpm_versions(left, right):
    def parts(value):
        epoch, version = value.split(":", 1)
        version_release, arch = version.rsplit(".", 1)
        version, release = version_release.split("-", 1)
        return int(epoch), version, release, arch

    first, second = parts(left), parts(right)
    if first[0] != second[0]:
        return 1 if first[0] > second[0] else -1
    return _rpm_part_compare(first[1], second[1]) or _rpm_part_compare(first[2], second[2]) or ((first[3] > second[3]) - (first[3] < second[3]))


def _parse_versions(data, deadline):
    versions, records = set(), 0
    malformed = "The Fleet Manager RPM repository returned malformed package metadata."
    try:
        events = ET.iterparse(io.BytesIO(_safe_xml(data)), events=("start", "end"))
        _, root = next(events)
        if root.tag != f"{{{PACKAGE_NAMESPACE}}}metadata":
            raise ValueError
        depth, elements = 1, 1
        for event, element in events:
            if event == "start":
                depth += 1
                elements += 1
                if depth > 32 or elements > MAX_XML_ELEMENTS:
                    raise ValueError
                if not elements % 1024:
                    _remaining(deadline)
            else:
                depth -= 1
            if event != "end" or element.tag != f"{{{PACKAGE_NAMESPACE}}}package":
                continue
            records += 1
            if records > MAX_RECORDS:
                raise FleetRepositoryError("Fleet Manager RPM metadata contains too many package records.")
            if not records % 1024:
                _remaining(deadline)
            name = element.find(f"{{{PACKAGE_NAMESPACE}}}name")
            architecture = element.find(f"{{{PACKAGE_NAMESPACE}}}arch")
            if name is None or architecture is None:
                raise ValueError
            if name.text == "corelight-fleet" and architecture.text in {"x86_64", "noarch"}:
                version_elements = element.findall(f"{{{PACKAGE_NAMESPACE}}}version")
                if len(version_elements) != 1:
                    raise ValueError
                version = version_elements[0]
                epoch = version.get("epoch", "0")
                value = f"{epoch}:{version.get('ver', '')}-{version.get('rel', '')}.{architecture.text}"
                if len(value) > 128 or not re.fullmatch(RPM_VERSION_PATTERN, value):
                    raise ValueError
                versions.add(value)
                if len(versions) > MAX_VERSIONS:
                    raise FleetRepositoryError("Fleet Manager RPM metadata contains too many versions.")
            element.clear()
        _remaining(deadline)
    except (ET.ParseError, ValueError, StopIteration, TypeError):
        raise FleetRepositoryError(malformed) from None
    if not versions:
        raise FleetRepositoryError("No Fleet Manager RPM versions for x86_64 were listed. Check repository entitlement or try again later.")
    return sorted(sorted(versions), key=cmp_to_key(compare_rpm_versions), reverse=True)


def available_versions(token):
    deadline = time.monotonic() + LOOKUP_TIMEOUT
    try:
        index = download_repository_file(token, INDEX_PATH, max_bytes=MAX_INDEX_BYTES, timeout=_remaining(deadline), resource="package metadata")
        path, checksum, open_checksum = _primary_reference(index)
        suffix = ".gz" if path.endswith(".gz") else ".xz" if path.endswith(".xz") else ""
        body = download_repository_file(token, REPOSITORY_PATH + path, max_bytes=MAX_COMPRESSED_BYTES if suffix else MAX_METADATA_BYTES, timeout=_remaining(deadline), resource="package metadata")
    except FleetInstallError as error:
        raise FleetRepositoryError(str(error)) from None
    _checksum(checksum, body)
    expanded = _metadata_bytes(body, suffix)
    if open_checksum is not None:
        _checksum(open_checksum, expanded)
    return _parse_versions(expanded, deadline)
