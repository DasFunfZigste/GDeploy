"""Native Splunk upload/OS selection and guest integrity regressions.

Fixtures contain synthetic package metadata, never vendor code or a license.
"""
import asyncio
import hashlib
import io
import json
import re
import struct
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import Mock

import pytest
import zstandard

from gdeploy import guest
from gdeploy.alma_apps import application_script
from gdeploy.db import Database, PackageStateError
from gdeploy.os_profiles import ALMA10, UBUNTU
from gdeploy.packages import PackageError, SplunkPackageManager
from gdeploy.splunk_native import NativePackageError, validate_native


def deb_bytes(name="splunk", arch="amd64", compression="gz"):
    control = io.BytesIO()
    with tarfile.open(fileobj=control, mode="w:" + ("" if compression == "zst" else compression)) as archive:
        body = f"Package: {name}\nVersion: 10.0.0\nArchitecture: {arch}\nDescription: Synthetic test\n".encode()
        info = tarfile.TarInfo("./control")
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
    compressed = zstandard.ZstdCompressor().compress(control.getvalue()) if compression == "zst" else control.getvalue()
    result = b"!<arch>\n"
    for label, body in [("debian-binary", b"2.0\n"), ("control.tar." + compression, compressed), ("data.tar.gz", b"synthetic payload")]:
        result += f"{label + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(body):<10}`\n".encode() + body
        if len(body) % 2:
            result += b"\n"
    return result


def rpm_header(fields):
    index, store = b"", b""
    for tag, value in fields.items():
        raw = value.encode() + b"\0"
        index += struct.pack(">IIII", tag, 6, len(store), 1)
        store += raw
    return b"\x8e\xad\xe8\x01\0\0\0\0" + struct.pack(">II", len(fields), len(store)) + index + store


def rpm_bytes(name="splunk", arch="x86_64"):
    lead = b"\xed\xab\xee\xdb\x03\x00\0\0" + b"\0" * 88
    # RPM signatures use tag 1000 with an integer type, unlike the identity header.
    signature = b"\x8e\xad\xe8\x01\0\0\0\0" + struct.pack(">II", 1, 4) + struct.pack(">IIII", 1000, 4, 0, 1) + b"\0" * 4
    prefix = lead + signature
    prefix += b"\0" * ((-len(prefix)) % 8)
    return prefix + rpm_header({1000: name, 1021: "linux", 1022: arch, 1124: "cpio"}) + b"synthetic payload"


class Request:
    headers = {}

    def __init__(self, body):
        self.body = body

    async def stream(self):
        yield self.body[:101]
        yield self.body[101:]


@pytest.fixture
def manager(config):
    return SplunkPackageManager(Database(config.data_dir, config.secret_key), config)


@pytest.mark.parametrize("fmt,body,profile", [("deb", deb_bytes(), UBUNTU), ("rpm", rpm_bytes(), ALMA10)], ids=lambda value: "package" if isinstance(value, bytes) else str(value))
@pytest.mark.parametrize("algorithm", ["sha256", "sha512"], ids=lambda value: "package" if isinstance(value, bytes) else str(value))
def test_native_upload_persistence_selection_protection_and_deletion(manager, config, spec, fmt, body, profile, algorithm):
    name = f"splunk-10.0.0-build-linux.{fmt}"
    selected = asyncio.run(manager.upload(Request(body), name, **{algorithm: hashlib.new(algorithm, body).hexdigest()}))["selected"]
    assert selected["format"] == fmt and selected["name"] == name
    assert Path(selected["path"]).suffix == "." + fmt
    assert manager.validate_snapshot(selected, profile).read_bytes() == body
    reopened = SplunkPackageManager(Database(config.data_dir, config.secret_key), config)
    assert reopened.selected() == selected
    manager.db.create("queued-native", spec, {"splunk_package": selected})
    manager.clear_selection()
    with pytest.raises(PackageStateError, match="queued"):
        manager.delete(selected["id"])
    manager.db.update("queued-native", status="completed")
    assert manager.delete(selected["id"])["items"] == []


@pytest.mark.parametrize("fmt,body,wrong_profile", [("deb", deb_bytes(), ALMA10), ("rpm", rpm_bytes(), UBUNTU)], ids=lambda value: "package" if isinstance(value, bytes) else str(value))
def test_native_os_mismatch_fails_before_guest_transfer(manager, fmt, body, wrong_profile, tmp_path, monkeypatch):
    snapshot = asyncio.run(manager.upload(Request(body), "splunk." + fmt, hashlib.sha256(body).hexdigest()))["selected"]
    with pytest.raises(PackageError, match="does not match"):
        manager.validate_snapshot(snapshot, wrong_profile)
    session = guest.GuestSession("192.0.2.20", "gdeploy", "private", "password")
    session.os_profile = wrong_profile
    transfer = Mock()
    monkeypatch.setattr(session, "_run_script", transfer)
    with pytest.raises(guest.GuestError, match="does not match"):
        session.install("splunk", {"splunk_password": "never-print"}, splunk_package=Path(snapshot["path"]), splunk_sha256=snapshot["sha256"])
    transfer.assert_not_called()


@pytest.mark.parametrize("fmt,body", [("deb", deb_bytes()), ("rpm", rpm_bytes())], ids=lambda value: "package" if isinstance(value, bytes) else str(value))
def test_native_server_selection_checksum_and_format_validation(manager, config, fmt, body):
    path = config.splunk_package.with_name("splunk." + fmt)
    path.write_bytes(body)
    item = manager.catalog()["items"][0]
    with pytest.raises(PackageError, match="publisher checksum"):
        manager.select(item["id"], "0" * 64)
    result = manager.select(item["id"], hashlib.sha256(body).hexdigest())
    assert result["ready"] and result["selected"]["format"] == fmt
    altered = dict(result["selected"], format="tgz")
    with pytest.raises(PackageError):
        manager.validate_snapshot(altered)


@pytest.mark.parametrize("fmt,body", [
    ("deb", deb_bytes("splunkforwarder")), ("deb", deb_bytes(arch="arm64")),
    ("rpm", rpm_bytes("splunkforwarder")), ("rpm", rpm_bytes(arch="aarch64")),
    ("deb", rpm_bytes()), ("rpm", deb_bytes()), ("deb", b"broken"), ("rpm", b"broken"),
    ("deb", deb_bytes()[:-10]), ("rpm", rpm_bytes()[:120]),
], ids=lambda value: "package" if isinstance(value, bytes) else str(value))
def test_wrong_native_package_keeps_previous_selection_and_removes_upload(manager, fmt, body):
    good = deb_bytes()
    previous = asyncio.run(manager.upload(Request(good), "splunk.deb", hashlib.sha256(good).hexdigest()))["selected"]
    with pytest.raises(PackageError):
        asyncio.run(manager.upload(Request(body), "bad." + fmt, hashlib.sha256(body).hexdigest()))
    assert manager.selected() == previous
    assert len(list(manager.upload_dir.iterdir())) == 1


@pytest.mark.parametrize("compression", ["gz", "xz", "bz2", "zst"], ids=lambda value: "package" if isinstance(value, bytes) else str(value))
def test_debian_control_compression(compression):
    validate_native(io.BytesIO(deb_bytes(compression=compression)), "deb")


@pytest.mark.parametrize("fmt,body,profile", [("deb", deb_bytes(), UBUNTU), ("rpm", rpm_bytes(), ALMA10)], ids=lambda value: "package" if isinstance(value, bytes) else str(value))
@pytest.mark.parametrize("change", [False, True], ids=lambda value: "package" if isinstance(value, bytes) else str(value))
def test_native_guest_guard_executes_before_install_and_dispatches_format(tmp_path, monkeypatch, fmt, body, profile, change):
    path = tmp_path / ("splunk." + fmt)
    path.write_bytes(body)
    expected = hashlib.sha256(body).hexdigest()
    session = guest.GuestSession("192.0.2.20", "gdeploy", "private", "password")
    session.os_profile = profile
    def run_guard(script, payload, package):
        assert package == path and payload["package_format"] == fmt
        block = re.search(r"python3 - <<'PY'\n(.*?)\nPY", script, re.S)
        assert block.end() < script.index("useradd --system")
        assert block.end() < script.index("./splunk." + fmt)
        (tmp_path / "payload.json").write_text(json.dumps(payload))
        if change:
            path.write_bytes(body + b"changed")
        result = subprocess.run([sys.executable, "-c", block[1]], cwd=tmp_path, capture_output=True, text=True, timeout=10)
        assert (result.returncode != 0) == change
        assert "never-print" not in result.stdout + result.stderr
        if change:
            raise guest.GuestError(result.stderr)
        assert "systemctl enable --now Splunkd" in script
        return {}
    monkeypatch.setattr(session, "_run_script", run_guard)
    if change:
        with pytest.raises(guest.GuestError, match="SHA-256 changed"):
            session.install("splunk", {"splunk_password": "never-print"}, splunk_package=path, splunk_sha256=expected)
    else:
        assert session.install("splunk", {"splunk_password": "never-print"}, splunk_package=path, splunk_sha256=expected)["services"][0]["name"] == "Splunk"


def test_native_metadata_size_limits_and_script_syntax():
    oversized = b"\xed\xab\xee\xdb\x03\x00\0\0" + b"\0" * 88
    oversized += b"\x8e\xad\xe8\x01\0\0\0\0" + struct.pack(">II", 65537, 1)
    with pytest.raises(NativePackageError, match="Too many"):
        validate_native(io.BytesIO(oversized), "rpm")
    for script in (guest._SPLUNK_SCRIPT, application_script("splunk")):
        checked = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize("fmt,body", [("deb", deb_bytes()), ("rpm", rpm_bytes())], ids=lambda value: "package" if isinstance(value, bytes) else str(value))
def test_native_authenticated_upload_api_keeps_format_and_user_filename(signed_in, fmt, body):
    name = "splunk-10.0.0-build-linux." + fmt
    response = signed_in.post("/api/settings/splunk-package/upload", params={
        "filename": name, "sha512": hashlib.sha512(body).hexdigest(),
    }, content=body, headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 200, response.text
    selected = response.json()["selected"]
    assert selected["name"] == name and selected["format"] == fmt
    assert selected["sha256"] == hashlib.sha256(body).hexdigest()
    assert signed_in.get("/api/settings/splunk-package").json()["ready"]


@pytest.mark.parametrize("fmt", ["deb", "rpm"], ids=lambda value: "package" if isinstance(value, bytes) else str(value))
def test_real_native_package_builders(tmp_path, fmt):
    """Linux CI builds genuine native containers without installing vendor code."""
    import shutil
    tool = shutil.which("dpkg-deb" if fmt == "deb" else "rpmbuild")
    if not tool:
        pytest.skip("Linux native package builder unavailable")
    root = tmp_path / "root"
    (root / "opt/splunk/bin").mkdir(parents=True)
    (root / "opt/splunk/bin/splunk").write_text("synthetic")
    if fmt == "deb":
        (root / "DEBIAN").mkdir()
        (root / "DEBIAN/control").write_text("Package: splunk\nVersion: 10.0.0\nArchitecture: amd64\nMaintainer: Fixture <fixture@example.invalid>\nDescription: Test fixture\n")
        path = tmp_path / "splunk.deb"
        command = [tool, "--build", "--root-owner-group", str(root), str(path)]
    else:
        spec = tmp_path / "splunk.spec"
        spec.write_text("""Name: splunk
Version: 10.0.0
Release: 1
Summary: Test fixture
License: MIT
BuildArch: x86_64
%description
Synthetic metadata for native reader regression.
%install
mkdir -p %{buildroot}/opt/splunk/bin
printf synthetic > %{buildroot}/opt/splunk/bin/splunk
%files
/opt/splunk/bin/splunk
""")
        command = [tool, "--define", "_topdir " + str(tmp_path), "--define", "__os_install_post %{nil}", "-bb", str(spec)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    if fmt == "rpm":
        path = next((tmp_path / "RPMS").rglob("*.rpm"))
    with path.open("rb") as handle:
        validate_native(handle, fmt)


@pytest.mark.parametrize("fmt", ["deb", "rpm"])
@pytest.mark.parametrize("exit_code", [0, 42])
def test_native_install_dispatch_and_failure_propagation(tmp_path, fmt, exit_code):
    script = guest._SPLUNK_SCRIPT if fmt == "deb" else application_script("splunk")
    dispatch = re.search(r'package_format="\$\(python3.*?\nesac', script, re.S)[0]
    (tmp_path / "payload.json").write_text(json.dumps({"package_format": fmt}))
    stub = f"""set -e
apt-get() {{ printf '%s\\n' "$@" > command.txt; return {exit_code}; }}
timeout() {{ printf '%s\\n' "$@" > command.txt; return {exit_code}; }}
tar() {{ echo 'Wrong installer selected' >&2; return 99; }}
"""
    result = subprocess.run(["bash", "-c", stub + dispatch], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == exit_code, result.stderr
    args = (tmp_path / "command.txt").read_text().splitlines()
    assert args[-1] == "./splunk." + fmt
    if fmt == "rpm":
        assert "dnf" in args
    else:
        assert args == ["-q", "install", "-y", "--", "./splunk.deb"]


@pytest.mark.parametrize("compression", ["gz", "xz", "bz2", "zst"])
def test_debian_metadata_decompression_is_bounded(monkeypatch, compression):
    import gdeploy.splunk_native as native
    body = deb_bytes(compression=compression)
    monkeypatch.setattr(native, "METADATA_LIMIT", 2048)
    with pytest.raises(NativePackageError, match="exceeds"):
        validate_native(io.BytesIO(body), "deb")
