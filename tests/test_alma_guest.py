import errno
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from passlib.hash import sha512_crypt

from gdeploy import alma_guest
from gdeploy.guest import GuestError


TREEINFO = """[general]
family = AlmaLinux
arch = x86_64
version = 10.0
[release]
name = AlmaLinux
version = 10.0
[tree]
arch = x86_64
[images-x86_64]
kernel = images/pxeboot/vmlinuz
initrd = images/pxeboot/initrd.img
[stage2]
mainimage = images/install.img
[variant-BaseOS]
repository = BaseOS
[variant-AppStream]
repository = AppStream
"""
GRUB = """set default="1"
set timeout=60
search --no-floppy --set=root -l 'AlmaLinux-10-0-x86_64-dvd'
menuentry 'Install AlmaLinux 10.0' {
  linux /images/pxeboot/vmlinuz inst.stage2=hd:LABEL=AlmaLinux-10-0-x86_64-dvd quiet
  initrd /images/pxeboot/initrd.img
}
menuentry 'Test this media & install AlmaLinux 10.0' {
  linux /images/pxeboot/vmlinuz inst.stage2=hd:LABEL=AlmaLinux-10-0-x86_64-dvd rd.live.check quiet
  initrd /images/pxeboot/initrd.img
}
menuentry 'Rescue' {
  linux /images/pxeboot/vmlinuz inst.stage2=hd:LABEL=AlmaLinux-10-0-x86_64-dvd inst.rescue quiet
  initrd /images/pxeboot/initrd.img
}
"""


@pytest.fixture
def spec():
    return {"name": "lab-alma", "role": "os_only", "ip_mode": "dhcp"}


@pytest.fixture
def keys():
    def key(seed):
        return ed25519.Ed25519PrivateKey.from_private_bytes(seed * 32).public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH,
        ).decode()
    return key(b"a"), key(b"b")


def post_script(kickstart):
    return kickstart.split("%post --erroronfail", 1)[1].split("\n", 1)[1].rsplit("%end", 1)[0]


def test_kickstart_hashes_password_and_uses_local_dvd_with_password_sudo(spec, keys):
    data, secrets = alma_guest.kickstart_data(spec, "gdeploy", "password with $'` shell chars", keys[0])
    assert "password with $'` shell chars" not in data
    assert "password with $'` shell chars" in secrets
    password_hash = re.search(r'--password="([^"]+)"', data)[1]
    assert sha512_crypt.verify("password with $'` shell chars", password_hash)
    assert "cdrom\n" in data
    assert 'repo --name="AppStream" --baseurl=file:///run/install/repo/AppStream' in data
    assert "rootpw --lock" in data and "PermitRootLogin no" in data
    assert "gdeploy ALL=(ALL) ALL" in data and "NOPASSWD" not in data
    assert "firewall --enabled --service=ssh" in data
    for name in ("open-vm-tools", "openssh-server", "python3", "firewalld", "sudo"):
        assert name in data
    assert "services --enabled=sshd,vmtoolsd,NetworkManager,firewalld" in data
    assert "--device=link --activate --onboot=yes --noipv6" in data
    assert "reqpart --add-boot" in data
    assert "part swap --fstype=swap --size=2048 --ondisk=sda" in data
    assert "part / --fstype=xfs --size=2048 --grow --ondisk=sda" in data
    assert "autopart" not in data and "--maxsize" not in data
    assert "clearpart --all --initlabel --drives=sda" in data
    assert "reboot --eject" in data
    assert "chmod 0600 " + alma_guest.COMPLETION_MARKER in data
    assert post_script(data).rstrip().endswith("chmod 0600 " + alma_guest.COMPLETION_MARKER)
    assert '"installer": "anaconda-kickstart"' in data


def test_static_kickstart_preserves_address_prefix_gateway_and_dns(spec, keys):
    spec.update(ip_mode="static", address="192.168.20.15/24", gateway="192.168.20.1", dns=["1.1.1.1", "8.8.8.8"])
    data, _ = alma_guest.kickstart_data(spec, "gdeploy", "password", keys[0])
    assert "--bootproto=static --ip=192.168.20.15 --netmask=255.255.255.0 --gateway=192.168.20.1" in data
    assert "--nameserver=1.1.1.1,8.8.8.8" in data
    assert "--bootproto=dhcp" not in data


def test_post_generates_and_labels_host_keys_before_validating_sshd(spec, keys):
    data, _ = alma_guest.kickstart_data(spec, "gdeploy", "password", keys[0])
    script = post_script(data)
    assert script.index("ssh-keygen -A") < script.index("restorecon -RF /etc/ssh") < script.index("sshd -t")
    assert script.index("sshd -t") < script.index(alma_guest.COMPLETION_MARKER)


@pytest.mark.parametrize("change", [
    {"name": "x\nrootpw password"}, {"name": "x'$(false)"}, {"ip_mode": "none"},
    {"ip_mode": "static", "address": "bad", "gateway": "192.168.20.1", "dns": ["1.1.1.1"]},
    {"ip_mode": "static", "address": "192.168.20.15/24", "gateway": "192.168.30.1", "dns": ["1.1.1.1"]},
    {"ip_mode": "static", "address": "192.168.20.15/24", "gateway": "192.168.20.1", "dns": []},
])
def test_unsafe_inputs_are_rejected_before_xorriso(spec, keys, change):
    spec.update(change)
    with pytest.raises(GuestError):
        alma_guest.kickstart_data(spec, "gdeploy", "password", keys[0])


def test_extra_ssh_keys_deduplicate_and_cannot_escape_post_script(spec, keys):
    comment = "GDEPLOY_POST_PY ' \" $(touch /tmp/pwned) %end"
    data, _ = alma_guest.kickstart_data(spec, "gdeploy", "password", keys[0],
                                        [keys[0] + " duplicate", keys[1] + " " + comment])
    assert data.count(keys[0]) == 1
    script = post_script(data)
    completed = subprocess.run(["/bin/bash", "-n"], input=script, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
    embedded = re.search("python3 - <<'GDEPLOY_POST_PY'\n(.*?)\nGDEPLOY_POST_PY", script, re.S)[1]
    compile(embedded, "kickstart-post.py", "exec")
    assert json.loads(re.search(r"keys.write_text\((.*)\)", embedded)[1]) == keys[0] + "\n" + keys[1] + " " + comment + "\n"
    assert script.count("\nGDEPLOY_POST_PY\n") == 1
    with pytest.raises(GuestError, match="Saved deployment SSH"):
        alma_guest.kickstart_data(spec, "gdeploy", "password", keys[0], ["not a key"])


@pytest.mark.parametrize("role", ["splunk", "elasticsearch", "kibana", "fleetmanager"])
def test_all_single_nic_roles_generate_kickstart(spec, keys, role):
    spec["role"] = role
    data, _ = alma_guest.kickstart_data(spec, "gdeploy", "password", keys[0])
    assert "--device=link" in data


@pytest.mark.parametrize("mode", ["dhcp", "static"])
def test_sensor_has_mac_bound_management_and_nonaddressed_monitoring(spec, keys, mode):
    spec.update(role="corelight_sensor", ip_mode=mode, dhcp_reserved=True,
                management_mac="00:50:56:11:22:33", monitor_mac="00:50:56:11:22:34")
    if mode == "static":
        spec.update(address="192.168.20.15/24", gateway="192.168.20.1", dns=["1.1.1.1"])
    data, _ = alma_guest.kickstart_data(spec, "gdeploy", "password", keys[0])
    network_directives = [line for line in data.splitlines() if line.startswith("network ")]
    assert len(network_directives) == 1
    assert "--device=00:50:56:11:22:33" in network_directives[0]
    assert "--bindto=mac" in network_directives[0]
    assert "00:50:56:11:22:34" not in network_directives[0]
    script = post_script(data)
    checked = subprocess.run(["/bin/bash", "-n"], input=script, text=True, capture_output=True)
    assert checked.returncode == 0, checked.stderr
    embedded = re.search("python3 - <<'GDEPLOY_NETWORK_PY'\n(.*?)\nGDEPLOY_NETWORK_PY", script, re.S)[1]
    compile(embedded, "kickstart-network.py", "exec")
    # Decode the generated literal instead of executing host network mutations.
    import ast
    expression = next(node.value for node in ast.parse(embedded).body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "data" for target in node.targets))
    configurations = json.loads(ast.literal_eval(expression.args[0]))
    monitor = configurations["profiles"]["gdeploymon"]
    assert "mac-address=00:50:56:11:22:34" in monitor
    assert "interface-name=gdeploymon" in monitor
    assert monitor.count("method=disabled") == 2
    assert monitor.count("never-default=true") == 2
    assert "address1=" not in monitor and "method=auto" not in monitor
    management = configurations["profiles"]["gdeploymgmt"]
    assert "mac-address=00:50:56:11:22:33" in management
    assert "interface-name=gdeploymgmt" in management
    assert ("method=auto" in management) is (mode == "dhcp")
    if mode == "static":
        assert "address1=192.168.20.15/24,192.168.20.1" in management
        assert "dns=1.1.1.1;" in management
    assert "MACAddress=00:50:56:11:22:34\n\n[Link]\nName=gdeploymon" in configurations["links"]["gdeploymon"]
    assert "no-auto-default=*" in embedded
    assert script.index("dracut --regenerate-all --force") < script.index(alma_guest.COMPLETION_MARKER)


@pytest.mark.parametrize("changes", [
    {"management_mac": None}, {"monitor_mac": "invalid"}, {"monitor_mac": "00:50:56:11:22:33"},
    {"monitor_mac": "01:50:56:11:22:34"}, {"dhcp_reserved": False},
])
def test_invalid_sensor_nic_identity_or_unreserved_dhcp_is_rejected(spec, keys, changes):
    spec.update(role="corelight_sensor", management_mac="00:50:56:11:22:33",
                monitor_mac="00:50:56:11:22:34", dhcp_reserved=True)
    spec.update(changes)
    with pytest.raises(GuestError, match="Software Sensor"):
        alma_guest.kickstart_data(spec, "gdeploy", "password", keys[0])


@pytest.mark.parametrize("kernel_command", ["linux", "linuxefi"])
def test_grub_patches_bios_uefi_preserves_labels_and_skips_rescue(kernel_command):
    original = GRUB.replace("  linux ", "  " + kernel_command + " ")
    changed = alma_guest.patch_grub(original)
    lines = [line for line in changed.splitlines() if "/images/pxeboot/vmlinuz" in line]
    assert all("inst.stage2=hd:LABEL=AlmaLinux-10-0-x86_64-dvd" in line for line in lines)
    assert all("inst.ks=cdrom:/gdeploy.ks inst.text" in line for line in lines[:2])
    assert "rd.live.check" not in changed
    assert "inst.ks=" not in lines[-1]
    assert "search --no-floppy --set=root -l 'AlmaLinux-10-0-x86_64-dvd'" in changed
    assert 'set default="1"' not in changed and "set timeout=60" not in changed
    sensor = alma_guest.patch_grub(original, network_macs=("00:50:56:11:22:33", "00:50:56:11:22:34"))
    assert "ifname=gdeploymgmt:00:50:56:11:22:33 ifname=gdeploymon:00:50:56:11:22:34" in sensor
    with pytest.raises(GuestError, match="no recognized"):
        alma_guest.patch_grub("menuentry 'Other' {}")


def test_profile_validation_uses_release_arch_and_local_repositories():
    assert alma_guest.parse_treeinfo(TREEINFO) == {
        "profile": "alma10-kickstart", "os_id": "almalinux", "version": "10.0", "arch": "x86_64",
    }
    for invalid in (
        TREEINFO.replace("family = AlmaLinux", "family = Rocky Linux"),
        TREEINFO.replace("version = 10.0", "version = 9.6"),
        TREEINFO.replace("x86_64", "aarch64"),
        TREEINFO.replace("x86_64", "x86_64_v2"),
        TREEINFO.replace("repository = AppStream", "repository = ../../../AppStream/x86_64/os"),
        TREEINFO.replace("repository = BaseOS", "repository = ."),
        "not an installer tree",
    ):
        with pytest.raises(GuestError, match="original AlmaLinux 10"):
            alma_guest.parse_treeinfo(invalid)


def fake_xorriso(output, captured, commands, *, readonly=True):
    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["env"]["TMPDIR"] == kwargs["env"]["TMP"] == kwargs["env"]["TEMP"]
        if "-extract" in command:
            index = command.index("-extract")
            path, local = command[index + 1:index + 3]
            Path(local).write_text(TREEINFO if path == "/.treeinfo" else GRUB)
            if readonly:
                Path(local).chmod(0o444)
        elif "-map" in command:
            for index, word in enumerate(command):
                if word == "-map":
                    captured[command[index + 2]] = Path(command[index + 1]).read_bytes()
            output.write_bytes(b"remastered")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
    return run


def test_builder_verifies_original_metadata_and_replays_both_boot_modes(tmp_path, monkeypatch, spec, keys):
    source, output = tmp_path / "renamed-ubuntu.iso", tmp_path / "build" / "alma.iso"
    source.write_bytes(b"original DVD")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    captured, commands, logs = {}, [], []
    run = fake_xorriso(output, captured, commands)

    def check_workspace(command, **kwargs):
        assert Path(kwargs["env"]["TMPDIR"]).parent == output.parent
        return run(command, **kwargs)

    monkeypatch.setattr(alma_guest.subprocess, "run", check_workspace)
    alma_guest.build_install_iso(source, output, spec, "gdeploy", "password-unique", keys[0],
                                 log=lambda text, level: logs.append(text))
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert output.stat().st_mode & 0o777 == 0o600
    assert set(captured) == {*alma_guest.BOOT_CONFIGS, "/gdeploy.ks"}
    assert "password-unique" not in captured["/gdeploy.ks"].decode()
    assert all(b"inst.ks=cdrom:/gdeploy.ks" in captured[path] for path in alma_guest.BOOT_CONFIGS)
    assert "-boot_image" in commands[-1]
    assert commands[-1][commands[-1].index("-boot_image") + 1:][:2] == ["any", "replay"]
    listing = next(command for command in commands if "-ls" in command)
    assert all(path in listing for path in alma_guest.DVD_FILES)
    assert any("GiB available" in line for line in logs)


def test_preflight_checks_dvd_contents_without_using_filename(tmp_path, monkeypatch):
    source = tmp_path / "ubuntu.iso"
    source.write_bytes(b"DVD")
    commands = []
    monkeypatch.setattr(alma_guest.subprocess, "run", fake_xorriso(None, {}, commands))
    assert alma_guest.validate_install_iso(source)["os_id"] == "almalinux"
    assert any("/.treeinfo" in command for command in commands)
    assert any("/AppStream/repodata/repomd.xml" in command for command in commands)


@pytest.mark.parametrize("failure, expected", [
    (OSError(errno.ENOSPC, "No space left on device"), "filesystem is full"),
    (OSError(errno.EDQUOT, "Disk quota exceeded"), "storage quota"),
    (PermissionError(errno.EACCES, "Permission denied"), "permission denied"),
    (FileNotFoundError(errno.ENOENT, "No such file", "xorriso"), "xorriso is missing"),
    (subprocess.TimeoutExpired("xorriso", 120, stderr=b"timed out"), "120-second time limit"),
    (subprocess.CalledProcessError(-9, "xorriso"), "container memory limit"),
])
def test_builder_preserves_source_and_cleans_partial_failure(tmp_path, monkeypatch, spec, keys, failure, expected):
    source, output = tmp_path / "source.iso", tmp_path / "output.iso"
    source.write_bytes(b"DVD")

    def fail(*args, **kwargs):
        output.write_bytes(b"partial")
        raise failure

    monkeypatch.setattr(alma_guest.subprocess, "run", fail)
    with pytest.raises(GuestError, match=expected):
        alma_guest.build_install_iso(source, output, spec, "gdeploy", "password", keys[0])
    assert source.read_bytes() == b"DVD"
    assert not output.exists()


def test_xorriso_errors_keep_step_and_redact_guest_password_hash_keys(tmp_path, monkeypatch, spec, keys):
    source, output = tmp_path / "source.iso", tmp_path / "output.iso"
    source.write_bytes(b"DVD")
    data, secrets = alma_guest.kickstart_data(spec, "gdeploy", "password-private", keys[0])
    monkeypatch.setattr(alma_guest, "kickstart_data", lambda *args: (data, secrets))
    logs = []

    def fail(command, **kwargs):
        output.write_bytes(b"partial")
        diagnostic = "\n".join(secrets) + "\nxorriso : FAILURE : Cannot find path '/.treeinfo'"
        raise subprocess.CalledProcessError(5, command, stderr=diagnostic.encode())

    monkeypatch.setattr(alma_guest.subprocess, "run", fail)
    with pytest.raises(GuestError, match="Inspect AlmaLinux DVD metadata.*status 5"):
        alma_guest.build_install_iso(source, output, spec, "gdeploy", "password-private", keys[0],
                                     log=lambda message, level: logs.append(message))
    diagnostic = "\n".join(logs)
    assert "Cannot find path '/.treeinfo'" in diagnostic
    assert all(secret not in diagnostic for secret in secrets)
    assert not output.exists()


def test_builder_refuses_source_overwrite_or_existing_destination(tmp_path, spec, keys):
    source = tmp_path / "source.iso"
    source.write_bytes(b"DVD")
    with pytest.raises(GuestError, match="separate readable"):
        alma_guest.build_install_iso(source, source, spec, "gdeploy", "password", keys[0])
    output = tmp_path / "output.iso"
    output.write_bytes(b"other deployment")
    with pytest.raises(GuestError, match="Refusing to overwrite"):
        alma_guest.build_install_iso(source, output, spec, "gdeploy", "password", keys[0])
    assert output.read_bytes() == b"other deployment"


@pytest.mark.skipif(shutil.which("xorriso") is None, reason="xorriso is not installed on this test host")
def test_real_xorriso_roundtrip_preserves_bios_uefi_dvd_and_source(tmp_path, spec, keys):
    """Exercise real ISO parsing/remastering; synthetic boot images do not boot a VM."""
    root = tmp_path / "dvd"
    root.mkdir()
    (root / ".treeinfo").write_text(TREEINFO)
    for path in alma_guest.DVD_FILES:
        file = root / path.removeprefix("/")
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(GRUB.replace("linux ", "linuxefi ") if path == "/EFI/BOOT/grub.cfg"
                        else GRUB if path == "/boot/grub2/grub.cfg" else "fixture repository/installer data\n")
    bios = root / "boot/grub2/eltorito.img"
    bios.write_bytes(b"\0" * 2048)
    uefi = root / "images/efiboot.img"
    uefi.write_bytes(b"\0" * (256 * 1024))
    source, output = tmp_path / "original.iso", tmp_path / "generated.iso"
    subprocess.run([
        "xorriso", "-as", "mkisofs", "-r", "-J", "-V", "AlmaLinux-10-0-x86_64-dvd", "-o", str(source),
        "-b", "boot/grub2/eltorito.img", "-c", "boot.catalog", "-no-emul-boot", "-boot-load-size", "4",
        "-boot-info-table", "-eltorito-alt-boot", "-e", "images/efiboot.img", "-no-emul-boot", str(root),
    ], check=True, capture_output=True)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert alma_guest.validate_install_iso(source)["profile"] == alma_guest.PROFILE
    alma_guest.build_install_iso(source, output, spec, "gdeploy", "password-unique", keys[0])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    report = subprocess.run(["xorriso", "-indev", str(output), "-report_el_torito", "plain"],
                            check=True, capture_output=True, text=True)
    assert "BIOS" in report.stdout + report.stderr
    assert "UEFI" in report.stdout + report.stderr
    extracted = tmp_path / "extracted"
    subprocess.run(["xorriso", "-osirrox", "on", "-indev", str(output), "-extract", "/", str(extracted)],
                   check=True, capture_output=True)
    assert "password-unique" not in (extracted / "gdeploy.ks").read_text()
    assert "inst.ks=cdrom:/gdeploy.ks" in (extracted / "boot/grub2/grub.cfg").read_text()
    assert "inst.ks=cdrom:/gdeploy.ks" in (extracted / "EFI/BOOT/grub.cfg").read_text()
    assert (extracted / "BaseOS/repodata/repomd.xml").read_bytes() == (root / "BaseOS/repodata/repomd.xml").read_bytes()
    assert (extracted / "AppStream/repodata/repomd.xml").read_bytes() == (root / "AppStream/repodata/repomd.xml").read_bytes()
