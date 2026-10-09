import hashlib
import errno
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import Mock

import paramiko
import pytest
import yaml
from passlib.hash import sha512_crypt

from gdeploy import guest


@pytest.fixture
def spec():
    return {"name": "lab-elasticsearch", "ip_mode": "dhcp"}


@pytest.fixture
def credentials():
    return {
        "elastic_password": "elastic-secret-unique",
        "splunk_password": "splunk-secret-unique",
        "kibana_encryption_key": "E" * 40,
        "kibana_security_key": "S" * 40,
        "kibana_reporting_key": "R" * 40,
    }


def test_autoinstall_hashes_password_and_configures_tools(spec):
    password = "unique-os-password"
    public_key = "ssh-rsa AAAATEST gdeploy"
    config = guest._autoinstall_data(spec, "gdeploy", password, public_key)
    serialized = yaml.safe_dump(config)
    assert password not in serialized
    install = yaml.safe_load(serialized)["autoinstall"]
    assert sha512_crypt.verify(password, install["identity"]["password"])
    assert install["identity"]["password"].startswith("$6$rounds=100000$")
    assert install["identity"]["hostname"] == spec["name"]
    assert install["ssh"]["authorized-keys"] == [public_key]
    assert "open-vm-tools" in install["packages"]
    assert install["shutdown"] == "reboot"
    assert install["refresh-installer"] == {"update": False}
    assert install["network"]["ethernets"]["gdeploy"]["dhcp4"] is True
    assert "NOPASSWD" not in serialized


def test_static_netplan_uses_prefix_route_dns_and_vmxnet3(spec):
    spec.update(ip_mode="static", address="192.168.10.25/24", gateway="192.168.10.1", dns=["192.168.10.1", "1.1.1.1"])
    config = guest._autoinstall_data(spec, "gdeploy", "password-unique", "ssh-rsa AAAA")
    network = config["autoinstall"]["network"]["ethernets"]["gdeploy"]
    assert network == {
        "match": {"driver": "vmxnet3"},
        "dhcp6": False,
        "dhcp4": False,
        "addresses": ["192.168.10.25/24"],
        "routes": [{"to": "default", "via": "192.168.10.1"}],
        "nameservers": {"addresses": ["192.168.10.1", "1.1.1.1"]},
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"gateway": "192.168.30.1"},
        {"address": "bad"},
        {"dns": []},
        {"dns": ["bad"]},
    ],
)
def test_reject_bad_static_network_before_building(spec, changes):
    spec.update(ip_mode="static", address="192.168.10.25/24", gateway="192.168.10.1", dns=["1.1.1.1"])
    spec.update(changes)
    with pytest.raises(guest.GuestError):
        guest._autoinstall_data(spec, "gdeploy", "unique-password", "ssh-rsa AAAA")


def test_grub_patches_standard_hwe_and_loopback_entries():
    original = """set timeout=30
set default=2
menuentry "Try or Install Ubuntu Server" {
    linux /casper/vmlinuz quiet ---
    initrd /casper/initrd
}
menuentry "HWE" {
    linux /casper/hwe-vmlinuz iso-scan/filename=${iso_path} --- quiet
    initrd /casper/hwe-initrd
}
"""
    modified = guest._patch_grub(original)
    linux = [line for line in modified.splitlines() if line.strip().startswith("linux ")]
    assert len(linux) == 2
    assert all(r"autoinstall noprompt ds=nocloud\;s=/cdrom/nocloud/ ---" in line for line in linux)
    assert all(line.count("---") == 1 for line in linux)
    assert "set timeout=30" not in modified
    assert "set default=2" not in modified
    assert "initrd /casper/hwe-initrd" in modified
    with pytest.raises(guest.GuestError, match="no recognized"):
        guest._patch_grub("menuentry Other {}")


@pytest.mark.parametrize("extracted_mode", [0o644, 0o444])
def test_iso_replays_boot_metadata_and_updates_media_manifest(tmp_path, monkeypatch, spec, extracted_mode):
    source, output = tmp_path / "source.iso", tmp_path / "output.iso"
    source.write_bytes(b"test source")
    captured = {}
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert Path(kwargs["env"]["TMPDIR"]).parent == output.parent
        assert kwargs["env"]["TMP"] == kwargs["env"]["TEMP"] == kwargs["env"]["TMPDIR"]
        if "-extract" in command:
            index = command.index("-extract")
            path, local = command[index + 1 : index + 3]
            if path == "/md5sum.txt":
                Path(local).write_text(
                    "0" * 32
                    + "  ./boot/grub/grub.cfg\n"
                    + "1" * 32
                    + "  ./boot/grub/loopback.cfg\n"
                    + "2" * 32
                    + "  ./casper/filesystem.squashfs\n"
                )
            else:
                Path(local).write_text("set timeout=30\n linux /casper/vmlinuz ---\n initrd /casper/initrd\n")
            Path(local).chmod(extracted_mode)
        else:
            for index, word in enumerate(command):
                if word == "-map":
                    captured[command[index + 2]] = Path(command[index + 1]).read_bytes()
            output.write_bytes(b"remastered")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(guest.subprocess, "run", run)
    guest.build_seed_iso(source, output, spec, "gdeploy", "secret-os-password", "ssh-rsa AAAA")
    assert commands[-1][commands[-1].index("-boot_image") + 1 :][:2] == ["any", "replay"]
    assert captured["/nocloud/user-data"].startswith(b"#cloud-config\n")
    assert b"secret-os-password" not in captured["/nocloud/user-data"]
    manifest = captured["/md5sum.txt"].decode()
    for path in ("/nocloud/user-data", "/nocloud/meta-data", "/boot/grub/grub.cfg", "/boot/grub/loopback.cfg"):
        digest = hashlib.md5(captured[path], usedforsecurity=False).hexdigest()
        assert f"{digest}  .{path}" in manifest
    assert "2" * 32 + "  ./casper/filesystem.squashfs" in manifest
    assert output.stat().st_mode & 0o777 == 0o600
    assert source.read_bytes() == b"test source"


def test_iso_failure_deletes_partial_output_and_never_exposes_tool_output(tmp_path, monkeypatch, spec):
    source, output = tmp_path / "source.iso", tmp_path / "output.iso"
    source.touch()

    def fail(*args, **kwargs):
        output.write_bytes(b"incomplete")
        raise subprocess.CalledProcessError(1, args[0], stderr=b"secret text")

    monkeypatch.setattr(guest.subprocess, "run", fail)
    with pytest.raises(guest.GuestError) as caught:
        guest.build_seed_iso(source, output, spec, "gdeploy", "password-unique", "ssh-rsa AAAA")
    assert "secret text" not in str(caught.value)
    assert not output.exists()


def test_iso_failure_logs_step_exit_space_and_redacts_generated_secrets(tmp_path, monkeypatch, spec):
    source, output = tmp_path / "source.iso", tmp_path / "output.iso"
    source.touch()
    data = guest._autoinstall_data(spec, "gdeploy", "password-unique", "ssh-rsa AAAA")
    password_hash = data["autoinstall"]["identity"]["password"]
    monkeypatch.setattr(guest, "_autoinstall_data", lambda *args, **kwargs: data)
    logs = []

    def fail(command, **kwargs):
        diagnostic = (
            "xorriso : FAILURE : Cannot find path '/boot/grub/grub.cfg' in loaded ISO\n"
            f"password-unique {password_hash} ssh-rsa AAAA\n"
            "password: another-secret\n"
            "-----BEGIN OPENSSH PRIVATE KEY-----\nprivate-material\n-----END OPENSSH PRIVATE KEY-----"
        )
        raise subprocess.CalledProcessError(5, command, stderr=diagnostic.encode())

    monkeypatch.setattr(guest.subprocess, "run", fail)
    with pytest.raises(guest.GuestError, match="Extract /boot/grub/grub.cfg.*status 5"):
        guest.build_seed_iso(source, output, spec, "gdeploy", "password-unique", "ssh-rsa AAAA", log=lambda text, level: logs.append((text, level)))
    messages = "\n".join(text for text, _ in logs)
    for secret in ("password-unique", password_hash, "ssh-rsa AAAA", "another-secret", "private-material"):
        assert secret not in messages
    assert "Cannot find path '/boot/grub/grub.cfg'" in messages
    assert "GiB available" in messages
    assert any(level == "error" for _, level in logs)
    assert not output.exists()


@pytest.mark.parametrize("failure, expected", [
    (OSError(errno.ENOSPC, "No space left on device"), "filesystem is full"),
    (OSError(errno.EDQUOT, "Disk quota exceeded"), "storage quota"),
    (PermissionError(errno.EACCES, "Permission denied"), "permission denied"),
    (OSError(errno.EROFS, "Read-only file system"), "read-only filesystem"),
    (FileNotFoundError(errno.ENOENT, "No such file", "xorriso"), "xorriso is missing"),
    (subprocess.TimeoutExpired("xorriso", 120, stderr=b"timed out"), "120-second time limit"),
    (subprocess.CalledProcessError(-9, "xorriso"), "container memory limit"),
])
def test_iso_build_failure_categories(tmp_path, monkeypatch, spec, failure, expected):
    source, output = tmp_path / "source.iso", tmp_path / "output.iso"
    source.touch()

    def fail(*args, **kwargs):
        output.write_bytes(b"partial")
        raise failure

    monkeypatch.setattr(guest.subprocess, "run", fail)
    with pytest.raises(guest.GuestError, match=expected):
        guest.build_seed_iso(source, output, spec, "gdeploy", "password-unique", "ssh-rsa AAAA")
    assert not output.exists()
    assert source.exists()


def test_iso_tool_log_redacts_before_bounding():
    secret = "SECRET" * 800
    lines = guest._iso_tool_log(("line\n" * 40) + secret + "\nlast diagnostic", [secret])
    assert len(lines) == 30
    assert lines[-1] == "last diagnostic"
    assert "SECRET" not in "\n".join(lines)
    assert "[redacted]" in lines


def test_elastic_and_kibana_require_verified_tls(credentials):
    elastic = {
        "ip": "192.168.10.10",
        "service_token": "private-token",
        "version": "9.1.4",
        "ca_pem": "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----",
    }
    es_config = guest._elasticsearch_config(elastic["ip"])
    assert es_config["xpack.security.http.ssl.enabled"] is True
    assert es_config["xpack.security.transport.ssl.enabled"] is True
    assert es_config["network.host"] == elastic["ip"]
    kibana = guest._kibana_config("192.168.10.11", elastic, credentials)
    assert kibana["elasticsearch.hosts"] == ["https://192.168.10.10:9200"]
    assert kibana["elasticsearch.ssl.verificationMode"] == "full"
    assert kibana["elasticsearch.serviceAccountToken"] == "private-token"
    assert "elasticsearch.username" not in kibana
    assert "elasticsearch.password" not in kibana
    assert kibana["server.ssl.enabled"] is True
    assert "subjectAltName=IP:" in guest._ELASTICSEARCH_SCRIPT
    assert "ssl.create_default_context(cafile=" in guest._KIBANA_SCRIPT
    assert "_create_unverified_context" not in guest._KIBANA_SCRIPT


def test_remote_scripts_are_valid_bash_and_embedded_python():
    for script in (guest._ELASTICSEARCH_SCRIPT, guest._KIBANA_SCRIPT, guest._SPLUNK_SCRIPT):
        completed = subprocess.run(["/bin/bash", "-n"], input=script, text=True, capture_output=True)
        assert completed.returncode == 0, completed.stderr
        snippets = re.findall(r"python3 - <<'PY'\n(.*?)\nPY", script, re.S)
        assert snippets
        for snippet in snippets:
            compile(snippet, "remote-provision.py", "exec")
        assert "set -x" not in script


def test_elasticsearch_clears_generated_settings_and_resets_after_start():
    script = guest._ELASTICSEARCH_SCRIPT
    assert "elastic-package.log 2>&1" in script
    assert "entry.startswith(('xpack.security.http.ssl.', 'xpack.security.transport.ssl.'))" in script
    assert script.index("keystore, 'remove'") < script.index("systemctl enable --now elasticsearch")
    assert script.index("systemctl enable --now elasticsearch") < script.index("elasticsearch-reset-password")
    assert "input=(password+'\\n'+password+'\\n').encode()" in script
    assert script.index("elasticsearch-reset-password") < script.index("/_cluster/health")
    assert "'kibana='+p['elastic']['version']" in guest._KIBANA_SCRIPT


def test_generated_ssh_keys_match():
    private, public = guest.generate_ssh_key()
    key = paramiko.RSAKey.from_private_key(io.StringIO(private))
    assert key.get_bits() >= 3072
    assert public.split()[:2] == [key.get_name(), key.get_base64()]


def test_host_key_is_captured_then_pinned():
    session = guest.GuestSession("192.168.10.25", "gdeploy", "private", "os-secret")
    key = Mock()
    key.get_name.return_value = "ssh-ed25519"
    key.get_base64.return_value = "first-key"
    policy = guest._PinnedHostKeyPolicy(session)
    policy.missing_host_key(None, session.ip, key)
    assert session.host_key == "ssh-ed25519 first-key"
    policy.missing_host_key(None, session.ip, key)
    key.get_base64.return_value = "different-key"
    with pytest.raises(guest.GuestHostKeyError, match="host key changed") as caught:
        policy.missing_host_key(None, session.ip, key)
    assert not isinstance(caught.value, guest.GuestConnectionError)


def test_initial_ssh_connect_never_falls_back_to_password(monkeypatch):
    client = Mock()
    monkeypatch.setattr(guest.paramiko, "SSHClient", lambda: client)
    monkeypatch.setattr(guest.paramiko.RSAKey, "from_private_key", lambda stream: "key")
    session = guest.GuestSession("192.168.10.25", "gdeploy", "private", "os-secret")
    session._connect()
    assert "password" not in client.connect.call_args.kwargs
    assert client.connect.call_args.kwargs["pkey"] == "key"
    assert client.connect.call_args.kwargs["look_for_keys"] is False
    assert client.connect.call_args.kwargs["allow_agent"] is False


def test_only_transient_ssh_failures_use_connection_error(monkeypatch):
    client = Mock()
    client.connect.side_effect = paramiko.AuthenticationException("Not ready")
    monkeypatch.setattr(guest.paramiko, "SSHClient", lambda: client)
    monkeypatch.setattr(guest.paramiko.RSAKey, "from_private_key", lambda stream: "key")
    session = guest.GuestSession("192.168.10.25", "gdeploy", "private", "os-secret")
    with pytest.raises(guest.GuestConnectionError):
        session._connect()
    client.connect.side_effect = guest.GuestHostKeyError("Changed key")
    with pytest.raises(guest.GuestHostKeyError):
        session._connect()
    monkeypatch.setattr(
        guest.paramiko.RSAKey, "from_private_key", Mock(side_effect=paramiko.SSHException("Invalid key"))
    )
    with pytest.raises(guest.GuestError, match="private key is invalid") as caught:
        session._connect()
    assert not isinstance(caught.value, guest.GuestConnectionError)


class FakeChannel:
    def __init__(self, output, status=0):
        self.output = output
        self.status = status
        self.command = None
        self.input = None

    def set_combine_stderr(self, value):
        pass

    def settimeout(self, value):
        pass

    def exec_command(self, command):
        self.command = command

    def sendall(self, data):
        self.input = data

    def shutdown_write(self):
        pass

    def recv_ready(self):
        return bool(self.output)

    def recv(self, size):
        data, self.output = self.output, b""
        return data

    def exit_status_ready(self):
        return True

    def recv_exit_status(self):
        return self.status

    def close(self):
        pass


def test_sudo_password_only_in_stdin_and_failure_details_are_redacted(credentials):
    session = guest.GuestSession("192.168.10.25", "gdeploy", "private", "os-secret")
    session._secrets.update(credentials.values())
    channel = FakeChannel(
        b'failed os-secret splunk-secret-unique\nGDEPLOY_RESULT={"service_token":"unseen-token"}\n', 1
    )
    session.client = Mock()
    session.client.get_transport.return_value.open_session.return_value = channel
    with pytest.raises(guest.GuestError) as caught:
        session._exec("/bin/bash /home/gdeploy/.gdeploy-test/install.sh", sudo=True)
    assert "os-secret" not in channel.command
    assert "sudo -k -S" in channel.command
    assert channel.input == b"os-secret\n"
    assert "os-secret" not in str(caught.value)
    assert "splunk-secret-unique" not in str(caught.value)
    assert "unseen-token" not in str(caught.value)
    assert "exit 1" in str(caught.value)


def test_cloud_init_errors_prevent_application_install(monkeypatch):
    session = guest.GuestSession("192.168.10.25", "gdeploy", "private", "os-secret")
    session.client = Mock()
    monkeypatch.setattr(
        session, "_exec_result", lambda *args, **kwargs: guest.CommandResult(
            json.dumps({"status": "error", "errors": ["failed package"]}), "", 1,
        )
    )
    with pytest.raises(guest.GuestError, match="reported errors") as caught:
        session.wait_ready()
    assert not isinstance(caught.value, guest.GuestConnectionError)


def test_service_credentials_are_returned_privately_not_logged(monkeypatch, credentials):
    session = guest.GuestSession("192.168.10.25", "gdeploy", "private", "os-secret")
    elastic = {
        "ip": session.ip,
        "ca_pem": "-----BEGIN CERTIFICATE-----\ntest",
        "service_token": "private-service-token",
        "version": "9.1.4",
    }
    monkeypatch.setattr(session, "_run_script", lambda *args, **kwargs: elastic)
    messages = []
    result = session.install("elasticsearch", credentials, log=messages.append)
    assert result["services"][0]["password"] == credentials["elastic_password"]
    assert result["elastic"]["service_token"] == elastic["service_token"]
    assert all(value not in "\n".join(messages) for value in credentials.values())
    assert elastic["service_token"] not in "\n".join(messages)


def _archive(tmp_path, additions=()):
    path = tmp_path / "splunk.tgz"
    with tarfile.open(path, "w:gz") as archive:
        elf = b"\x7fELF\x02\x01" + b"\x00" * 12 + (62).to_bytes(2, "little")
        for name, content in (("splunk/bin/splunk", b"shell"), ("splunk/bin/splunkd", elf)):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        for info in additions:
            archive.addfile(info)
    return path


def test_splunk_package_validation_rejects_tar_traversal_and_link_writes(tmp_path):
    guest._validate_splunk_archive(_archive(tmp_path))
    outside = tarfile.TarInfo("../../etc/passwd")
    with pytest.raises(guest.GuestError, match="unsafe file path"):
        guest._validate_splunk_archive(_archive(tmp_path, [outside]))
    link = tarfile.TarInfo("splunk/linked-dir")
    link.type = tarfile.SYMTYPE
    link.linkname = "."
    write = tarfile.TarInfo("splunk/linked-dir/file")
    with pytest.raises(guest.GuestError, match="linked directory"):
        guest._validate_splunk_archive(_archive(tmp_path, [link, write]))


@pytest.mark.parametrize("mode", ["dhcp", "static"])
def test_sensor_autoinstall_uses_minimal_source_and_two_mac_matched_interfaces(spec, mode):
    spec.update(role="corelight_sensor", management_mac="00:50:56:aa:bb:01", monitor_mac="00:50:56:aa:bb:02", ip_mode=mode, dhcp_reserved=True)
    if mode == "static":
        spec.update(address="192.168.50.2/24", gateway="192.168.50.1", dns=["192.168.50.1"])
    install = guest._autoinstall_data(spec, "gdeploy", "password", "ssh-rsa AAAA")["autoinstall"]
    assert install["source"] == {"id": "ubuntu-server-minimal", "search_drivers": False}
    networks = install["network"]["ethernets"]
    assert networks["gdeploy"]["match"] == {"macaddress": spec["management_mac"]}
    assert networks["gdeploy"]["set-name"] == "gdeploymgmt"
    assert networks["gdeploy"]["dhcp4"] == (mode == "dhcp")
    assert networks["monitoring"] == {
        "match": {"macaddress": spec["monitor_mac"]}, "set-name": "gdeploymon", "dhcp4": False,
        "dhcp6": False, "accept-ra": False, "link-local": [], "optional": True,
    }
    assert "vmxnet3" not in str(networks)
    if mode == "static":
        assert networks["gdeploy"]["addresses"] == ["192.168.50.2/24"]


@pytest.mark.parametrize("change", [
    {"management_mac": None}, {"monitor_mac": "bad"}, {"monitor_mac": "01:50:56:aa:bb:02"},
    {"monitor_mac": "00:50:56:aa:bb:01"}, {"dhcp_reserved": False},
])
def test_sensor_autoinstall_rejects_unknown_or_duplicate_nic_identity_and_unreserved_dhcp(spec, change):
    spec.update(role="corelight_sensor", management_mac="00:50:56:aa:bb:01", monitor_mac="00:50:56:aa:bb:02", dhcp_reserved=True)
    spec.update(change)
    with pytest.raises(guest.GuestError, match="Software Sensor"):
        guest._autoinstall_data(spec, "gdeploy", "password", "ssh-rsa AAAA")
