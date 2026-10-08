"""Requested stops happen between guest operations, never inside a package manager."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from gdeploy import guest


class RequestedStop(RuntimeError):
    pass


@pytest.fixture
def stop():
    state = SimpleNamespace(requested=False)

    def check():
        if state.requested:
            raise RequestedStop("Administrator requested a stop.")

    state.check = check
    return state


@pytest.fixture
def session(stop):
    result = guest.GuestSession("192.0.2.20", "gdeploy", "private-key", "guest-password")
    result.set_cancellation_hooks(stop.check)
    return result


def command_channel(session, output=b"", *, on_start=lambda: None):
    pending = bytearray(output)
    channel = MagicMock()
    channel.exec_command.side_effect = lambda command: on_start()
    channel.recv_ready.side_effect = lambda: bool(pending)

    def read(size):
        data = bytes(pending[:size])
        del pending[:size]
        return data

    channel.recv.side_effect = read
    channel.recv_stderr_ready.return_value = False
    channel.exit_status_ready.return_value = True
    channel.recv_exit_status.return_value = 0
    session.client = Mock()
    session.client.get_transport.return_value.open_session.return_value = channel
    return channel


def test_readiness_stop_before_connect_avoids_ssh_and_preserves_stop_exception(session, stop, monkeypatch):
    stop.requested = True
    client = Mock()
    monkeypatch.setattr(guest.paramiko, "SSHClient", client)
    with pytest.raises(RequestedStop) as caught:
        session.wait_ready(timeout=60)
    assert not isinstance(caught.value, guest.GuestConnectionError)
    client.assert_not_called()


def test_stop_after_failed_connection_does_not_retry_or_sleep(session, stop, monkeypatch):
    def connect():
        stop.requested = True
        raise guest.GuestConnectionError("Still booting")

    retry_wait = Mock(side_effect=AssertionError("No retry after stop"))
    monkeypatch.setattr(session, "_connect", connect)
    with pytest.raises(RequestedStop):
        session.wait_ready(timeout=60, check_cancelled=stop.check, wait=retry_wait)
    retry_wait.assert_not_called()


def test_readiness_wait_hook_wakes_without_launching_another_diagnostic(session, stop, monkeypatch):
    session.client = Mock()
    execute = Mock(return_value=guest.CommandResult(json.dumps({"status": "running", "errors": []}), "", 0))
    waits = []

    def wake(seconds):
        waits.append(seconds)
        stop.requested = True

    monkeypatch.setattr(session, "_exec_result", execute)
    with pytest.raises(RequestedStop):
        session.wait_ready(timeout=60, check_cancelled=stop.check, wait=wake)
    assert len(waits) == 1 and 0 < waits[0] <= 5
    execute.assert_called_once()


def test_default_readiness_sleep_checks_stop_in_short_intervals(session, stop, monkeypatch):
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        stop.requested = True

    monkeypatch.setattr(guest.time, "sleep", sleep)
    with pytest.raises(RequestedStop):
        session._wait(5)
    assert sleeps == [0.25]


@pytest.mark.parametrize("stdout", [json.dumps({"status": "done", "errors": []}), "not JSON"])
def test_stop_after_readiness_command_prevents_probe_and_status_processing(session, stop, monkeypatch, stdout):
    session.client = Mock()

    def execute(*args, **kwargs):
        stop.requested = True
        return guest.CommandResult(stdout, "", 0)

    execute = Mock(side_effect=execute)
    monkeypatch.setattr(session, "_exec_result", execute)
    with pytest.raises(RequestedStop):
        session.wait_ready(timeout=60)
    execute.assert_called_once()


def test_stop_after_probe_prevents_readiness_success(session, stop, monkeypatch):
    session.client = Mock()
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        if command.startswith("cloud-init"):
            return guest.CommandResult(json.dumps({"status": "done", "errors": []}), "", 0)
        stop.requested = True
        return guest.CommandResult("diagnostic result need not be parsed after stop", "", 0)

    monkeypatch.setattr(session, "_exec_result", execute)
    with pytest.raises(RequestedStop):
        session.wait_ready(timeout=60)
    assert len(commands) == 2


def test_stopped_session_never_opens_new_command_channel(session, stop):
    channel = command_channel(session)
    stop.requested = True
    with pytest.raises(RequestedStop):
        session._exec_result("cloud-init status --format json", timeout=5)
    session.client.get_transport.return_value.open_session.assert_not_called()
    channel.exec_command.assert_not_called()


def test_stop_while_opening_channel_prevents_command_execution(session, stop):
    channel = command_channel(session)

    def open_session(**kwargs):
        stop.requested = True
        return channel

    session.client.get_transport.return_value.open_session.side_effect = open_session
    with pytest.raises(RequestedStop):
        session._exec_result("cloud-init status --format json", timeout=5)
    channel.exec_command.assert_not_called()
    channel.close.assert_called_once()


def test_running_command_finishes_without_cancellation_closing_its_transport(session, stop):
    channel = command_channel(session, b"finished", on_start=lambda: setattr(stop, "requested", True))
    assert session._exec("package manager operation", timeout=5) == "finished"
    session.client.get_transport.return_value.close.assert_not_called()
    channel.close.assert_called_once()
    with pytest.raises(RequestedStop):
        session._exec("next provisioning operation", timeout=5)
    channel.exec_command.assert_called_once()


def test_already_stopped_application_does_not_transfer_files_or_log_installation(session, stop, monkeypatch):
    stop.requested = True
    script = Mock()
    logs = Mock()
    monkeypatch.setattr(session, "_run_script", script)
    with pytest.raises(RequestedStop):
        session.install("ubuntu", {}, log=logs)
    script.assert_not_called()
    logs.assert_not_called()


@pytest.mark.parametrize("point", ["write", "package", "extra", "before_script"])
def test_stop_during_staging_removes_private_files_without_starting_installer(session, stop, monkeypatch, tmp_path, point):
    session.client = MagicMock()
    sftp_context = session.client.open_sftp.return_value
    sftp = sftp_context.__enter__.return_value
    execute = Mock(return_value="")
    monkeypatch.setattr(session, "_exec", execute)
    package = tmp_path / "splunk.tgz" if point == "package" else None
    extra_files = {"fleetmanager.deb": tmp_path / "fleet.deb", "dependency-1.deb": tmp_path / "dependency.deb"}

    if point == "write":
        sftp.file.return_value.__enter__.return_value.write.side_effect = lambda contents: setattr(stop, "requested", True)
    elif point in {"package", "extra"}:
        def transfer(source, target, *, callback):
            stop.requested = True
            callback(32768, 1024 * 1024)

        sftp.put.side_effect = transfer
    else:
        sftp_context.__exit__.side_effect = lambda *args: setattr(stop, "requested", True)

    with pytest.raises(RequestedStop) as caught:
        session._run_script("installer", {"secret": "private-payload"}, package, extra_files=extra_files)
    assert not isinstance(caught.value, guest.GuestConnectionError)
    execute.assert_called_once()
    assert execute.call_args.args[0].startswith("rm -rf -- /home/gdeploy/.gdeploy-")
    assert execute.call_args.kwargs == {"timeout": 30, "cancellable": False}
    if point == "write":
        sftp.file.assert_called_once()
        sftp.put.assert_not_called()
    elif point in {"package", "extra"}:
        sftp.put.assert_called_once()


def test_staging_cleanup_command_can_run_after_stop_without_hiding_the_stop(session, stop, monkeypatch):
    session.client = MagicMock()
    sftp = session.client.open_sftp.return_value.__enter__.return_value
    sftp.file.return_value.__enter__.return_value.write.side_effect = lambda contents: setattr(stop, "requested", True)
    cleanup = Mock(side_effect=guest.GuestError("Cleanup connection unavailable"))
    monkeypatch.setattr(session, "_exec", cleanup)
    with pytest.raises(RequestedStop):
        session._run_script("installer", {})
    assert cleanup.call_args.kwargs["cancellable"] is False


def test_stop_during_application_command_preserves_generated_credentials_and_blocks_next_step(session, stop, monkeypatch):
    session.client = MagicMock()
    result = {"username": "admin", "password": "newly-generated-password", "password_change_required": True, "version": "29.2.2-1"}

    def execute(command, **kwargs):
        assert kwargs["cancellable"] is False and kwargs["sudo"] is True
        assert "install.sh" in command
        stop.requested = True
        return guest.CommandResult("GDEPLOY_RESULT=" + json.dumps(result), "", 0)

    execute = Mock(side_effect=execute)
    monkeypatch.setattr(session, "_exec_result", execute)
    installed = session.install("fleetmanager", {}, fleetmanager={
        "mode": "online", "community_string": "community", "repository_token": "repository-token",
        "license_pem": "synthetic-license", "license_sha256": "a" * 64,
    })
    assert installed["services"][0]["password"] == "newly-generated-password"
    assert "newly-generated-password" in session._secrets
    with pytest.raises(RequestedStop):
        session.install("ubuntu", {})
    execute.assert_called_once()


def test_cancellation_bypass_is_limited_to_the_explicit_cleanup_command(session, stop):
    channel = command_channel(session, b"cleaned")
    stop.requested = True
    assert session._exec("rm -rf -- /home/gdeploy/.gdeploy-test", timeout=5, cancellable=False) == "cleaned"
    with pytest.raises(RequestedStop):
        session._exec("new provisioning operation", timeout=5)
    channel.exec_command.assert_called_once()
