"""The bytes installed in a VM must match its queued Splunk package checksum."""

import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from gdeploy import guest
from gdeploy.db import Database
from gdeploy.packages import SplunkPackageManager


ELF_X86_64 = b"\x7fELF\x02\x01" + b"\0" * 12 + (62).to_bytes(2, "little")


def entry(name, contents=b"", *, kind=tarfile.REGTYPE, target=""):
    member = tarfile.TarInfo(name)
    member.type = kind
    member.linkname = target
    member.size = len(contents) if member.isfile() else 0
    return member, contents


def write_archive(path, *, launcher=b"launcher", extra=(), launcher_kind=tarfile.REGTYPE):
    members = [
        entry("splunk/bin/splunk", launcher, kind=launcher_kind, target="../lib/launcher"),
        entry("splunk/bin/splunkd", ELF_X86_64),
        *extra,
    ]
    with tarfile.open(path, "w:gz") as archive:
        for member, contents in members:
            archive.addfile(member, io.BytesIO(contents) if member.isfile() else None)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_remote_integrity_guard(directory: Path, script: str, payload: dict):
    """Execute the actual remote guard, never package installation or extraction."""
    first_block = re.search(r"python3 - <<'PY'\n(.*?)\nPY", script, re.S)
    assert first_block is not None
    # A failing integrity check must precede every modifying installer command.
    for command in ("apt-get -q update", "tar --extract", "useradd --system"):
        assert first_block.end() < script.index(command)
    (directory / "payload.json").write_text(json.dumps(payload))
    block = first_block[1] + "\npathlib.Path('guard-complete').touch()\n"
    return subprocess.run(
        [sys.executable, "-c", block], cwd=directory, capture_output=True, text=True, timeout=10,
    )


@pytest.mark.parametrize("replacement_point", ["after_local_verification", "during_transfer"])
def test_replaced_package_after_local_verification_cannot_reach_installation(config, tmp_path, monkeypatch, replacement_point):
    expected = write_archive(config.splunk_package, launcher=b"queued release")
    cfg = replace(config, splunk_sha256=expected)
    manager = SplunkPackageManager(Database(cfg.data_dir, cfg.secret_key), cfg)
    snapshot = manager.legacy()
    verified_path = manager.validate_snapshot(snapshot)
    remote = tmp_path / "remote-stage"
    remote.mkdir()
    if replacement_point == "after_local_verification":
        replacement = write_archive(verified_path, launcher=b"replaced before SSH connection")
        assert replacement != expected

    session = guest.GuestSession("192.0.2.20", "gdeploy", "private-key", "guest-password")
    session.client = Mock()
    probes = []

    def transfer_and_run_guard(script, payload, package):
        assert payload["package_sha256"] == snapshot["sha256"]
        if replacement_point == "during_transfer":
            replacement = write_archive(package, launcher=b"replaced after archive inspection")
            assert replacement != expected
        shutil.copyfile(package, remote / "splunk.tgz")
        result = run_remote_integrity_guard(remote, script, payload)
        probes.append(result)
        if result.returncode:
            raise guest.GuestError(result.stderr)
        pytest.fail("Changed package passed the remote integrity check")

    monkeypatch.setattr(session, "_run_script", transfer_and_run_guard)
    with pytest.raises(guest.GuestError, match="SHA-256 changed during transfer"):
        session.install(
            "splunk", {"splunk_password": "splunk-password"},
            splunk_package=verified_path, splunk_sha256=snapshot["sha256"],
        )
    assert len(probes) == 1 and probes[0].returncode != 0
    assert not (remote / "guard-complete").exists()
    assert not (remote / "splunk").exists()
    assert "splunk-password" not in probes[0].stderr


@pytest.mark.parametrize("expected", [None, "", "not-a-checksum", "0" * 63, "0" * 65, "G" * 64, False])
def test_missing_or_malformed_expected_checksum_stops_before_transfer(tmp_path, monkeypatch, expected):
    package = tmp_path / "splunk.tgz"
    write_archive(package)
    session = guest.GuestSession("192.0.2.20", "gdeploy", "private-key", "guest-password")
    transfer = Mock(side_effect=AssertionError("Unverified package must not be transferred"))
    monkeypatch.setattr(session, "_run_script", transfer)
    with pytest.raises(guest.GuestError, match="verified Splunk package SHA-256"):
        session.install(
            "splunk", {"splunk_password": "splunk-password"}, splunk_package=package, splunk_sha256=expected,
        )
    transfer.assert_not_called()


@pytest.mark.parametrize("expected", [None, "0" * 64])
def test_remote_guard_fails_closed_without_the_correct_expected_checksum(tmp_path, expected):
    write_archive(tmp_path / "splunk.tgz")
    payload = {"secrets": {"splunk_password": "never-log-this-password"}}
    if expected is not None:
        payload["package_sha256"] = expected
    result = run_remote_integrity_guard(tmp_path, guest._SPLUNK_SCRIPT, payload)
    assert result.returncode != 0
    assert not (tmp_path / "guard-complete").exists()
    assert not (tmp_path / "splunk").exists()
    assert "never-log-this-password" not in result.stdout + result.stderr


def test_matching_transferred_bytes_advance_past_the_real_guard(tmp_path, monkeypatch):
    package = tmp_path / "splunk.tgz"
    expected = write_archive(package)
    session = guest.GuestSession("192.0.2.20", "gdeploy", "private-key", "guest-password")
    observed = []

    def run_guard_only(script, payload, transferred):
        assert transferred == package
        assert payload["package_sha256"] == expected
        result = run_remote_integrity_guard(tmp_path, script, payload)
        assert result.returncode == 0, result.stderr
        observed.append(result)
        return {}

    monkeypatch.setattr(session, "_run_script", run_guard_only)
    result = session.install(
        "splunk", {"splunk_password": "splunk-password"}, splunk_package=package, splunk_sha256=expected,
    )
    assert len(observed) == 1
    assert (tmp_path / "guard-complete").is_file()
    assert not (tmp_path / "splunk").exists()
    assert result["services"][0]["name"] == "Splunk"


@pytest.mark.parametrize("duplicate_name", ["splunk/bin/splunkd", "./splunk/bin/splunkd", "splunk/bin/./splunkd"])
def test_duplicate_daemon_cannot_replace_previously_verified_elf(tmp_path, duplicate_name):
    package = tmp_path / "duplicate.tgz"
    write_archive(package, extra=[entry(duplicate_name, b"not an ELF executable")])
    with pytest.raises(guest.GuestError, match="duplicate file paths"):
        guest._validate_splunk_archive(package)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.DIRTYPE, tarfile.LNKTYPE])
def test_launcher_must_be_a_regular_nonempty_file(tmp_path, kind):
    package = tmp_path / "wrong-launcher.tgz"
    # Hard links use an archive-root path; symlinks use the containing directory.
    if kind == tarfile.LNKTYPE:
        with tarfile.open(package, "w:gz") as archive:
            for member, contents in (
                entry("splunk/bin/splunk", kind=kind, target="splunk/lib/launcher"),
                entry("splunk/bin/splunkd", ELF_X86_64),
                entry("splunk/lib/launcher", b"launcher"),
            ):
                archive.addfile(member, io.BytesIO(contents) if member.isfile() else None)
    else:
        write_archive(package, launcher_kind=kind)
    with pytest.raises(guest.GuestError, match="Enterprise Linux x86_64"):
        guest._validate_splunk_archive(package)


def test_empty_launcher_is_not_a_usable_package(tmp_path):
    package = tmp_path / "empty-launcher.tgz"
    write_archive(package, launcher=b"")
    with pytest.raises(guest.GuestError, match="Enterprise Linux x86_64"):
        guest._validate_splunk_archive(package)


def test_normal_library_links_and_repeated_directory_entries_remain_supported(tmp_path):
    package = tmp_path / "valid-links.tgz"
    write_archive(package, extra=[
        entry("splunk/lib", kind=tarfile.DIRTYPE),
        entry("./splunk/lib/.", kind=tarfile.DIRTYPE),
        entry("splunk/lib/libexample.so.1", b"library"),
        entry("splunk/lib/libexample.so", kind=tarfile.SYMTYPE, target="libexample.so.1"),
    ])
    guest._validate_splunk_archive(package)
