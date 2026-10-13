"""Alma Kickstart completion is independent of Ubuntu's cloud-init lifecycle."""

import copy
import json
import os
import pathlib
import platform
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gdeploy import alma_readiness, guest
from gdeploy.os_profiles import ALMA10


@pytest.fixture
def evidence():
    return {
        "os_id": "almalinux", "os_version": "10.0", "architecture": "x86_64", "sudo": True,
        "root_source": "/dev/mapper/almalinux-root", "root_fstype": "xfs", "hostname": "alma-test",
        "installer_marker": {
            "schema": 1, "os_id": "almalinux", "major": 10,
            "installer": "anaconda-kickstart", "hostname": "alma-test",
        },
        "installer_marker_safe": True,
        "services": {name: {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "Result": "success"}
                     for name in ("sshd.service", "vmtoolsd.service")},
    }


@pytest.mark.parametrize("source,filesystem", [
    ("/dev/mapper/almalinux-root", "xfs"), ("/dev/sda3", "ext4"),
    ("/dev/nvme0n1p3", "xfs"), ("/dev/disk/by-uuid/abcd-1234", "xfs"),
])
def test_verified_installed_alma_disks_are_ready(evidence, source, filesystem):
    evidence.update(root_source=source, root_fstype=filesystem)
    assert alma_readiness.pending_reason(evidence, "alma-test") is None


@pytest.mark.parametrize("changes", [
    {"os_id": "ubuntu"}, {"os_version": "9.6"}, {"os_version": "10.invalid"}, {"os_version": 10},
    {"architecture": "aarch64"}, {"root_source": "overlay"}, {"root_fstype": "squashfs"},
    {"root_source": "/dev/loop0"}, {"root_source": "/dev/sr0"}, {"root_source": "/dev/ram0"},
    {"root_source": "/dev/mapper/live-rw"}, {"root_source": "/dev/../tmp/root"}, {"root_source": "/dev/"},
])
def test_live_installer_or_wrong_os_cannot_pass_disk_evidence(evidence, changes):
    evidence.update(changes)
    with pytest.raises(guest.GuestError, match="installed AlmaLinux"):
        alma_readiness.pending_reason(evidence, "alma-test")


@pytest.mark.parametrize("sudo", [False, None, 1, "yes"])
def test_unverified_sudo_stops_readiness(evidence, sudo):
    evidence["sudo"] = sudo
    with pytest.raises(guest.GuestError, match="sudo"):
        alma_readiness.pending_reason(evidence, "alma-test")


def test_missing_completion_marker_waits_for_kickstart(evidence):
    evidence["installer_marker"] = None
    evidence["installer_marker_safe"] = False
    assert alma_readiness.pending_reason(evidence, "alma-test") == "Kickstart completion marker is not present"


@pytest.mark.parametrize("changes", [
    {"schema": 2}, {"schema": True}, {"major": "10"}, {"major": 9},
    {"os_id": "ubuntu"}, {"installer": "cloud-init"}, {"hostname": "a-different-vm"},
])
def test_mismatched_marker_is_fatal_without_echoing_its_contents(evidence, changes):
    evidence["installer_marker"].update(changes, private_token="private-token-not-for-logs")
    with pytest.raises(guest.GuestError, match="completion evidence") as caught:
        alma_readiness.pending_reason(evidence, "alma-test")
    assert "private-token-not-for-logs" not in str(caught.value)


@pytest.mark.parametrize("marker", ["invalid", [], False, 1])
def test_unsupported_marker_response_is_fatal(evidence, marker):
    evidence["installer_marker"] = marker
    with pytest.raises(guest.GuestError, match="completion evidence"):
        alma_readiness.pending_reason(evidence, "alma-test")


@pytest.mark.parametrize("safe", [False, None, 1, "true"])
def test_only_verified_marker_ownership_and_mode_are_accepted(evidence, safe):
    evidence["installer_marker_safe"] = safe
    with pytest.raises(guest.GuestError, match="completion evidence"):
        alma_readiness.pending_reason(evidence, "alma-test")


@pytest.mark.parametrize("observed", [None, "", "a different VM", "alma-test.example.com", "WrongCase"])
def test_missing_or_invalid_guest_hostname_is_fatal_even_without_expected_name(evidence, observed):
    evidence["hostname"] = observed
    with pytest.raises(guest.GuestError, match="valid guest hostname"):
        alma_readiness.pending_reason(evidence, None)


def test_expected_deployment_hostname_cannot_be_skipped(evidence):
    evidence["hostname"] = evidence["installer_marker"]["hostname"] = "another-vm"
    with pytest.raises(guest.GuestError, match="does not match this deployment"):
        alma_readiness.pending_reason(evidence, "alma-test")


@pytest.mark.parametrize("name", ["sshd.service", "vmtoolsd.service"])
@pytest.mark.parametrize("field,value", [
    ("LoadState", "not-found"), ("ActiveState", "failed"), ("Result", "exit-code"),
])
def test_missing_or_failed_services_stop_readiness(evidence, name, field, value):
    evidence["services"][name][field] = value
    with pytest.raises(guest.GuestError, match=name):
        alma_readiness.pending_reason(evidence, "alma-test")


@pytest.mark.parametrize("name", ["sshd.service", "vmtoolsd.service"])
def test_service_not_yet_running_is_pending(evidence, name):
    evidence["services"][name].update(ActiveState="activating", SubState="start")
    assert alma_readiness.pending_reason(evidence, "alma-test") == name + " is not running"


@pytest.fixture
def probe_environment(tmp_path, monkeypatch, evidence):
    """Execute the actual emitted probe against real protected filesystem objects."""
    real_path, real_open, real_fstat = pathlib.Path, os.open, os.fstat
    directory = tmp_path / "gdeploy"
    directory.mkdir(mode=0o755)
    marker = directory / "os-install-complete.json"
    marker.write_text(json.dumps(evidence["installer_marker"]))
    marker.chmod(0o600)
    release = tmp_path / "os-release"
    release.write_text('ID="almalinux"\nVERSION_ID="10.0"\n')
    owner = {"directory": 0, "marker": 0}

    def fstat(descriptor):
        value = real_fstat(descriptor)
        return SimpleNamespace(st_mode=value.st_mode, st_size=value.st_size,
                               st_uid=owner["directory" if stat.S_ISDIR(value.st_mode) else "marker"])

    def open_file(path, flags, *args, **kwargs):
        return real_open(str(directory) if str(path) == "/var/lib/gdeploy" else path, flags, *args, **kwargs)

    def path(path):
        return real_path(release) if str(path) == "/etc/os-release" else real_path(path)

    def run(args, **kwargs):
        assert kwargs["timeout"] == 10
        if args[0] == "findmnt":
            output = "/dev/mapper/almalinux-root xfs\n"
        elif args[0] == "hostname":
            output = "alma-test\n"
        else:
            output = "LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n"
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(pathlib, "Path", path)
    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(subprocess, "run", run)
    return directory, marker, owner


def execute_probe(capsys):
    exec(compile(alma_readiness.PROBE, "emitted-alma-readiness", "exec"), {})
    return json.loads(capsys.readouterr().out)


def test_actual_probe_accepts_only_root_owned_private_regular_marker(probe_environment, capsys):
    result = execute_probe(capsys)
    assert result["installer_marker_safe"] is True
    assert result["installer_marker"]["hostname"] == "alma-test"
    assert alma_readiness.pending_reason(result, "alma-test") is None


@pytest.mark.parametrize("failure", [
    "parent-write", "parent-owner", "file-owner", "file-mode", "empty", "oversized", "malformed",
    "symlink", "fifo", "parent-symlink",
])
def test_actual_probe_rejects_unsafe_marker_without_blocking_or_reading_secrets(probe_environment, capsys, failure):
    directory, marker, owner = probe_environment
    if failure == "parent-write":
        directory.chmod(0o777)
    elif failure == "parent-owner":
        owner["directory"] = 1000
    elif failure == "file-owner":
        owner["marker"] = 1000
    elif failure == "file-mode":
        marker.chmod(0o644)
    elif failure in {"empty", "oversized", "malformed"}:
        marker.write_text({"empty": "", "oversized": marker.read_text() + " " * 4097,
                           "malformed": '{"private_key":"secret-not-for-logs"'}[failure])
    elif failure == "symlink":
        outside = marker.parent.parent / "outside"
        outside.write_text("secret-not-for-logs")
        marker.unlink()
        marker.symlink_to(outside)
    elif failure == "fifo":
        marker.unlink()
        os.mkfifo(marker, mode=0o600)
    else:
        directory.rename(directory.with_name("outside-directory"))
        directory.symlink_to(directory.with_name("outside-directory"), target_is_directory=True)
    result = execute_probe(capsys)
    assert result["installer_marker_safe"] is False
    assert result["installer_marker"] == "invalid"
    assert "secret-not-for-logs" not in json.dumps(result)
    with pytest.raises(guest.GuestError, match="completion evidence"):
        alma_readiness.pending_reason(result, "alma-test")


def test_actual_probe_missing_marker_is_pending(probe_environment, capsys):
    _, marker, _ = probe_environment
    marker.unlink()
    result = execute_probe(capsys)
    assert result["installer_marker"] is None
    assert alma_readiness.pending_reason(result, "alma-test") == "Kickstart completion marker is not present"


@pytest.fixture
def session(monkeypatch):
    result = guest.GuestSession("192.0.2.20", "gdeploy", "private-ssh-key", "private-os-password",
                                os_profile=ALMA10, expected_hostname="alma-test")
    result.client = Mock()
    clock = SimpleNamespace(now=100.0, sleeps=[])

    def wait(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(guest.time, "monotonic", lambda: clock.now)
    result.set_cancellation_hooks(lambda: None, wait)
    return result, clock


def probe_result(evidence, *, stderr="", exit_code=0):
    return guest.CommandResult(json.dumps(evidence), stderr, exit_code)


def test_alma_guest_never_runs_cloud_init_and_uses_sudo_bounded_probe(session, monkeypatch, evidence):
    target, _ = session
    run = Mock(return_value=probe_result(evidence))
    monkeypatch.setattr(target, "_exec_result", run)
    events = []
    target.wait_ready(timeout=20, log=lambda message, level: events.append(message))
    command = run.call_args.args[0]
    assert "cloud-init" not in command
    assert "os-install-complete.json" in command
    assert run.call_args.kwargs == {"timeout": 20, "sudo": True}
    assert any("AlmaLinux OS readiness verified" in event for event in events)


def test_kickstart_and_service_progress_are_polled_until_complete(session, monkeypatch, evidence):
    target, clock = session
    pending = copy.deepcopy(evidence)
    pending.update(installer_marker=None, installer_marker_safe=False)
    run = Mock(side_effect=[probe_result(pending), probe_result(pending), probe_result(evidence)])
    monkeypatch.setattr(target, "_exec_result", run)
    events = []
    target.wait_ready(timeout=20, log=lambda message, level: events.append(message))
    assert clock.sleeps == [5, 5]
    assert sum("Waiting for AlmaLinux" in event for event in events) == 1
    assert [call.kwargs["timeout"] for call in run.call_args_list] == [20, 15, 10]


def test_success_arriving_after_overall_deadline_is_not_accepted(session, monkeypatch, evidence):
    target, clock = session

    def run(*args, **kwargs):
        clock.now += 11
        return probe_result(evidence)

    monkeypatch.setattr(target, "_exec_result", run)
    with pytest.raises(guest.GuestError, match="Timed out verifying AlmaLinux"):
        target.wait_ready(timeout=10)


def test_missing_marker_timeout_is_bounded_and_actionable(session, monkeypatch, evidence):
    target, clock = session
    evidence.update(installer_marker=None, installer_marker_safe=False)
    run = Mock(return_value=probe_result(evidence))
    monkeypatch.setattr(target, "_exec_result", run)
    with pytest.raises(guest.GuestError, match="Kickstart completion marker is not present"):
        target.wait_ready(timeout=11)
    assert clock.sleeps == [5, 5, 1]
    assert [call.kwargs["timeout"] for call in run.call_args_list] == [11, 6, 1]


@pytest.mark.parametrize("output,code", [("invalid JSON private-os-password", 0), ("null", 0), ("{}", 1)])
def test_bad_probe_outputs_are_sanitized_and_fatal(session, monkeypatch, output, code):
    target, clock = session
    monkeypatch.setattr(target, "_exec_result", lambda *args, **kwargs: guest.CommandResult(
        output, "diagnostic private-os-password private-ssh-key", code,
    ))
    events = []
    with pytest.raises(guest.GuestError) as caught:
        target.wait_ready(timeout=20, log=lambda message, level: events.append(message))
    rendered = str(caught.value) + "\n" + "\n".join(events)
    assert "private-os-password" not in rendered
    assert "private-ssh-key" not in rendered
    assert "diagnostic" in rendered
    assert clock.sleeps == []


@pytest.mark.parametrize("point", ["before", "after-probe", "during-wait"])
def test_stop_requests_interrupt_alma_readiness_at_safe_boundaries(session, monkeypatch, evidence, point):
    target, clock = session
    stopped = {"value": point == "before"}

    def check():
        if stopped["value"]:
            raise RuntimeError("deployment stopped")

    def run(*args, **kwargs):
        if point == "after-probe":
            stopped["value"] = True
        if point == "during-wait":
            return probe_result({**evidence, "installer_marker": None, "installer_marker_safe": False})
        return probe_result(evidence)

    def wait(seconds):
        clock.sleeps.append(seconds)
        stopped["value"] = True

    command = Mock(side_effect=run)
    monkeypatch.setattr(target, "_exec_result", command)
    with pytest.raises(RuntimeError, match="deployment stopped"):
        target.wait_ready(timeout=20, check_cancelled=check, wait=wait)
    assert command.call_count == (0 if point == "before" else 1)


def test_ssh_connect_and_probe_share_the_same_deadline(session, monkeypatch, evidence):
    target, clock = session
    target.client = None

    def connect():
        clock.now += 7
        target.client = Mock()

    monkeypatch.setattr(target, "_connect", connect)
    command = Mock(return_value=probe_result(evidence))
    monkeypatch.setattr(target, "_exec_result", command)
    target.wait_ready(timeout=10)
    assert command.call_args.kwargs["timeout"] == 3
