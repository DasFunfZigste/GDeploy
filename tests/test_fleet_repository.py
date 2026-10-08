import gzip
import lzma
import shutil
import struct
import subprocess
import time
import zlib

import pytest

from gdeploy import fleet_repository as repository
from gdeploy.fleet_guest import RepositoryDownloadError
from gdeploy.fleet_repository import FleetRepositoryError


@pytest.fixture(autouse=True)
def no_live_repository(monkeypatch):
    monkeypatch.setattr(repository, "download_repository_file", lambda *args, **kwargs: pytest.fail("No live repository requests are permitted in tests"))


def package(version="29.2.2-1", name="corelight-fleet", architecture="amd64", *, extra=""):
    return f"Package: {name}\nVersion: {version}\nArchitecture: {architecture}\nDescription: Fleet package\n continuation with UTF-8 café\n{extra}\n".encode()


def parse(data):
    return repository._parse_versions(data, time.monotonic() + 60)


def test_fetches_fixed_vendor_metadata_with_bounded_transport_and_sorts_exact_versions(monkeypatch):
    requested = []
    source = b"".join([
        package("29.2.2-9"), package("29.2.2-10"), package("29.2.2~rc1-1"),
        package("1:1.0-1"), package("30.0-1", architecture="all"), package("30.0-1"),
        package("99.0-1", architecture="arm64"), package("100.0-1", name="different-package"),
    ])

    def download(token, path, **kwargs):
        requested.append((token, path, kwargs))
        return gzip.compress(source)

    monkeypatch.setattr(repository, "download_repository_file", download)
    assert repository.available_versions("synthetic-token") == ["1:1.0-1", "30.0-1", "29.2.2-10", "29.2.2-9", "29.2.2~rc1-1"]
    assert len(requested) == 1
    token, path, options = requested[0]
    assert token == "synthetic-token" and path == "any/dists/any/main/binary-amd64/Packages.gz"
    assert options["max_bytes"] == 8 * 1024**2 and 0 < options["timeout"] <= 60
    assert options["resource"] == "package metadata"


@pytest.mark.parametrize("available_suffix", [".xz", ""])
def test_only_404_falls_back_to_alternate_package_index_formats(monkeypatch, available_suffix):
    paths = []

    def download(token, path, **kwargs):
        paths.append(path)
        if path == repository.INDEX_PATH + available_suffix:
            return lzma.compress(package()) if available_suffix else package()
        raise RepositoryDownloadError("Metadata format unavailable.", status_code=404)

    monkeypatch.setattr(repository, "download_repository_file", download)
    assert repository.available_versions("synthetic-token") == ["29.2.2-1"]
    assert paths == [repository.INDEX_PATH + suffix for suffix in ((".gz", ".xz") if available_suffix else (".gz", ".xz", ""))]


@pytest.mark.parametrize("code", [401, 403, 429, 500, None])
def test_authentication_and_transport_failures_do_not_fallback_or_echo_response_details(monkeypatch, code):
    paths = []

    def download(token, path, **kwargs):
        paths.append(path)
        raise RepositoryDownloadError("Safe repository access failure.", status_code=code)

    monkeypatch.setattr(repository, "download_repository_file", download)
    with pytest.raises(FleetRepositoryError, match="Safe repository access failure"):
        repository.available_versions("synthetic-token")
    assert paths == [repository.INDEX_PATH + ".gz"]


def test_missing_metadata_all_formats_has_actionable_error(monkeypatch):
    paths = []

    def download(token, path, **kwargs):
        paths.append(path)
        raise RepositoryDownloadError("Missing index.", status_code=404)

    monkeypatch.setattr(repository, "download_repository_file", download)
    with pytest.raises(FleetRepositoryError, match="package list was not found.*entitlement"):
        repository.available_versions("synthetic-token")
    assert len(paths) == 3


def test_shared_timeout_is_not_reset_by_format_fallback(monkeypatch):
    current, timeouts = [10.0], []
    monkeypatch.setattr(repository.time, "monotonic", lambda: current[0])

    def download(token, path, **kwargs):
        timeouts.append(kwargs["timeout"])
        current[0] += 31
        raise RepositoryDownloadError("Missing index.", status_code=404)

    monkeypatch.setattr(repository, "download_repository_file", download)
    with pytest.raises(FleetRepositoryError, match="timed out"):
        repository.available_versions("synthetic-token")
    assert timeouts == [60, 29]


@pytest.mark.parametrize("suffix,body", [(".gz", b"not gzip"), (".xz", b"not xz"), (".gz", gzip.compress(package())[:-4]), (".xz", lzma.compress(package())[:-4])])
def test_invalid_or_truncated_compressed_metadata_has_safe_error(suffix, body):
    with pytest.raises(FleetRepositoryError, match="unreadable package metadata") as caught:
        repository._metadata_bytes(body, suffix)
    assert "not gzip" not in str(caught.value) and "not xz" not in str(caught.value)


@pytest.mark.parametrize("suffix,compress", [(".gz", gzip.compress), (".xz", lzma.compress)])
def test_expansion_limit_prevents_compressed_metadata_bombs(monkeypatch, suffix, compress):
    monkeypatch.setattr(repository, "MAX_METADATA_BYTES", 1024)
    with pytest.raises(FleetRepositoryError, match="expanded.*exceeds"):
        repository._metadata_bytes(compress(b"x" * 1025), suffix)


def test_download_and_xz_dictionary_limits_are_enforced(monkeypatch):
    monkeypatch.setattr(repository, "MAX_COMPRESSED_BYTES", 8)
    with pytest.raises(FleetRepositoryError, match="download size"):
        repository._metadata_bytes(b"x" * 9, ".gz")
    monkeypatch.setattr(repository, "MAX_COMPRESSED_BYTES", 8 * 1024**2)
    large_dictionary = bytearray(lzma.compress(package()))
    block_size = (large_dictionary[12] + 1) * 4
    assert large_dictionary[14:16] == b"\x21\x01"  # One LZMA2 filter with a one-byte dictionary property.
    large_dictionary[16] = 30  # Advertise a 128 MiB dictionary without allocating one in the test.
    checksum_offset = 12 + block_size - 4
    large_dictionary[checksum_offset:checksum_offset + 4] = struct.pack("<I", zlib.crc32(large_dictionary[12:checksum_offset]))
    with pytest.raises(FleetRepositoryError, match="unreadable package metadata"):
        repository._metadata_bytes(large_dictionary, ".xz")
    monkeypatch.setattr(repository, "MAX_METADATA_BYTES", 8)
    with pytest.raises(FleetRepositoryError, match="download size"):
        repository._metadata_bytes(b"x" * 9, "")


@pytest.mark.parametrize("data", [
    b"<html>private repository error</html>", b"Package: corelight-fleet\nVersion: 29.2.2\n", b" orphan continuation\n",
    package(extra="Version: 30.0-1\n"), package("29.*"), package("29;id"), package("1" * 129),
    package().replace(b"Version: 29.2.2-1", b"Version: 29.2.2-1\n unexpected-continuation"),
    package().replace(b"Fleet package", b"private\x00content"), package().replace(b"Fleet package", b"\xff"),
    package(extra="Description2: " + "x" * 16385),
])
def test_malformed_metadata_is_rejected_without_echoing_contents(data):
    with pytest.raises(FleetRepositoryError, match="malformed package metadata") as caught:
        parse(data)
    assert "private" not in str(caught.value) and "unexpected-continuation" not in str(caught.value)


@pytest.mark.parametrize("data", [b"", b"\n\n", package(name="different-package"), package(architecture="arm64")])
def test_missing_compatible_package_versions_is_actionable(data):
    with pytest.raises(FleetRepositoryError, match="No Fleet Manager versions for amd64.*entitlement"):
        parse(data)


def test_crlf_final_record_without_blank_line_and_debian_equivalent_versions():
    source = (package("1.01") + package("1.1") + package("1.1", architecture="all")).replace(b"\n", b"\r\n").rstrip()
    assert set(parse(source)) == {"1.01", "1.1"}


def test_record_version_and_processing_limits_are_bounded(monkeypatch):
    monkeypatch.setattr(repository, "MAX_RECORDS", 1)
    with pytest.raises(FleetRepositoryError, match="too many package records"):
        parse(package() + package(name="other"))
    monkeypatch.setattr(repository, "MAX_RECORDS", 10)
    monkeypatch.setattr(repository, "MAX_VERSIONS", 1)
    with pytest.raises(FleetRepositoryError, match="too many versions"):
        parse(package("1") + package("2"))
    with pytest.raises(FleetRepositoryError, match="timed out"):
        repository._parse_versions(package(), time.monotonic() - 1)


VERSION_CASES = [
    ("1.0~rc1", "1.0", -1), ("1.0~~", "1.0~", -1), ("1.0", "1.0-0", 0),
    ("1.01", "1.1", 0), ("1.0-00", "1.0-0", 0), ("1.10", "1.9", 1),
    ("1:1.0", "2.0", 1), ("0:1.0", "1.0", 0), ("2:0", "1:999", 1),
    ("1a", "1+", -1), ("1+", "1.", -1), ("1.0-1~bpo1", "1.0-1", -1),
    ("1.0-1", "1.0-1+b1", -1), ("1.0-9", "1.0-10", -1), ("1.0A", "1.0a", -1),
    ("1.0-1-2", "1.0-1-10", -1), ("1.0+git2", "1.0+git10", -1),
    ("1.00000000000000000001", "1.1", 0),
]


@pytest.mark.parametrize("left,right,expected", VERSION_CASES)
def test_debian_version_sorting_handles_epochs_tildes_numeric_runs_and_revisions(left, right, expected):
    assert repository.compare_debian_versions(left, right) == expected
    assert repository.compare_debian_versions(right, left) == -expected
    assert repository.compare_debian_versions(left, left) == 0


@pytest.mark.skipif(not shutil.which("dpkg"), reason="Native Debian version ordering oracle runs on Linux CI")
@pytest.mark.parametrize("left,right,expected", VERSION_CASES)
def test_version_comparator_matches_native_dpkg(left, right, expected):
    operation = {-1: "lt", 0: "eq", 1: "gt"}[expected]
    result = subprocess.run(["dpkg", "--compare-versions", left, operation, right], capture_output=True, timeout=5, check=False)
    assert result.returncode == 0
    assert repository.compare_debian_versions(left, right) == expected
