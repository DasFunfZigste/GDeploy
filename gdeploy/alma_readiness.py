"""Independent readiness evidence for Alma's Kickstart installation."""

import re

PROBE = r'''
import json
import os
import platform
import stat
from pathlib import Path
import subprocess

def output(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError("OS readiness probe command failed: " + args[0])
    return result.stdout.strip()

release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
root = output(["findmnt", "-n", "-o", "SOURCE,FSTYPE", "/"]).split()
services = {}
for name in ("sshd.service", "vmtoolsd.service"):
    properties = output(["systemctl", "show", name, "-p", "LoadState,ActiveState,SubState,Result"])
    services[name] = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
marker = None
marker_safe = False
directory_descriptor = None
try:
    directory_descriptor = os.open("/var/lib/gdeploy", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    parent = os.fstat(directory_descriptor)
    # A present but unsafe marker location is a failed check, not a reason
    # to keep waiting indefinitely. Open relative to the verified directory
    # descriptor so replacement of the pathname cannot change this read.
    marker = "invalid"
    if stat.S_ISDIR(parent.st_mode) and parent.st_uid == 0 and not parent.st_mode & 0o022:
        descriptor = os.open("os-install-complete.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory_descriptor)
        with os.fdopen(descriptor, "r") as stream:
            info = os.fstat(stream.fileno())
            if (stat.S_ISREG(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600
                    and 0 < info.st_size <= 4096):
                marker = json.loads(stream.read(4096))
                marker_safe = True
except FileNotFoundError:
    marker = None
except (OSError, ValueError):
    marker = "invalid"
finally:
    if directory_descriptor is not None:
        os.close(directory_descriptor)
print(json.dumps({
    "os_id": release.get("ID", "").strip('"'),
    "os_version": release.get("VERSION_ID", "").strip('"'),
    "architecture": platform.machine(),
    "sudo": os.geteuid() == 0,
    "root_source": root[0] if len(root) == 2 else "",
    "root_fstype": root[1] if len(root) == 2 else "",
    "hostname": output(["hostname"]),
    "installer_marker": marker,
    "installer_marker_safe": marker_safe,
    "services": services,
}))
'''


def pending_reason(evidence, hostname):
    # Import lazily: the probe has no dependency on the Ubuntu readiness code.
    from .guest import GuestError

    if not isinstance(evidence, dict):
        raise GuestError("AlmaLinux readiness probe returned an unsupported response.")
    if evidence.get("sudo") is not True:
        raise GuestError("Could not verify sudo access for the GDeploy account on AlmaLinux.")
    version = evidence.get("os_version")
    source = evidence.get("root_source")
    if (evidence.get("os_id") != "almalinux" or not isinstance(version, str)
            or not re.fullmatch(r"10(?:\.\d+)*", version)
            or evidence.get("architecture") != "x86_64"
            or not isinstance(source, str)
            or not re.fullmatch(r"/dev/(?:[A-Za-z0-9_+-]+/)*[A-Za-z0-9_+-]+", source)
            or re.match(r"/dev/(?:loop|sr|ram|fd)\d+(?:$|/)|/dev/mapper/live-(?:rw|base)$", source)
            or evidence.get("root_fstype") not in {"xfs", "ext4"}):
        raise GuestError("Readiness did not find an installed AlmaLinux 10 x86_64 disk root; check the VM console.")
    observed_hostname = evidence.get("hostname")
    if not isinstance(observed_hostname, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", observed_hostname):
        raise GuestError("AlmaLinux readiness probe did not report a valid guest hostname.")
    if hostname is not None and (not isinstance(hostname, str) or observed_hostname != hostname):
        raise GuestError("AlmaLinux guest hostname does not match this deployment; check its network address.")
    marker = evidence.get("installer_marker")
    pending = []
    if marker is None:
        pending.append("Kickstart completion marker is not present")
    elif (evidence.get("installer_marker_safe") is not True or not isinstance(marker, dict)
          or type(marker.get("schema")) is not int or marker.get("schema") != 1
          or marker.get("os_id") != "almalinux" or type(marker.get("major")) is not int or marker.get("major") != 10
          or marker.get("installer") != "anaconda-kickstart"
          or marker.get("hostname") != evidence.get("hostname")):
        raise GuestError("AlmaLinux installer completion evidence is unsafe or does not match this guest.")
    services = evidence.get("services")
    if not isinstance(services, dict):
        raise GuestError("AlmaLinux readiness probe did not report service states.")
    for name in ("sshd.service", "vmtoolsd.service"):
        state = services.get(name)
        if not isinstance(state, dict) or state.get("LoadState") != "loaded":
            raise GuestError(f"AlmaLinux readiness could not find {name}.")
        if state.get("ActiveState") == "failed" or state.get("Result") != "success":
            raise GuestError(f"AlmaLinux readiness found a failed {name}; inspect its guest journal.")
        if state.get("ActiveState") != "active" or state.get("SubState") != "running":
            pending.append(name + " is not running")
    return "; ".join(pending) or None
