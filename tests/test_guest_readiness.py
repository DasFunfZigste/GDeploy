"""Regressions for installed Ubuntu guests whose cloud-init is intentionally disabled."""

import json
import socket
import threading
from unittest.mock import Mock

import paramiko
import pytest

from gdeploy import guest


# Ubuntu 24.04.5 / cloud-init 26.1 output supplied with the failed deployment.
DISABLED_STATUS = {
    "boot_status_code": "disabled-by-marker-file",
    "datasource": "none",
    "detail": "DataSourceNone",
    "errors": [],
    "extended_status": "disabled",
    "init": {"errors": [], "finished": 15.0, "recoverable_errors": {}, "start": 12.85},
    "init-local": {"errors": [], "finished": 10.38, "recoverable_errors": {}, "start": 9.4},
    "last_update": "Thu, 01 Jan 1970 00:00:20 +0000",
    "modules-config": {"errors": [], "finished": 19.54, "recoverable_errors": {}, "start": 15.55},
    "modules-final": {"errors": [], "finished": 20.18, "recoverable_errors": {}, "start": 19.95},
    "recoverable_errors": {},
    "stage": None,
    "status": "disabled",
}
PERMISSION_WARNINGS = (
    "2026-10-02 14:26:07,513 - util.py[WARNING]: REDACTED config part "
    "/etc/cloud/cloud.cfg.d/99-installer.cfg, insufficient permissions\n"
    "2026-10-02 14:26:07,514 - util.py[WARNING]: REDACTED config part "
    "/etc/cloud/cloud.cfg.d/90-installer-network.cfg, insufficient permissions\n"
)


class DiagnosticSSHServer(paramiko.ServerInterface):
    """A real SSH peer that sends interleaved streams without invoking a shell."""

    def __init__(self, stdout, stderr, status):
        self.stdout = stdout
        self.stderr = stderr
        self.status = status
        self.command = None
        self.stdin = bytearray()
        self.worker = None
        self.failure = None

    def check_auth_none(self, username):
        return paramiko.AUTH_SUCCESSFUL

    def check_channel_request(self, kind, channel_id):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        self.command = command.decode()

        def run():
            try:
                while data := channel.recv(32768):
                    self.stdin.extend(data)
                channel.sendall_stderr(self.stderr[:37])
                channel.sendall(self.stdout[:19])
                channel.sendall_stderr(self.stderr[37:])
                channel.sendall(self.stdout[19:])
                channel.send_exit_status(self.status)
                channel.shutdown_write()
            except Exception as exc:
                self.failure = exc

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()
        return True


@pytest.mark.parametrize("exit_code", [0, 2])
@pytest.mark.parametrize("large_output", [False, True])
def test_ssh_result_separates_streams_retains_exit_code_and_bounds_output(exit_code, large_output):
    stdout = (b"out" * 100000 + b"stdout-end") if large_output else json.dumps(DISABLED_STATUS).encode()
    stderr = (b"err" * 100000 + b"stderr-end") if large_output else PERMISSION_WARNINGS.encode()
    server = DiagnosticSSHServer(stdout, stderr, exit_code)
    client_socket, server_socket = socket.socketpair()
    server_transport = paramiko.Transport(server_socket)
    client_transport = paramiko.Transport(client_socket)
    try:
        server_transport.add_server_key(paramiko.RSAKey.generate(1024))
        server_transport.start_server(event=threading.Event(), server=server)
        client_transport.start_client(timeout=5)
        client_transport.auth_none("gdeploy")
        session = guest.GuestSession("192.0.2.20", "gdeploy", "private-key", "os-password")
        session.client = Mock()
        session.client.get_transport.return_value = client_transport
        result = session._exec_result("cloud-init status --format json", timeout=5, sudo=True)
        assert result.exit_code == exit_code
        assert result.stdout == stdout[-131072:].decode()
        assert result.stderr == stderr[-131072:].decode()
        if not large_output:
            assert json.loads(result.stdout) == DISABLED_STATUS
        assert server.stdin == b"os-password\n"
        assert "os-password" not in server.command
        assert "sudo -k -S" in server.command
        assert server.failure is None
    finally:
        client_transport.close()
        server_transport.close()
        if server.worker:
            server.worker.join(timeout=5)
        client_socket.close()
        server_socket.close()


@pytest.fixture
def connected_session():
    session = guest.GuestSession("192.0.2.20", "gdeploy", "private-key", "os-password")
    session.client = Mock()
    return session


@pytest.mark.parametrize(
    "status, exit_code",
    [
        ({"status": "error", "errors": ["package install failed"]}, 1),
        ({"status": "done", "errors": ["package install failed"]}, 0),
        ({"status": "done", "errors": [], "init": {"errors": ["network failed"]}}, 0),
        ({"status": "done", "errors": [], "modules-final": {"errors": ["user setup failed"]}}, 0),
        ({"status": "done", "errors": [], "recoverable_errors": {"WARNING": ["apt failed"]}}, 2),
        ({"status": "done", "errors": [], "modules-config": {"recoverable_errors": {"WARNING": ["failed"]}}}, 0),
        ({"status": "done", "errors": [], "extended_status": "degraded done"}, 2),
        ({"status": "done", "errors": []}, 1),
        ({**DISABLED_STATUS, "boot_status_code": "disabled-by-generator"}, 0),
        ({**DISABLED_STATUS, "boot_status_code": "disabled-by-kernel-command-line"}, 0),
        ({"status": "not-an-understood-state", "errors": []}, 0),
    ],
)
def test_genuine_cloud_init_failures_and_unsupported_states_stop_readiness(
    monkeypatch, connected_session, status, exit_code,
):
    monkeypatch.setattr(
        connected_session, "_exec_result",
        lambda *args, **kwargs: guest.CommandResult(json.dumps(status), "", exit_code),
    )
    with pytest.raises(guest.GuestError) as caught:
        connected_session.wait_ready(timeout=10)
    assert not isinstance(caught.value, guest.GuestConnectionError)


@pytest.mark.parametrize("output", ["", "warning before JSON\n{}", "not JSON", "null", "[]", '"done"'])
def test_invalid_status_output_fails_with_actionable_sanitized_diagnostics(monkeypatch, connected_session, output):
    monkeypatch.setattr(
        connected_session, "_exec_result",
        lambda *args, **kwargs: guest.CommandResult(output, "diagnostic os-password private-key", 0),
    )
    events = []
    with pytest.raises(guest.GuestError) as caught:
        connected_session.wait_ready(timeout=10, log=lambda message, level: events.append((message, level)))
    rendered = str(caught.value) + "\n" + "\n".join(message for message, _ in events)
    assert "os-password" not in rendered
    assert "private-key" not in rendered
    assert "cloud-init" in rendered.lower()
    assert "diagnostic" in rendered


@pytest.fixture
def boot_clock(monkeypatch):
    class Clock:
        now = 100.0

        def sleep(self, duration):
            self.now += duration

    clock = Clock()
    monkeypatch.setattr(guest.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(guest.time, "sleep", clock.sleep)
    return clock


@pytest.fixture
def ready_evidence():
    return {
        "os_id": "ubuntu", "sudo": True, "root_source": "/dev/sda2", "root_fstype": "ext4",
        "boot_finished": True, "disabled_marker": True, "installer_log": True,
        "services": {
            name: {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "Result": "success"}
            for name in ("ssh.service", "open-vm-tools.service")
        },
    }


def diagnostic_replies(monkeypatch, session, statuses, evidence):
    """Return representative guest responses while retaining the actual verifier."""
    remaining = iter(statuses)
    current = None
    calls = []

    def execute(command, *, timeout, sudo, combine_stderr=False):
        nonlocal current
        assert sudo is True
        assert combine_stderr is False
        assert timeout > 0
        calls.append(command)
        if command == "cloud-init status --format json":
            current = next(remaining, current)
            assert current is not None
            return guest.CommandResult(json.dumps(current), PERMISSION_WARNINGS, 0)
        assert command.startswith("python3 -c ")
        return guest.CommandResult(json.dumps(evidence), "", 0)

    monkeypatch.setattr(session, "_exec_result", execute)
    return calls


def test_reported_installer_disabled_guest_passes_without_treating_stderr_as_json(
    monkeypatch, connected_session, ready_evidence,
):
    calls = diagnostic_replies(monkeypatch, connected_session, [DISABLED_STATUS], ready_evidence)
    events = []
    connected_session.wait_ready(timeout=20, log=lambda message, level: events.append((message, level)))
    messages = "\n".join(message for message, _ in events)
    assert len(calls) == 2
    assert "disabled-by-marker-file" in messages
    assert "insufficient permissions" in messages
    assert "verified" in messages
    assert any(level == "warning" for _, level in events)


@pytest.mark.parametrize("initial", ["running", "not started"])
def test_boot_in_progress_is_retried_then_verified(
    monkeypatch, connected_session, ready_evidence, boot_clock, initial,
):
    states = [{"status": initial, "errors": []}, {"status": "done", "errors": []}]
    calls = diagnostic_replies(monkeypatch, connected_session, states, ready_evidence)
    connected_session.wait_ready(timeout=20)
    assert calls.count("cloud-init status --format json") == 2
    assert boot_clock.now == 105.0


@pytest.mark.parametrize(
    "missing",
    ["boot_finished", "ssh.service", "open-vm-tools.service"],
)
def test_incomplete_guest_is_waited_for_but_never_marked_ready(
    monkeypatch, connected_session, ready_evidence, boot_clock, missing,
):
    if missing == "boot_finished":
        ready_evidence[missing] = False
    else:
        ready_evidence["services"][missing]["ActiveState"] = "inactive"
        ready_evidence["services"][missing]["SubState"] = "dead"
    diagnostic_replies(monkeypatch, connected_session, [DISABLED_STATUS], ready_evidence)
    with pytest.raises(guest.GuestError, match="Timed out"):
        connected_session.wait_ready(timeout=11)
    assert boot_clock.now == 111.0


def test_cloud_init_running_has_an_absolute_readiness_deadline(
    monkeypatch, connected_session, ready_evidence, boot_clock,
):
    calls = diagnostic_replies(monkeypatch, connected_session, [{"status": "running", "errors": []}], ready_evidence)
    with pytest.raises(guest.GuestError, match="Timed out"):
        connected_session.wait_ready(timeout=12)
    assert boot_clock.now == 112.0
    assert len(calls) == 3
    assert all(command.startswith("cloud-init") for command in calls)


@pytest.mark.parametrize(
    "override",
    [
        {"sudo": False}, {"os_id": "debian"}, {"root_source": "overlay"}, {"root_fstype": "squashfs"},
        {"disabled_marker": False}, {"installer_log": False}, {"services": {}},
        {"services": {"ssh.service": {"LoadState": "not-found"}}},
    ],
)
def test_disabled_cloud_init_requires_affirmative_installed_system_evidence(
    monkeypatch, connected_session, ready_evidence, override,
):
    ready_evidence.update(override)
    diagnostic_replies(monkeypatch, connected_session, [DISABLED_STATUS], ready_evidence)
    with pytest.raises(guest.GuestError):
        connected_session.wait_ready(timeout=10)


@pytest.mark.parametrize("service", ["ssh.service", "open-vm-tools.service"])
def test_failed_service_blocks_even_a_clean_disabled_status(monkeypatch, connected_session, ready_evidence, service):
    ready_evidence["services"][service].update(ActiveState="failed", SubState="failed", Result="exit-code")
    diagnostic_replies(monkeypatch, connected_session, [DISABLED_STATUS], ready_evidence)
    with pytest.raises(guest.GuestError, match="failed"):
        connected_session.wait_ready(timeout=10)


@pytest.mark.parametrize("stdout, stderr, code", [("{}", "sudo failed", 1), ("not JSON", "", 0), ("null", "", 0)])
def test_readiness_probe_failure_is_never_ignored(monkeypatch, connected_session, stdout, stderr, code):
    def execute(command, **kwargs):
        if command.startswith("cloud-init"):
            return guest.CommandResult(json.dumps(DISABLED_STATUS), "", 0)
        return guest.CommandResult(stdout, stderr, code)

    monkeypatch.setattr(connected_session, "_exec_result", execute)
    with pytest.raises(guest.GuestError):
        connected_session.wait_ready(timeout=10)


def test_failing_stage_is_identified_without_dumping_config_or_secrets(monkeypatch, connected_session):
    status = {"status": "done", "errors": [], "modules-final": {"errors": ["apt install failed: os-password"]}}
    monkeypatch.setattr(
        connected_session, "_exec_result",
        lambda *args, **kwargs: guest.CommandResult(json.dumps(status), "private-key", 0),
    )
    events = []
    with pytest.raises(guest.GuestError) as caught:
        connected_session.wait_ready(timeout=10, log=lambda message, level: events.append((message, level)))
    text = str(caught.value) + "\n" + "\n".join(message for message, _ in events)
    assert "modules-final" in text
    assert "errors_present=True" in text
    assert "apt install failed" not in text
    assert "os-password" not in text
    assert "private-key" not in text


def test_failed_readiness_preserves_vm_and_stops_before_kibana_or_app_install(config, spec, monkeypatch):
    from unittest.mock import MagicMock

    from gdeploy.db import Database
    from gdeploy import service as service_module

    spec["vms"].append({**spec["vms"][0], "name": "lab-kibana", "role": "kibana"})
    db = Database(config.data_dir, config.secret_key)
    esxi = MagicMock()
    esxi.__enter__.return_value = esxi
    esxi.guest_ip.return_value = "192.0.2.20"
    esxi.create_vm.return_value = "vm-123"
    service = service_module.DeploymentService(db, config, lambda **kwargs: esxi)
    monkeypatch.setattr(service, "preflight", lambda *args, **kwargs: {"ok": True, "checks": []})
    monkeypatch.setattr(service_module, "generate_ssh_key", lambda: ("private-key", "public-key"))
    monkeypatch.setattr(
        service_module, "build_seed_iso", lambda source, target, *args, **kwargs: target.write_bytes(b"iso"),
    )
    session = guest.GuestSession("192.0.2.20", "gdeploy", "private-key", "os-password")
    session.host_key = "ssh-ed25519 pinned-host-key"
    session.client = Mock()
    monkeypatch.setattr(session, "_connect", lambda: None)
    monkeypatch.setattr(
        session, "_exec_result",
        lambda *args, **kwargs: guest.CommandResult(
            json.dumps({"status": "done", "errors": [], "modules-final": {"errors": ["apt failed"]}}), "", 0,
        ),
    )
    install = Mock(side_effect=AssertionError("Application provisioning must not run after a readiness failure"))
    monkeypatch.setattr(session, "install", install)
    monkeypatch.setattr(service_module, "GuestSession", lambda *args, **kwargs: session)
    queued = service.enqueue(spec, {"host": "esxi", "username": "root", "password": "esxi-password"})
    db.claim()
    service.run(queued["id"])
    failed = db.get(queued["id"])
    assert failed["status"] == "failed"
    assert failed["vms"][0]["vm_id"] == "vm-123"
    assert esxi.create_vm.call_count == 1
    esxi.detach_iso.assert_not_called()
    esxi.delete_iso.assert_not_called()
    esxi.destroy_vm.assert_not_called()
    install.assert_not_called()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_log_prefix_does_not_bypass_sensitive_field_redaction(monkeypatch, connected_session, stream):
    result = guest.CommandResult(
        "password: unrelated-diagnostic-secret" if stream == "stdout" else "not JSON",
        "token: unrelated-diagnostic-secret" if stream == "stderr" else "",
        0,
    )
    monkeypatch.setattr(connected_session, "_exec_result", lambda *args, **kwargs: result)
    messages = []
    with pytest.raises(guest.GuestError) as caught:
        connected_session.wait_ready(timeout=10, log=lambda message, level: messages.append(message))
    assert "unrelated-diagnostic-secret" not in str(caught.value) + "\n".join(messages)


def test_stalled_ssh_exec_acknowledgement_is_closed_at_command_deadline(connected_session):
    class StalledChannel:
        def __init__(self):
            self.closed = threading.Event()

        def settimeout(self, timeout):
            pass

        def set_combine_stderr(self, enabled):
            pass

        def exec_command(self, command):
            if not self.closed.wait(timeout=2):
                pytest.fail("A peer withholding the exec acknowledgement bypassed the command deadline")
            raise paramiko.SSHException("Channel closed")

        def close(self):
            self.closed.set()

    channel = StalledChannel()
    connected_session.client.get_transport.return_value.open_session.return_value = channel
    started = guest.time.monotonic()
    with pytest.raises(guest.GuestError, match="time limit") as caught:
        connected_session._exec_result("cloud-init status --format json", timeout=0.03)
    assert not isinstance(caught.value, guest.GuestConnectionError)
    assert channel.closed.is_set()
    assert guest.time.monotonic() - started < 1


def test_clean_installer_disabled_guests_continue_through_elasticsearch_and_kibana(
    config, spec, monkeypatch, ready_evidence,
):
    from unittest.mock import MagicMock

    from gdeploy.db import Database
    from gdeploy import service as service_module

    spec["vms"].append({**spec["vms"][0], "name": "lab-kibana", "role": "kibana"})
    db = Database(config.data_dir, config.secret_key)
    esxi = MagicMock()
    esxi.__enter__.return_value = esxi
    esxi.guest_ip.side_effect = lambda vm_id: {"vm-123": "192.0.2.20", "vm-124": "192.0.2.21"}[vm_id]
    esxi.create_vm.side_effect = ["vm-123", "vm-124"]
    service = service_module.DeploymentService(db, config, lambda **kwargs: esxi)
    monkeypatch.setattr(service, "preflight", lambda *args, **kwargs: {"ok": True, "checks": []})
    monkeypatch.setattr(service_module, "generate_ssh_key", lambda: ("private-key", "public-key"))
    monkeypatch.setattr(
        service_module, "build_seed_iso", lambda source, target, *args, **kwargs: target.write_bytes(b"iso"),
    )
    elastic = {"ip": "192.0.2.20", "service_token": "integration-token", "ca_pem": "test-ca", "version": "9.1.0"}

    def provision(role, secrets, **kwargs):
        if role == "elasticsearch":
            return {"elastic": elastic, "services": []}
        assert role == "kibana"
        assert kwargs["elastic"] == elastic
        return {"services": []}

    install = Mock(side_effect=provision)

    def connected_guest(ip, username, key, password, known=None):
        session = guest.GuestSession(ip, username, key, password, known)
        session.host_key = known or "ssh-ed25519 pinned-host-key"
        session.client = Mock()
        monkeypatch.setattr(session, "_connect", lambda: None)
        diagnostic_replies(monkeypatch, session, [DISABLED_STATUS], ready_evidence)
        monkeypatch.setattr(session, "install", install)
        return session

    monkeypatch.setattr(service_module, "GuestSession", connected_guest)
    queued = service.enqueue(spec, {"host": "esxi", "username": "root", "password": "esxi-password"})
    db.claim()
    service.run(queued["id"])
    completed = db.get(queued["id"])
    assert completed["status"] == "completed", completed.get("error")
    assert all(vm["status"] == "completed" for vm in completed["vms"])
    assert [call.args[0] for call in install.call_args_list] == ["elasticsearch", "kibana"]
    assert esxi.create_vm.call_count == esxi.detach_iso.call_count == esxi.delete_iso.call_count == 2
    esxi.destroy_vm.assert_not_called()
    messages = "\n".join(event["message"] for event in db.events(queued["id"]))
    assert messages.count("OS readiness verified") == 2
    assert "disabled-by-marker-file" in messages
    assert "integration-token" not in messages


def test_readiness_probe_reads_only_nonsecret_completion_evidence(monkeypatch, capsys, ready_evidence):
    import os
    from pathlib import Path
    import subprocess

    read_paths = []
    checked_paths = []
    commands = []

    def read_text(path, *args, **kwargs):
        read_paths.append(str(path))
        assert str(path) == "/etc/os-release"
        return 'PRETTY_NAME="Ubuntu 24.04.5 LTS"\nID=ubuntu\nVERSION_ID="24.04"\n'

    def is_file(path):
        checked_paths.append(str(path))
        return str(path) in {
            "/var/lib/cloud/instance/boot-finished", "/etc/cloud/cloud-init.disabled",
            "/var/log/installer/curtin-install.log",
        }

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["capture_output"] is True
        assert kwargs["timeout"] <= 10
        if command[0] == "findmnt":
            return subprocess.CompletedProcess(command, 0, "/dev/sda2 ext4\n", "")
        assert command[0] == "systemctl" and command[1] == "show"
        assert command[2] in {"ssh.service", "open-vm-tools.service"}
        return subprocess.CompletedProcess(command, 0, "Result=success\nLoadState=loaded\nActiveState=active\nSubState=running\n", "")

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    exec(compile(guest._READINESS_PROBE, "readiness-probe.py", "exec"), {})
    evidence = json.loads(capsys.readouterr().out)
    assert evidence == ready_evidence
    assert read_paths == ["/etc/os-release"]
    assert len(checked_paths) == 3
    assert len(commands) == 3


@pytest.mark.parametrize("blocked_phase", ["exec", "send"])
def test_stalled_transport_send_is_closed_and_eof_becomes_deadline_error(connected_session, blocked_phase):
    closed = threading.Event()
    transport = Mock()
    transport.close.side_effect = closed.set
    channel = Mock()
    transport.open_session.return_value = channel
    connected_session.client.get_transport.return_value = transport

    def wait_for_transport(*args):
        if not closed.wait(timeout=2):
            pytest.fail("A blocked transport write bypassed the command deadline")
        raise EOFError("Transport closed while writing an SSH packet")

    if blocked_phase == "exec":
        channel.exec_command.side_effect = wait_for_transport
    else:
        channel.sendall.side_effect = wait_for_transport
    started = guest.time.monotonic()
    with pytest.raises(guest.GuestError, match="time limit") as caught:
        connected_session._exec_result("cloud-init status --format json", timeout=0.03, sudo=True)
    assert not isinstance(caught.value, guest.GuestConnectionError)
    transport.close.assert_called_once()
    channel.close.assert_called()
    assert guest.time.monotonic() - started < 1


@pytest.mark.parametrize("cleanup_eof", [False, True])
def test_command_cleanup_remains_bounded_and_preserves_received_result(connected_session, cleanup_eof):
    closed = threading.Event()
    transport = Mock()
    transport.close.side_effect = closed.set
    channel = Mock()
    channel.recv_ready.return_value = False
    channel.recv_stderr_ready.return_value = False
    channel.exit_status_ready.return_value = True
    channel.recv_exit_status.return_value = 0
    caller = threading.get_ident()

    def close_channel():
        if not closed.wait(timeout=2):
            pytest.fail("Final channel cleanup bypassed the command deadline")
        if cleanup_eof and threading.get_ident() == caller:
            raise EOFError("Transport closed during final channel cleanup")

    channel.close.side_effect = close_channel
    transport.open_session.return_value = channel
    connected_session.client.get_transport.return_value = transport
    started = guest.time.monotonic()
    result = connected_session._exec_result("cloud-init status --format json", timeout=0.03)
    assert result == guest.CommandResult("", "", 0)
    transport.close.assert_called_once()
    channel.close.assert_called()
    assert guest.time.monotonic() - started < 1
