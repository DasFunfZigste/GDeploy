"""AlmaLinux 10 DVD validation and unattended Anaconda/Kickstart media.

The source DVD remains immutable. xorriso replays its existing BIOS/UEFI boot
metadata and volume label while mapping only the private Kickstart and boot
configuration copies. Installation packages come from the full DVD, so initial
OS installation does not depend on an external package mirror.
"""

from __future__ import annotations

import configparser
import errno
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from passlib.hash import sha512_crypt

from .guest import GuestError, _iso_tool_log
from .ssh_keys import SSHKeyError, normalize_ssh_public_keys


PROFILE = "alma10-kickstart"
COMPLETION_MARKER = "/var/lib/gdeploy/os-install-complete.json"
BOOT_CONFIGS = ("/boot/grub2/grub.cfg", "/EFI/BOOT/grub.cfg")
DVD_FILES = (
    "/BaseOS/repodata/repomd.xml", "/AppStream/repodata/repomd.xml",
    "/images/pxeboot/vmlinuz", "/images/pxeboot/initrd.img", "/images/install.img",
    *BOOT_CONFIGS,
)


def parse_treeinfo(text: str) -> dict:
    """Approve DVD metadata, never an ISO's filename or advertised volume label."""
    data = configparser.ConfigParser(interpolation=None)
    try:
        data.read_string(text)
        family = data.get("general", "family")
        version = data.get("release", "version")
        arch = data.get("tree", "arch")
        if (
            family != "AlmaLinux" or data.get("release", "name") != "AlmaLinux"
            or not re.fullmatch(r"10(?:\.\d+)?", version)
            or arch != "x86_64"
            or data.get("general", "arch") != arch
            or data.get("variant-BaseOS", "repository") != "BaseOS"
            or data.get("variant-AppStream", "repository") != "AppStream"
            or data.get("images-x86_64", "kernel") != "images/pxeboot/vmlinuz"
            or data.get("images-x86_64", "initrd") != "images/pxeboot/initrd.img"
            or data.get("stage2", "mainimage") != "images/install.img"
        ):
            raise ValueError("unsupported installer tree")
    except (configparser.Error, ValueError, TypeError) as exc:
        raise GuestError(
            "Select an original AlmaLinux 10 x86_64 full DVD ISO with BaseOS and AppStream packages; "
            "boot, minimal, live, and other architecture images are not supported by this installer."
        ) from exc
    return {"profile": PROFILE, "os_id": "almalinux", "version": version, "arch": arch}


def _verify_tree(source_iso: Path, root: Path, run) -> dict:
    treeinfo = root / "treeinfo"
    run(["xorriso", "-abort_on", "FAILURE", "-osirrox", "on", "-indev", str(source_iso),
         "-extract", "/.treeinfo", str(treeinfo)], 120, "Inspect AlmaLinux DVD metadata")
    if treeinfo.stat().st_size > 64 * 1024:
        raise GuestError("AlmaLinux ISO metadata is larger than expected; select an original full DVD ISO.")
    try:
        metadata = parse_treeinfo(treeinfo.read_text(encoding="utf-8"))
    except UnicodeError as exc:
        raise GuestError("AlmaLinux ISO metadata is unreadable; select the original x86_64 full DVD ISO.") from exc
    # A boot ISO can advertise the same release. Verify local package repository
    # metadata and installer files before allowing an unattended DVD installation.
    run(["xorriso", "-abort_on", "FAILURE", "-indev", str(source_iso), "-ls", *DVD_FILES, "--"],
        120, "Verify AlmaLinux DVD repositories and boot files")
    return metadata


def validate_install_iso(
    source_iso: Path, *, log: Callable[[str, str], None] | None = None, workspace_root: Path | None = None,
) -> dict:
    """Validate the AlmaLinux profile in preflight using the ISO's internal files."""
    source_iso = Path(source_iso)
    if not source_iso.is_file():
        raise GuestError("The AlmaLinux OS ISO is missing or unreadable; select it again in Setup.")
    try:
        if workspace_root is not None:
            workspace_root = Path(workspace_root)
            workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if workspace_root.is_symlink():
                raise GuestError("The AlmaLinux validation workspace must not be a symbolic link.")
        with tempfile.TemporaryDirectory(prefix="gdeploy-alma-check-", dir=workspace_root) as folder:
            root = Path(folder)
            environment = dict(os.environ, TMPDIR=str(root), TMP=str(root), TEMP=str(root))

            def run(command, timeout, step):
                try:
                    return subprocess.run(command, check=True, capture_output=True, timeout=timeout, env=environment)
                except (OSError, subprocess.SubprocessError) as exc:
                    if log:
                        for line in _iso_tool_log(getattr(exc, "stderr", ""), ()):
                            log("xorriso: " + line, "error")
                    raise GuestError(
                        "Could not verify the AlmaLinux 10 full DVD ISO; check xorriso, the internal installer "
                        "metadata, and local BaseOS/AppStream repositories. Select the original x86_64 DVD image."
                    ) from exc

            return _verify_tree(source_iso, root, run)
    except OSError as exc:
        raise GuestError("Unable to create or read the AlmaLinux media-validation workspace; check data-volume space and permissions.") from exc


def _keys(automation_key: str, extra_keys: list[str] | None) -> list[str]:
    if not isinstance(automation_key, str) or not automation_key or any(c in automation_key for c in "\r\n\x00"):
        raise GuestError("Invalid SSH public key.")
    try:
        additional = normalize_ssh_public_keys(extra_keys if extra_keys is not None else [])
    except SSHKeyError:
        raise GuestError("Saved deployment SSH public keys are invalid. Review SSH access in Setup before redeploying.") from None
    identity = " ".join(automation_key.split()[:2])
    return [automation_key] + [key for key in additional if " ".join(key.split()[:2]) != identity]


def _sensor_macs(spec: dict) -> tuple[str, str] | None:
    if spec.get("role") != "corelight_sensor":
        return None
    macs = [spec.get(key) for key in ("management_mac", "monitor_mac")]
    if any(
        not isinstance(mac, str) or not re.fullmatch(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", mac)
        or int(mac[:2], 16) & 1 or mac.lower() == "00:00:00:00:00:00"
        for mac in macs
    ) or macs[0].lower() == macs[1].lower():
        raise GuestError("Software Sensor installation requires two distinct, valid management and monitoring MAC addresses.")
    if spec.get("ip_mode") == "dhcp" and spec.get("dhcp_reserved") is not True:
        raise GuestError("Software Sensor management DHCP requires a reserved IPv4 address.")
    return macs[0].lower(), macs[1].lower()


def _sensor_post_network(spec: dict, macs: tuple[str, str] | None) -> str:
    if macs is None:
        return ""
    management_mac, monitor_mac = macs
    if spec["ip_mode"] == "dhcp":
        management_ipv4 = "method=auto\nmay-fail=false\n"
    else:
        address = str(ipaddress.IPv4Interface(spec["address"]))
        gateway = str(ipaddress.IPv4Address(spec["gateway"]))
        dns = ";".join(str(ipaddress.IPv4Address(value)) for value in spec["dns"])
        management_ipv4 = f"method=manual\naddress1={address},{gateway}\ndns={dns};\nmay-fail=false\n"
    profiles = {}
    links = {}
    for interface, mac, ipv4 in (
        ("gdeploymgmt", management_mac, management_ipv4),
        ("gdeploymon", monitor_mac, "method=disabled\nnever-default=true\n"),
    ):
        profiles[interface] = (
            f"[connection]\nid={interface}\ntype=ethernet\ninterface-name={interface}\nautoconnect=true\n"
            f"autoconnect-priority=100\n\n[ethernet]\nmac-address={mac}\n\n[ipv4]\n{ipv4}\n"
            "[ipv6]\nmethod=disabled\nnever-default=true\n"
        )
        links[interface] = f"[Match]\nMACAddress={mac}\n\n[Link]\nName={interface}\n"
    encoded = repr(json.dumps({"profiles": profiles, "links": links}))
    return f'''# Persist distinct NIC identities and prohibit automatic monitor DHCP/RA.
python3 - <<'GDEPLOY_NETWORK_PY'
import json
from pathlib import Path

data = json.loads({encoded})
connections = Path("/etc/NetworkManager/system-connections")
connections.mkdir(mode=0o700, parents=True, exist_ok=True)
# Anaconda generated only the management connection. Replace installer profiles
# before first boot, avoiding an obsolete interface-name binding after renaming.
for path in connections.glob("*.nmconnection"):
    path.unlink()
for name, contents in data["profiles"].items():
    path = connections / (name + ".nmconnection")
    path.write_text(contents)
    path.chmod(0o600)
links = Path("/etc/systemd/network")
links.mkdir(mode=0o755, parents=True, exist_ok=True)
for name, contents in data["links"].items():
    path = links / ("10-" + name + ".link")
    path.write_text(contents)
    path.chmod(0o644)
configuration = Path("/etc/NetworkManager/conf.d/90-gdeploy-no-auto-default.conf")
configuration.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
configuration.write_text("[main]\\nno-auto-default=*\\n")
configuration.chmod(0o644)
GDEPLOY_NETWORK_PY
restorecon -RF /etc/NetworkManager /etc/systemd/network
# Regenerate the target initramfs so renamed interfaces apply from early boot.
dracut --regenerate-all --force
'''


def kickstart_data(
    spec: dict, username: str, password: str, ssh_public_key: str,
    authorized_ssh_keys: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Generate a validated Kickstart; shell text contains no plaintext password."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", username or ""):
        raise GuestError("AlmaLinux username must start with a letter and be at most 32 characters.")
    hostname = spec.get("name")
    if not isinstance(hostname, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname):
        raise GuestError("Invalid AlmaLinux hostname.")
    if not isinstance(password, str) or not password or any(c in password for c in "\r\n\x00"):
        raise GuestError("Invalid OS password.")
    macs = _sensor_macs(spec)
    keys = _keys(ssh_public_key, authorized_ssh_keys)
    password_hash = sha512_crypt.using(rounds=100000).hash(password)
    network = f"network --device={macs[0] if macs else 'link'} --activate --onboot=yes --noipv6 --hostname={hostname}"
    if macs:
        network += " --bindto=mac"
    if spec.get("ip_mode") == "dhcp":
        network += " --bootproto=dhcp"
    elif spec.get("ip_mode") == "static":
        try:
            address = ipaddress.IPv4Interface(spec["address"])
            gateway = ipaddress.IPv4Address(spec["gateway"])
            dns = [str(ipaddress.IPv4Address(value)) for value in spec["dns"]]
            if gateway not in address.network or not dns:
                raise ValueError("gateway/DNS")
        except (ValueError, TypeError, KeyError) as exc:
            raise GuestError("Invalid static network configuration; supply a gateway in the subnet and IPv4 DNS servers.") from exc
        network += (f" --bootproto=static --ip={address.ip} --netmask={address.netmask}"
                    f" --gateway={gateway} --nameserver={','.join(dns)}")
    else:
        raise GuestError("IP mode must be dhcp or static.")

    marker = json.dumps({"schema": 1, "os_id": "almalinux", "major": 10,
                         "hostname": hostname, "installer": "anaconda-kickstart"}, sort_keys=True)
    # Keys are written with Python JSON encoding rather than a shell heredoc:
    # administrator-supplied key comments cannot terminate a heredoc or execute
    # shell text. Every account/path value is separately restricted above.
    encoded_keys = json.dumps("\n".join(keys) + "\n", ensure_ascii=True)
    sensor_network = _sensor_post_network(spec, macs)
    text = f'''#version=RHEL10
text
cdrom
repo --name="AppStream" --baseurl=file:///run/install/repo/AppStream
lang en_US.UTF-8
keyboard --vckeymap=us --xlayouts=us
timezone UTC --utc
rootpw --lock
user --name={username} --groups=wheel --password="{password_hash}" --iscrypted
{network}
selinux --enforcing
firewall --enabled --service=ssh
services --enabled=sshd,vmtoolsd,NetworkManager,firewalld
firstboot --disable
ignoredisk --only-use=sda
zerombr
clearpart --all --initlabel --drives=sda
reqpart --add-boot
part swap --fstype=swap --size=2048 --ondisk=sda
part / --fstype=xfs --size=2048 --grow --ondisk=sda
bootloader --location=mbr --boot-drive=sda
reboot --eject

%packages
@core
openssh-server
open-vm-tools
python3
sudo
NetworkManager
dracut
ca-certificates
curl
openssl
firewalld
%end

%post --erroronfail --interpreter=/bin/bash --log=/root/gdeploy-kickstart-post.log
set -euo pipefail
umask 077
install -d -m 0700 -o {username} -g {username} /home/{username}/.ssh
python3 - <<'GDEPLOY_POST_PY'
import os
from pathlib import Path
import pwd

account = pwd.getpwnam("{username}")
keys = Path("/home/{username}/.ssh/authorized_keys")
keys.write_text({encoded_keys})
keys.chmod(0o600)
os.chown(keys, account.pw_uid, account.pw_gid)
GDEPLOY_POST_PY
restorecon -RF /home/{username}/.ssh
install -d -m 0755 /etc/ssh/sshd_config.d
printf '%s\\n' 'PasswordAuthentication yes' 'PubkeyAuthentication yes' 'PermitRootLogin no' > /etc/ssh/sshd_config.d/00-gdeploy.conf
chmod 0644 /etc/ssh/sshd_config.d/00-gdeploy.conf
printf '%s\\n' '{username} ALL=(ALL) ALL' > /etc/sudoers.d/gdeploy
chmod 0440 /etc/sudoers.d/gdeploy
visudo -cf /etc/sudoers.d/gdeploy
ssh-keygen -A
restorecon -RF /etc/ssh
sshd -t
systemctl enable sshd.service vmtoolsd.service NetworkManager.service firewalld.service
{sensor_network}# This marker is written last; failed post-install setup never claims success.
install -d -m 0700 -o root -g root /var/lib/gdeploy
printf '%s\\n' '{marker}' > {COMPLETION_MARKER}
chmod 0600 {COMPLETION_MARKER}
%end
'''
    return text, [password, password_hash, *keys, *(key.split()[1] for key in keys if len(key.split()) >= 2)]


def patch_grub(text: str, *, network_macs: tuple[str, str] | None = None) -> str:
    """Patch real AlmaLinux 10 BIOS and UEFI GRUB entries and keep stage2 labels."""
    modified, found = [], False
    for line in text.splitlines():
        if re.match(r"\s*linux(?:efi)?\s+/images/pxeboot/vmlinuz(?:\s|$)", line) and "inst.rescue" not in line:
            found = True
            # Remastering changes the ISO's implanted checksum. The original
            # publisher SHA-256 was verified by GDeploy before this build.
            line = re.sub(r"\s+(?:rd\.live\.check|inst\.ks=\S+|inst\.text)(?=\s|$)", "", line).rstrip()
            line += " inst.ks=cdrom:/gdeploy.ks inst.text"
            if network_macs:
                line += f" ifname=gdeploymgmt:{network_macs[0]} ifname=gdeploymon:{network_macs[1]}"
        if re.match(r"\s*set\s+timeout=", line):
            line = "set timeout=1"
        if re.match(r"\s*set\s+default=", line):
            line = "set default=0"
        modified.append(line)
    if not found:
        raise GuestError("AlmaLinux ISO has no recognized Anaconda GRUB installation entry.")
    return "set default=0\nset timeout=1\n" + "\n".join(modified) + "\n"


def build_install_iso(
    source_iso: Path, output_iso: Path, spec: dict, username: str, password: str, ssh_public_key: str,
    *, log: Callable[[str, str], None] | None = None, authorized_ssh_keys: list[str] | None = None,
) -> None:
    """Create a private, bootable full-DVD installer, preserving the source ISO."""
    source_iso, output_iso = Path(source_iso), Path(output_iso)
    if not source_iso.is_file() or source_iso.resolve() == output_iso.resolve():
        raise GuestError("A separate readable AlmaLinux 10 x86_64 full DVD ISO is required.")
    if output_iso.exists():
        raise GuestError("Refusing to overwrite an existing deployment ISO.")
    kickstart, secrets = kickstart_data(spec, username, password, ssh_public_key, authorized_ssh_keys)
    step = "Create installation workspace"

    def emit(message, level="info"):
        if log:
            log(message, level)

    def storage_details():
        try:
            usage = shutil.disk_usage(output_iso.parent)
            emit(f"Build filesystem {output_iso.parent}: {usage.free / 1024**3:.2f} GiB available, "
                 f"{usage.used / 1024**3:.2f} GiB used of {usage.total / 1024**3:.2f} GiB. "
                 "ISO workspace uses the data volume, not /tmp.")
        except OSError:
            emit("Could not read available space for the build filesystem.", "warning")

    try:
        output_iso.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        emit(f"Source AlmaLinux ISO: {source_iso.name} ({source_iso.stat().st_size / 1024**3:.2f} GiB).")
        storage_details()
        with tempfile.TemporaryDirectory(prefix="gdeploy-alma-iso-", dir=output_iso.parent) as folder:
            root = Path(folder)
            environment = dict(os.environ, TMPDIR=str(root), TMP=str(root), TEMP=str(root))

            def run(command, timeout, label):
                nonlocal step
                step = label
                emit(step)
                try:
                    result = subprocess.run(command, check=True, capture_output=True, timeout=timeout, env=environment)
                except subprocess.SubprocessError as exc:
                    for line in _iso_tool_log(getattr(exc, "stderr", ""), secrets):
                        emit("xorriso: " + line, "error")
                    raise
                for line in _iso_tool_log(result.stderr, secrets):
                    emit("xorriso: " + line)
                emit(f"{step}: complete (xorriso exit 0).")
                return result

            metadata = _verify_tree(source_iso, root, run)
            emit(f"Verified AlmaLinux {metadata['version']} {metadata['arch']} full DVD installation media.")
            files = {}
            for number, path in enumerate(BOOT_CONFIGS):
                local = root / f"grub-{number}.cfg"
                run(["xorriso", "-abort_on", "FAILURE", "-osirrox", "on", "-indev", str(source_iso),
                     "-extract", path, str(local)], 120, f"Extract {path}")
                step = f"Update {path} for unattended installation"
                local.chmod(0o644)
                local.write_text(patch_grub(local.read_text(), network_macs=_sensor_macs(spec)))
                files[path] = local
            step = "Write unattended AlmaLinux installation settings"
            local = root / "gdeploy.ks"
            local.write_text(kickstart)
            local.chmod(0o600)
            files["/gdeploy.ks"] = local
            command = ["xorriso", "-abort_on", "FAILURE", "-indev", str(source_iso), "-outdev", str(output_iso),
                       "-boot_image", "any", "replay"]
            for path, local in files.items():
                command.extend(["-map", str(local), path])
            command.extend(["-commit", "-end"])
            run(command, 1800, "Write bootable unattended AlmaLinux installation ISO")
            output_iso.chmod(0o600)
            emit(f"AlmaLinux installation ISO prepared: {output_iso.stat().st_size / 1024**3:.2f} GiB.")
    except (OSError, subprocess.SubprocessError, GuestError) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            reason = f"xorriso exceeded the {exc.timeout}-second time limit"
        elif isinstance(exc, subprocess.CalledProcessError):
            reason = f"xorriso exited with status {exc.returncode}; review the tool output in Deployment logs"
            if exc.returncode < 0:
                reason += "; the process was terminated by a signal, so check the container memory limit and host logs"
        elif isinstance(exc, OSError):
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                reason = "the build filesystem is full or its storage quota was reached; free space on the data volume"
            elif exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                reason = "permission denied or read-only filesystem; check data-volume access for container UID 10001"
            elif isinstance(exc, FileNotFoundError) and exc.filename == "xorriso":
                reason = "xorriso is missing; use the supplied GDeploy Docker image"
            else:
                reason = f"filesystem error {exc.errno}: {exc.strerror or 'unable to read or write installation files'}"
        else:
            reason = str(exc)
        message = f"ISO build failed during '{step}': {reason}."
        emit(message, "error")
        storage_details()
        try:
            output_iso.unlink(missing_ok=True)
        except OSError:
            emit("Could not remove the partial installation ISO; check data-volume permissions.", "warning")
        raise GuestError(message) from exc
