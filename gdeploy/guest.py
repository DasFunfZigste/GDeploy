"""Ubuntu autoinstall media and SSH application provisioning.

Secret material is transferred using SFTP, never command arguments. The first
successful SSH connection uses trust on first use; callers persist ``host_key``
and supply it on every later connection to the VM.
"""

from __future__ import annotations

import io
import errno
import hashlib
import ipaddress
import json
import os
import posixpath
import re
import secrets as secret_tools
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import paramiko
import yaml
from passlib.hash import sha512_crypt

from .ssh_keys import SSHKeyError, normalize_ssh_public_keys


class GuestError(RuntimeError):
    """A sanitized failure provisioning the guest."""


class GuestConnectionError(GuestError):
    """A transient connection/authentication failure while the guest boots."""


class GuestHostKeyError(GuestError):
    """A fatal mismatch with a previously observed SSH host identity."""


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int


# Read only completion evidence, never installer configuration or user-data:
# those files can contain credentials. Run with sudo to verify app-install access.
_READINESS_PROBE = r'''
import json
import os
from pathlib import Path
import subprocess

def output(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError("OS readiness probe command failed: " + args[0])
    return result.stdout.strip()

root = output(["findmnt", "-n", "-o", "SOURCE,FSTYPE", "/"]).split()
os_id = ""
for line in Path("/etc/os-release").read_text().splitlines():
    if line.startswith("ID="):
        os_id = line[3:].strip('"')
services = {}
for name in ("ssh.service", "open-vm-tools.service"):
    properties = output(["systemctl", "show", name, "-p",
                         "LoadState,ActiveState,SubState,Result"])
    services[name] = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
print(json.dumps({
    "os_id": os_id,
    "sudo": os.geteuid() == 0,
    "root_source": root[0] if len(root) == 2 else "",
    "root_fstype": root[1] if len(root) == 2 else "",
    "boot_finished": Path("/var/lib/cloud/instance/boot-finished").is_file(),
    "disabled_marker": Path("/etc/cloud/cloud-init.disabled").is_file(),
    "installer_log": Path("/var/log/installer/curtin-install.log").is_file(),
    "services": services,
}))
'''


def _installed_system_pending(evidence: dict, *, disabled: bool) -> str | None:
    """Reject unsafe evidence; return a reason to wait for normal boot progress."""
    if evidence.get("sudo") is not True:
        raise GuestError("OS readiness check could not verify sudo access for the GDeploy account.")
    if (
        evidence.get("os_id") != "ubuntu"
        or not str(evidence.get("root_source", "")).startswith("/dev/")
        or evidence.get("root_fstype") not in ("ext4", "xfs", "btrfs")
    ):
        raise GuestError("OS readiness check did not find an installed Ubuntu disk root; check the VM console.")
    if disabled and (evidence.get("disabled_marker") is not True or evidence.get("installer_log") is not True):
        raise GuestError("Cloud-init is disabled without verified Ubuntu installer completion evidence.")
    services = evidence.get("services")
    if not isinstance(services, dict):
        raise GuestError("OS readiness probe did not report service states.")
    pending = []
    for name in ("ssh.service", "open-vm-tools.service"):
        state = services.get(name)
        if not isinstance(state, dict) or state.get("LoadState") != "loaded":
            raise GuestError(f"OS readiness check could not find {name}.")
        if state.get("ActiveState") == "failed" or state.get("Result") != "success":
            raise GuestError(f"OS readiness check found a failed {name}; inspect its guest journal.")
        if state.get("ActiveState") != "active" or state.get("SubState") != "running":
            pending.append(name + " is not running")
    if evidence.get("boot_finished") is not True:
        pending.append("cloud-init boot-finished marker is not present")
    return "; ".join(pending) or None


def generate_ssh_key() -> tuple[str, str]:
    key = paramiko.RSAKey.generate(3072)
    out = io.StringIO()
    key.write_private_key(out)
    return out.getvalue(), f"{key.get_name()} {key.get_base64()} gdeploy"


def _safe_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or any(c in value for c in "\r\n\x00"):
        raise GuestError(f"Invalid {name}.")
    return value


def _ipv4(value: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except (ipaddress.AddressValueError, TypeError) as exc:
        raise GuestError("Guest must have a valid IPv4 address.") from exc


def _autoinstall_data(
    spec: dict, username: str, password: str, ssh_public_key: str, authorized_ssh_keys: list[str] | None = None,
) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", username):
        raise GuestError("Ubuntu username must start with a letter and be at most 32 characters.")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", spec["name"]):
        raise GuestError("Invalid Ubuntu hostname.")
    _safe_text(password, "OS password")
    _safe_text(ssh_public_key, "SSH public key")
    # Keep GDeploy's generated automation key first, even when an administrator
    # supplied the same public key with a different comment. Validate extras
    # again because queued records can outlive the settings that produced them.
    try:
        additional_keys = normalize_ssh_public_keys(authorized_ssh_keys if authorized_ssh_keys is not None else [])
    except SSHKeyError:
        raise GuestError("Saved deployment SSH public keys are invalid. Review SSH access in Setup before redeploying.") from None
    automation_identity = " ".join(ssh_public_key.split()[:2])
    ssh_keys = [ssh_public_key] + [key for key in additional_keys if " ".join(key.split()[:2]) != automation_identity]
    network: dict = {"match": {"driver": "vmxnet3"}, "dhcp6": False}
    if spec["ip_mode"] == "dhcp":
        network["dhcp4"] = True
    elif spec["ip_mode"] == "static":
        try:
            address = ipaddress.IPv4Interface(spec["address"])
            gateway = ipaddress.IPv4Address(spec["gateway"])
            dns = [str(ipaddress.IPv4Address(ip)) for ip in spec["dns"]]
        except (ValueError, TypeError, KeyError) as exc:
            raise GuestError("Invalid static network configuration.") from exc
        if gateway not in address.network or not dns:
            raise GuestError("Static networking needs a gateway in the subnet and a DNS server.")
        network.update(
            {
                "dhcp4": False,
                "addresses": [str(address)],
                "routes": [{"to": "default", "via": str(gateway)}],
                "nameservers": {"addresses": dns},
            }
        )
    else:
        raise GuestError("IP mode must be dhcp or static.")
    data = {
        "autoinstall": {
            "version": 1,
            "refresh-installer": {"update": False},
            "locale": "en_US.UTF-8",
            "keyboard": {"layout": "us"},
            "identity": {
                "hostname": spec["name"],
                "username": username,
                "password": sha512_crypt.using(rounds=100000).hash(password),
            },
            "network": {"version": 2, "ethernets": {"gdeploy": network}},
            "storage": {"layout": {"name": "direct"}},
            "ssh": {"install-server": True, "allow-pw": True, "authorized-keys": ssh_keys},
            "packages": ["open-vm-tools", "openssh-server", "python3", "ca-certificates"],
            "updates": "security",
            "late-commands": [
                "curtin in-target --target=/target -- systemctl enable open-vm-tools ssh",
            ],
            "shutdown": "reboot",
        }
    }
    return data


def _patch_grub(text: str) -> str:
    """Modify the live installer entries, preserving Ubuntu's kernel/initrd paths."""
    modified = []
    found = False
    for line in text.splitlines():
        if re.match(r"\s*linux(?:efi)?\s+.*?/casper/", line):
            found = True
            line = re.sub(r"\s+---(?:\s|$)", " ", line).rstrip()
            # The escaped semicolon is interpreted by GRUB, not a shell.
            line += r" autoinstall noprompt ds=nocloud\;s=/cdrom/nocloud/ ---"
        if re.match(r"\s*set\s+timeout=", line):
            line = "set timeout=1"
        if re.match(r"\s*set\s+default=", line):
            line = "set default=0"
        modified.append(line)
    if not found:
        raise GuestError("Ubuntu ISO has no recognized live-server GRUB boot entry.")
    return "set default=0\nset timeout=1\n" + "\n".join(modified) + "\n"


def _update_md5_manifest(original: str, mapped_files: dict[str, Path]) -> str:
    """Maintain Ubuntu's internal media check after adding the NoCloud seed.

    MD5 is used only for Ubuntu's existing media manifest; the source ISO itself
    must pass the SHA-256 check in preflight.
    """
    hashes = {
        path: hashlib.md5(local.read_bytes(), usedforsecurity=False).hexdigest() for path, local in mapped_files.items()
    }
    result = []
    for line in original.splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{32})\s+\*?(.+)", line)
        if match:
            path = "/" + match[2].removeprefix("./").lstrip("/")
            if path in hashes:
                result.append(f"{hashes.pop(path)}  .{path}")
                continue
        result.append(line)
    result.extend(f"{digest}  .{path}" for path, digest in hashes.items())
    return "\n".join(result) + "\n"


def _iso_tool_log(output, secrets):
    """Redact before limiting diagnostics; never expose generated login material."""
    text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output or "")
    for value in sorted((value for value in secrets if value), key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[redacted key]", text, flags=re.S)
    text = re.sub(r"\$6\$[^\s\"']+", "[redacted password hash]", text)
    text = re.sub(r"(?im)^(\s*(?:password|passwd|token|secret|authorized-keys)\s*[:=]).*$", r"\1 [redacted]", text)
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    text = "".join(char for char in text if char in "\n\t" or ord(char) >= 32)
    return [line[:1500] for line in text.splitlines()[-30:] if line.strip()]


def build_seed_iso(
    source_iso: Path, output_iso: Path, spec: dict, username: str, password: str, ssh_public_key: str,
    *, authorized_ssh_keys: list[str] | None = None, log: Callable[[str, str], None] | None = None,
) -> None:
    """Replay the original ISO's boot metadata while adding a NoCloud seed."""
    source_iso, output_iso = Path(source_iso), Path(output_iso)
    if not source_iso.is_file() or source_iso.resolve() == output_iso.resolve():
        raise GuestError("A separate readable Ubuntu 24.04 live-server ISO is required.")
    if output_iso.exists():
        raise GuestError("Refusing to overwrite an existing deployment ISO.")
    data = _autoinstall_data(spec, username, password, ssh_public_key, authorized_ssh_keys)
    installed_keys = data["autoinstall"]["ssh"]["authorized-keys"]
    # Tool errors may print a whole YAML key or only its encoded key material.
    secrets = (
        password, data["autoinstall"]["identity"]["password"], *installed_keys,
        *(key.split()[1] for key in installed_keys if len(key.split()) >= 2),
    )
    step = "Create installation workspace"

    def emit(message, level="info"):
        if log is not None:
            log(message, level)

    def storage_details():
        try:
            usage = shutil.disk_usage(output_iso.parent)
            emit(
                f"Build filesystem {output_iso.parent}: {usage.free / 1024**3:.2f} GiB available, "
                f"{usage.used / 1024**3:.2f} GiB used of {usage.total / 1024**3:.2f} GiB. "
                "ISO workspace uses the data volume, not /tmp."
            )
        except OSError:
            emit("Could not read available space for the build filesystem.", "warning")

    try:
        output_iso.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        emit(f"Source ISO: {source_iso.name} ({source_iso.stat().st_size / 1024**3:.2f} GiB).")
        storage_details()
        with tempfile.TemporaryDirectory(prefix="gdeploy-iso-", dir=output_iso.parent) as folder:
            root = Path(folder)
            # xorriso and any subprocess scratch files share the disk-backed
            # workspace, even when Docker deliberately limits /tmp to tmpfs.
            environment = dict(os.environ, TMPDIR=str(root), TMP=str(root), TEMP=str(root))

            def run(command, timeout):
                emit(step)
                try:
                    result = subprocess.run(command, check=True, capture_output=True, timeout=timeout, env=environment)
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                    for line in _iso_tool_log(error.stderr, secrets):
                        emit(f"xorriso: {line}", "error")
                    raise
                for line in _iso_tool_log(result.stderr, secrets):
                    emit(f"xorriso: {line}")
                emit(f"{step}: complete (xorriso exit 0).")

            files = {"/boot/grub/grub.cfg": root / "grub.cfg", "/boot/grub/loopback.cfg": root / "loopback.cfg"}
            for iso_path, local in files.items():
                step = f"Extract {iso_path}"
                run(
                    ["xorriso", "-osirrox", "on", "-indev", str(source_iso), "-extract", iso_path, str(local)],
                    120,
                )
                step = f"Update {iso_path} for unattended installation"
                # ISO files commonly extract with mode 0444. Only these private
                # working copies become writable; never change the source ISO.
                local.chmod(0o644)
                local.write_text(_patch_grub(local.read_text()))
                emit(f"{step}: complete.")
            step = "Write unattended installation settings"
            user_data, meta_data = root / "user-data", root / "meta-data"
            user_data.write_text("#cloud-config\n" + yaml.safe_dump(data, sort_keys=False))
            user_data.chmod(0o600)
            meta_data.write_text(
                yaml.safe_dump({"instance-id": f"gdeploy-{secret_tools.token_hex(16)}", "local-hostname": spec["name"]})
            )
            files.update({"/nocloud/user-data": user_data, "/nocloud/meta-data": meta_data})
            manifest = root / "md5sum.txt"
            step = "Extract /md5sum.txt"
            run(
                ["xorriso", "-osirrox", "on", "-indev", str(source_iso), "-extract", "/md5sum.txt", str(manifest)],
                120,
            )
            step = "Update installation media checksum manifest"
            manifest.chmod(0o644)
            manifest.write_text(_update_md5_manifest(manifest.read_text(), files))
            files["/md5sum.txt"] = manifest
            command = ["xorriso", "-indev", str(source_iso), "-outdev", str(output_iso), "-boot_image", "any", "replay"]
            for iso_path, local in files.items():
                command.extend(["-map", str(local), iso_path])
            command.extend(["-commit", "-end"])
            step = "Write bootable unattended installation ISO"
            run(command, 1800)
            output_iso.chmod(0o600)
            emit(f"Installation ISO prepared: {output_iso.stat().st_size / 1024**3:.2f} GiB.")
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


def _elasticsearch_config(ip: str) -> dict:
    return {
        "cluster.name": "gdeploy",
        "node.name": "elasticsearch",
        "path.data": "/var/lib/elasticsearch",
        "path.logs": "/var/log/elasticsearch",
        "network.host": _ipv4(ip),
        "http.port": 9200,
        "discovery.type": "single-node",
        "xpack.security.enabled": True,
        "xpack.security.enrollment.enabled": False,
        "xpack.security.http.ssl.enabled": True,
        "xpack.security.http.ssl.key": "certs/http.key",
        "xpack.security.http.ssl.certificate": "certs/http.crt",
        "xpack.security.http.ssl.certificate_authorities": ["certs/ca.crt"],
        "xpack.security.transport.ssl.enabled": True,
        "xpack.security.transport.ssl.verification_mode": "certificate",
        "xpack.security.transport.ssl.key": "certs/http.key",
        "xpack.security.transport.ssl.certificate": "certs/http.crt",
        "xpack.security.transport.ssl.certificate_authorities": ["certs/ca.crt"],
    }


def _kibana_config(ip: str, elastic: dict, secrets: dict) -> dict:
    for key in ("kibana_encryption_key", "kibana_security_key", "kibana_reporting_key"):
        if len(_safe_text(secrets[key], key)) < 32:
            raise GuestError("Kibana encryption keys must have at least 32 characters.")
    return {
        "server.host": "0.0.0.0",
        "server.port": 5601,
        "server.name": "kibana",
        "server.publicBaseUrl": f"https://{_ipv4(ip)}:5601",
        "server.ssl.enabled": True,
        "server.ssl.certificate": "/etc/kibana/certs/server.crt",
        "server.ssl.key": "/etc/kibana/certs/server.key",
        "elasticsearch.hosts": [f"https://{_ipv4(elastic['ip'])}:9200"],
        "elasticsearch.ssl.certificateAuthorities": ["/etc/kibana/certs/elasticsearch-ca.crt"],
        "elasticsearch.ssl.verificationMode": "full",
        "elasticsearch.serviceAccountToken": _safe_text(elastic["service_token"], "Kibana service token"),
        "xpack.encryptedSavedObjects.encryptionKey": secrets["kibana_encryption_key"],
        "xpack.security.encryptionKey": secrets["kibana_security_key"],
        "xpack.reporting.encryptionKey": secrets["kibana_reporting_key"],
    }


# Every script receives a private JSON payload. Package managers can be verbose;
# their output is retained only in bounded memory and never streamed to UI logs.
_SCRIPT_HEADER = r"""#!/bin/bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "$0")"
export DEBIAN_FRONTEND=noninteractive
trap 'echo "GDeploy provisioning failed at script line $LINENO" >&2' ERR
"""

_ELASTIC_REPO = r"""
apt-get -q update
apt-get -q install -y ca-certificates curl gnupg openssl python3
curl --fail --silent --show-error --retry 3 --proto '=https' --tlsv1.2 https://artifacts.elastic.co/GPG-KEY-elasticsearch -o elastic.asc
fingerprint=$(gpg --batch --show-keys --with-colons elastic.asc | awk -F: '$1=="fpr" { print $10; exit }')
test "$fingerprint" = "46095ACC8548582C1A2699A9D27D666CD88E42B4"
gpg --batch --yes --dearmor --output /usr/share/keyrings/elasticsearch-keyring.gpg elastic.asc
chmod 644 /usr/share/keyrings/elasticsearch-keyring.gpg
echo 'deb [signed-by=/usr/share/keyrings/elasticsearch-keyring.gpg] https://artifacts.elastic.co/packages/9.x/apt stable main' > /etc/apt/sources.list.d/elastic-9.x.list
chmod 644 /etc/apt/sources.list.d/elastic-9.x.list
apt-get -q update
"""

_ELASTICSEARCH_SCRIPT = (
    _SCRIPT_HEADER
    + _ELASTIC_REPO
    + r"""
apt-get -q install -y elasticsearch > elastic-package.log 2>&1 || { echo 'Elasticsearch package installation failed; verify guest disk space and package repository connectivity.' >&2; exit 1; }
apt-mark hold elasticsearch
install -d -o root -g elasticsearch -m 750 /etc/elasticsearch/certs
python3 - <<'PY'
import json, os, pathlib, subprocess
p = json.loads(pathlib.Path('payload.json').read_text())
cert = pathlib.Path('/etc/elasticsearch/certs')
def run(*args, **kwargs):
    return subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
run('openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072','-out',str(cert/'ca.key'))
run('openssl','req','-x509','-new','-sha256','-days','3650','-key',str(cert/'ca.key'),'-out',str(cert/'ca.crt'),'-subj','/CN=GDeploy Elasticsearch CA','-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign')
run('openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072','-out',str(cert/'http.key'))
run('openssl','req','-new','-key',str(cert/'http.key'),'-out',str(cert/'http.csr'),'-subj','/CN=GDeploy Elasticsearch')
(cert/'http.ext').write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth,clientAuth\nsubjectAltName=IP:'+p['ip']+',IP:127.0.0.1,DNS:localhost\n')
run('openssl','x509','-req','-in',str(cert/'http.csr'),'-CA',str(cert/'ca.crt'),'-CAkey',str(cert/'ca.key'),'-CAcreateserial','-days','825','-sha256','-extfile',str(cert/'http.ext'),'-out',str(cert/'http.crt'))
pathlib.Path('/etc/elasticsearch/elasticsearch.yml').write_text(json.dumps(p['config'], indent=2))
keystore = '/usr/share/elasticsearch/bin/elasticsearch-keystore'
entries = subprocess.check_output([keystore, 'list'], text=True).splitlines()
for entry in entries:
    if entry == 'bootstrap.password' or entry.startswith(('xpack.security.http.ssl.', 'xpack.security.transport.ssl.')):
        run(keystore, 'remove', entry)
PY
chown root:elasticsearch /etc/elasticsearch/elasticsearch.yml /etc/elasticsearch/certs/http.key /etc/elasticsearch/certs/http.crt /etc/elasticsearch/certs/ca.crt
chmod 640 /etc/elasticsearch/elasticsearch.yml /etc/elasticsearch/certs/http.key /etc/elasticsearch/certs/http.crt /etc/elasticsearch/certs/ca.crt
chmod 600 /etc/elasticsearch/certs/ca.key
echo 'vm.max_map_count=1048576' > /etc/sysctl.d/99-gdeploy-elasticsearch.conf
sysctl -p /etc/sysctl.d/99-gdeploy-elasticsearch.conf
systemctl daemon-reload
systemctl enable --now elasticsearch
python3 - <<'PY'
import base64, json, pathlib, ssl, subprocess, time, urllib.error, urllib.request, uuid
p = json.loads(pathlib.Path('payload.json').read_text())
context = ssl.create_default_context(cafile='/etc/elasticsearch/certs/ca.crt')
base_url = 'https://'+p['ip']+':9200'
deadline = time.monotonic()+600
while True:
    try:
        with urllib.request.urlopen(base_url, context=context, timeout=20):
            break
    except urllib.error.HTTPError as error:
        if error.code == 401:
            break
    except Exception:
        pass
    if time.monotonic() >= deadline:
        raise SystemExit('Elasticsearch HTTPS listener did not start within 10 minutes.')
    time.sleep(5)
password = p['secrets']['elastic_password']
subprocess.run(['/usr/share/elasticsearch/bin/elasticsearch-reset-password','-u','elastic','-i','-b','-s','--url',base_url], input=(password+'\n'+password+'\n').encode(), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
auth = base64.b64encode(('elastic:'+p['secrets']['elastic_password']).encode()).decode()
def request(path, data=None, method=None):
    req = urllib.request.Request('https://'+p['ip']+':9200'+path, data=None if data is None else json.dumps(data).encode(), method=method, headers={'Authorization':'Basic '+auth,'Content-Type':'application/json'})
    with urllib.request.urlopen(req, context=context, timeout=75) as response:
        return json.load(response)
deadline = time.monotonic()+600
while True:
    try:
        health = request('/_cluster/health?wait_for_status=yellow&timeout=60s')
        if health.get('status') in ('green','yellow') and not health.get('timed_out'):
            break
    except Exception:
        pass
    if time.monotonic() >= deadline:
        raise SystemExit('Elasticsearch did not pass its authenticated TLS health check within 10 minutes.')
    time.sleep(5)
token = request('/_security/service/elastic/kibana/credential/token/gdeploy-'+uuid.uuid4().hex, None, 'POST')['token']['value']
version = subprocess.check_output(['dpkg-query','-W','-f=${Version}','elasticsearch'], text=True).strip()
if not version.startswith('9.'):
    raise SystemExit('Unexpected Elasticsearch package version.')
print('GDEPLOY_RESULT='+json.dumps({'ip':p['ip'],'ca_pem':pathlib.Path('/etc/elasticsearch/certs/ca.crt').read_text(),'service_token':token,'version':version}))
PY
"""
)

_KIBANA_SCRIPT = (
    _SCRIPT_HEADER
    + _ELASTIC_REPO
    + r"""
python3 - <<'PY'
import json, pathlib, subprocess
p = json.loads(pathlib.Path('payload.json').read_text())
subprocess.run(['apt-get','-q','install','-y','--','kibana='+p['elastic']['version']], check=True)
PY
apt-mark hold kibana
install -d -o root -g kibana -m 750 /etc/kibana/certs
python3 - <<'PY'
import json, pathlib, subprocess
p = json.loads(pathlib.Path('payload.json').read_text())
cert = pathlib.Path('/etc/kibana/certs')
(cert/'elasticsearch-ca.crt').write_text(p['elastic']['ca_pem'])
subprocess.run(['openssl','req','-x509','-newkey','rsa:3072','-nodes','-sha256','-days','825','-keyout',str(cert/'server.key'),'-out',str(cert/'server.crt'),'-subj','/CN=GDeploy Kibana','-addext','basicConstraints=critical,CA:FALSE','-addext','subjectAltName=IP:'+p['ip']], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
pathlib.Path('/etc/kibana/kibana.yml').write_text(json.dumps(p['config'], indent=2))
PY
chown root:kibana /etc/kibana/kibana.yml /etc/kibana/certs/*
chmod 640 /etc/kibana/kibana.yml /etc/kibana/certs/*
systemctl daemon-reload
systemctl enable --now kibana
python3 - <<'PY'
import base64, json, pathlib, ssl, time, urllib.request
p = json.loads(pathlib.Path('payload.json').read_text())
context = ssl.create_default_context(cafile='/etc/kibana/certs/server.crt')
auth = base64.b64encode(('elastic:'+p['secrets']['elastic_password']).encode()).decode()
deadline = time.monotonic()+600
while True:
    try:
        request = urllib.request.Request('https://'+p['ip']+':5601/api/status', headers={'Authorization':'Basic '+auth})
        with urllib.request.urlopen(request, context=context, timeout=20) as response:
            if json.load(response).get('status',{}).get('overall',{}).get('level') == 'available':
                break
    except Exception:
        pass
    if time.monotonic() >= deadline:
        raise SystemExit('Kibana did not become available over authenticated HTTPS within 10 minutes; check its connection to Elasticsearch.')
    time.sleep(5)
print('GDEPLOY_RESULT={}')
PY
"""
)

_SPLUNK_SCRIPT = (
    _SCRIPT_HEADER
    + r"""
python3 - <<'PY'
import hashlib, hmac, json, pathlib
p = json.loads(pathlib.Path('payload.json').read_text())
with pathlib.Path('splunk.tgz').open('rb') as package:
    actual = hashlib.file_digest(package, 'sha256').hexdigest()
if not hmac.compare_digest(actual, p['package_sha256']):
    raise SystemExit('Splunk package SHA-256 changed during transfer; no package was installed. Review Software packages in Setup.')
PY
apt-get -q update
apt-get -q install -y ca-certificates openssl python3 libnuma1
test ! -e /opt/splunk
id -u splunk >/dev/null 2>&1 || useradd --system --home-dir /opt/splunk --shell /usr/sbin/nologin splunk
tar --extract --gzip --file splunk.tgz --directory /opt --no-same-owner
install -d -m 700 /opt/splunk/etc/auth/gdeploy
install -d -m 750 /opt/splunk/etc/system/local
python3 - <<'PY'
import json, pathlib, subprocess
p = json.loads(pathlib.Path('payload.json').read_text())
cert = pathlib.Path('/opt/splunk/etc/auth/gdeploy')
subprocess.run(['openssl','req','-x509','-newkey','rsa:3072','-nodes','-sha256','-days','825','-keyout',str(cert/'server.key'),'-out',str(cert/'server.crt'),'-subj','/CN=GDeploy Splunk','-addext','basicConstraints=critical,CA:FALSE','-addext','subjectAltName=IP:'+p['ip']], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
(cert/'server.pem').write_text((cert/'server.key').read_text()+(cert/'server.crt').read_text())
local = pathlib.Path('/opt/splunk/etc/system/local')
(local/'user-seed.conf').write_text('[user_info]\nUSERNAME = admin\nPASSWORD = '+p['secrets']['splunk_password']+'\n')
(local/'web.conf').write_text('[settings]\nenableSplunkWebSSL = true\nhttpport = 8000\nprivKeyPath = /opt/splunk/etc/auth/gdeploy/server.key\nserverCert = /opt/splunk/etc/auth/gdeploy/server.crt\n')
(local/'server.conf').write_text('[sslConfig]\nenableSplunkdSSL = true\nserverCert = /opt/splunk/etc/auth/gdeploy/server.pem\nsslPassword =\nsslRootCAPath = /opt/splunk/etc/auth/gdeploy/server.crt\n')
PY
chmod 600 /opt/splunk/etc/system/local/user-seed.conf /opt/splunk/etc/auth/gdeploy/*
chown -R splunk:splunk /opt/splunk
runuser -u splunk -- /opt/splunk/bin/splunk start --accept-license --answer-yes --no-prompt
runuser -u splunk -- /opt/splunk/bin/splunk stop
/opt/splunk/bin/splunk enable boot-start -user splunk -systemd-managed 1 --accept-license --answer-yes --no-prompt
systemctl daemon-reload
systemctl enable --now Splunkd
python3 - <<'PY'
import base64, json, pathlib, ssl, time, urllib.request
p = json.loads(pathlib.Path('payload.json').read_text())
context = ssl.create_default_context(cafile='/opt/splunk/etc/auth/gdeploy/server.crt')
auth = base64.b64encode(('admin:'+p['secrets']['splunk_password']).encode()).decode()
deadline = time.monotonic()+600
while True:
    try:
        req = urllib.request.Request('https://'+p['ip']+':8089/services/server/info?output_mode=json', headers={'Authorization':'Basic '+auth})
        with urllib.request.urlopen(req, context=context, timeout=20) as response:
            info = json.load(response)
            if not info.get('entry'):
                raise ValueError('No Splunk server information.')
        with urllib.request.urlopen('https://'+p['ip']+':8000/en-US/account/login', context=context, timeout=20) as response:
            if response.status == 200:
                break
    except Exception:
        pass
    if time.monotonic() >= deadline:
        raise SystemExit('Splunk did not pass its authenticated management and HTTPS web health checks within 10 minutes.')
    time.sleep(5)
pathlib.Path('/opt/splunk/etc/system/local/user-seed.conf').unlink(missing_ok=True)
print('GDEPLOY_RESULT={}')
PY
"""
)


def _validate_splunk_archive(package: Path, *, fileobj=None) -> None:
    """Reject unsafe tar paths and non-x86_64 packages before upload/extraction."""
    try:
        seen = set()
        directories = set()
        links: dict[str, str] = {}
        machine = None
        launcher = False
        with tarfile.open(name=package if fileobj is None else None, mode="r:gz", fileobj=fileobj) as archive:
            for member in archive:
                name = member.name.removeprefix("./")
                normalized = posixpath.normpath(name)
                if name.startswith("/") or normalized.split("/")[0] != "splunk" or ".." in name.split("/"):
                    raise GuestError("Splunk archive contains an unsafe file path.")
                if normalized in seen and not (member.isdir() and normalized in directories):
                    raise GuestError("Splunk archive contains duplicate file paths.")
                if member.isdir():
                    directories.add(normalized)
                if member.isdev() or member.isfifo():
                    raise GuestError("Splunk archive contains an unsupported special file.")
                if member.issym() or member.islnk():
                    target = member.linkname
                    resolved = posixpath.normpath(
                        posixpath.join(posixpath.dirname(name), target) if member.issym() else target
                    )
                    if target.startswith("/") or resolved.split("/")[0] != "splunk":
                        raise GuestError("Splunk archive contains a link outside its installation directory.")
                    links[normalized] = resolved
                seen.add(normalized)
                if normalized == "splunk/bin/splunk":
                    launcher = member.isfile() and member.size > 0
                if normalized == "splunk/bin/splunkd" and member.isfile():
                    reader = archive.extractfile(member)
                    if reader is not None:
                        header = reader.read(20)
                        if len(header) == 20 and header[:6] == b"\x7fELF\x02\x01":
                            machine = int.from_bytes(header[18:20], "little")
        for name in seen:
            ancestors = ["/".join(name.split("/")[:index]) for index in range(1, len(name.split("/")))]
            if any(parent in links for parent in ancestors):
                raise GuestError("Splunk archive tries to write through a linked directory.")
        if not launcher or machine != 62:
            raise GuestError("Supply a Splunk Enterprise Linux x86_64 .tgz package.")
    except (OSError, tarfile.TarError) as exc:
        raise GuestError("Could not read the Splunk .tgz package.") from exc


class _PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, session: GuestSession):
        self.session = session

    def missing_host_key(self, client, hostname, key):
        observed = f"{key.get_name()} {key.get_base64()}"
        if self.session.host_key and not secret_tools.compare_digest(self.session.host_key, observed):
            raise GuestHostKeyError("SSH host key changed; refusing to connect to a different guest.")
        self.session.host_key = observed


class GuestSession:
    def __init__(self, ip: str, username: str, private_key: str, password: str, known_host_key: str | None = None):
        self.ip = _ipv4(ip)
        self.username = _safe_text(username, "SSH username")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", self.username):
            raise GuestError("Invalid SSH account name.")
        self.password = _safe_text(password, "OS password")
        self.private_key = private_key
        self.host_key = known_host_key or ""
        self.client: paramiko.SSHClient | None = None
        self._secrets = {password, private_key}
        self._check_cancelled: Callable[[], None] = lambda: None
        self._wait_callback: Callable[[float], None] | None = None

    def set_cancellation_hooks(
        self, check_cancelled: Callable[[], None], wait: Callable[[float], None] | None = None,
    ) -> None:
        """Check for a requested stop only at safe provisioning boundaries."""
        self._check_cancelled = check_cancelled
        self._wait_callback = wait

    def _wait(self, seconds: float) -> None:
        self._check_cancelled()
        if self._wait_callback is not None:
            self._wait_callback(seconds)
        else:
            # Remain responsive without requiring callers to supply an Event.
            remaining = seconds
            while remaining > 0:
                delay = min(0.25, remaining)
                time.sleep(delay)
                remaining -= delay
                self._check_cancelled()
        self._check_cancelled()

    def __enter__(self) -> GuestSession:
        self._connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.client is not None:
            self.client.close()
            self.client = None

    def _connect(self) -> None:
        self._check_cancelled()
        if self.client is not None and self.client.get_transport() and self.client.get_transport().is_active():
            return
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(_PinnedHostKeyPolicy(self))
        try:
            key = paramiko.RSAKey.from_private_key(io.StringIO(self.private_key))
        except (paramiko.SSHException, ValueError) as exc:
            client.close()
            raise GuestError("The stored SSH private key is invalid.") from exc
        try:
            client.connect(
                self.ip,
                port=22,
                username=self.username,
                pkey=key,
                timeout=10,
                auth_timeout=10,
                banner_timeout=10,
                allow_agent=False,
                look_for_keys=False,
            )
            client.get_transport().set_keepalive(30)
            self.client = client
        except GuestError:
            client.close()
            raise
        except (OSError, paramiko.SSHException, ValueError) as exc:
            client.close()
            raise GuestConnectionError("SSH connection failed; the guest may still be installing or booting.") from exc

    def _sanitize(self, text: str) -> str:
        text = re.sub(r"(?m)^GDEPLOY_RESULT=.*$", "[service credentials omitted]", text)
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text

    def _exec(self, command: str, *, timeout: int = 1800, sudo: bool = False, cancellable: bool = True) -> str:
        options = {} if cancellable else {"cancellable": False}
        result = self._exec_result(command, timeout=timeout, sudo=sudo, combine_stderr=True, **options)
        if result.exit_code != 0:
            safe = self._sanitize(result.stdout).strip()[-3000:]
            raise GuestError(f"Guest command failed (exit {result.exit_code}). {safe}")
        return result.stdout

    def _exec_result(
        self, command: str, *, timeout: int = 1800, sudo: bool = False, combine_stderr: bool = False,
        cancellable: bool = True,
    ) -> CommandResult:
        """Drain both SSH streams with bounded memory and an absolute deadline."""
        if cancellable:
            self._check_cancelled()
        if self.client is None:
            raise GuestError("SSH session is not connected.")
        if sudo:
            command = "sudo -k -S -p '' -- /bin/bash -c " + shlex.quote(command)
        channel = None
        watchdog = None
        expired = threading.Event()
        deadline = time.monotonic() + timeout
        timeout_error = "Guest command exceeded its time limit; check the guest console and service logs."

        def expire() -> None:
            expired.set()
            # This session owns its transport. Closing it also releases rekey
            # and socket-write waits which a channel-only close cannot stop.
            transport.close()
            channel.close()

        try:
            transport = self.client.get_transport()
            if transport is None:
                raise GuestConnectionError("SSH transport is no longer connected.")
            channel = transport.open_session(timeout=min(15, max(0.1, deadline - time.monotonic())))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GuestError(timeout_error)
            # Paramiko's exec acknowledgement wait ignores Channel.settimeout.
            # Closing at the deadline also interrupts stalled exec/send calls.
            channel.settimeout(remaining)
            watchdog = threading.Timer(remaining, expire)
            watchdog.daemon = True
            watchdog.start()
            channel.set_combine_stderr(combine_stderr)
            if cancellable:
                self._check_cancelled()
            channel.exec_command(command)
            if sudo:
                channel.sendall((self.password + "\n").encode())
            channel.shutdown_write()
            stdout, stderr = bytearray(), bytearray()
            while True:
                if expired.is_set() or time.monotonic() >= deadline:
                    raise GuestError(timeout_error)
                if channel.recv_ready():
                    stdout.extend(channel.recv(32768))
                    del stdout[:-131072]
                if not combine_stderr and channel.recv_stderr_ready():
                    stderr.extend(channel.recv_stderr(32768))
                    del stderr[:-131072]
                if (channel.exit_status_ready() and not channel.recv_ready()
                        and (combine_stderr or not channel.recv_stderr_ready())):
                    status = channel.recv_exit_status()
                    if status == -1:
                        raise GuestConnectionError("SSH connection closed before the guest returned an exit status.")
                    return CommandResult(stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"), status)
                if not channel.recv_ready() and (combine_stderr or not channel.recv_stderr_ready()):
                    time.sleep(0.1)
        except (OSError, EOFError, paramiko.SSHException) as exc:
            if expired.is_set() or time.monotonic() >= deadline:
                raise GuestError(timeout_error) from exc
            raise GuestConnectionError("SSH connection was lost during guest provisioning.") from exc
        finally:
            try:
                if channel is not None:
                    channel.close()
            except (OSError, EOFError, paramiko.SSHException):
                # Preserve the result/error already obtained from the command.
                pass
            finally:
                if watchdog is not None:
                    watchdog.cancel()

    def wait_ready(
        self, timeout: int = 1800, *, log: Callable[[str, str], None] = lambda message, level: None,
        check_cancelled: Callable[[], None] | None = None, wait: Callable[[float], None] | None = None,
    ) -> None:
        """Verify cloud-init and installed-system evidence within one boot deadline."""
        if check_cancelled is not None:
            self.set_cancellation_hooks(check_cancelled, wait)
        elif wait is not None:
            self._wait_callback = wait
        self._check_cancelled()
        deadline = time.monotonic() + timeout

        def emit(message: str, level: str = "info") -> None:
            for line in _iso_tool_log(self._sanitize(message), self._secrets):
                log(line, level)

        def emit_stream(label: str, contents: str, level: str = "warning") -> None:
            # Redact the original lines before prefixing: anchored credential
            # filters must still see fields such as "password: ..." at column 0.
            for line in _iso_tool_log(self._sanitize(contents), self._secrets):
                log(f"{label}: {line}", level)

        while self.client is None:
            self._check_cancelled()
            try:
                self._connect()
            except GuestConnectionError:
                self._check_cancelled()
                if time.monotonic() + 5 >= deadline:
                    raise
                self._wait(5)
        previous = None
        previous_evidence = None
        last_pending = "cloud-init has not reported completion"
        while time.monotonic() < deadline:
            self._check_cancelled()
            # --wait can emit progress on stdout. Poll clean JSON instead, and
            # query as root because current cloud-init reads protected config.
            result = self._exec_result(
                "cloud-init status --format json", timeout=min(60, max(1, int(deadline - time.monotonic()))), sudo=True,
            )
            self._check_cancelled()
            observation = (result.stdout, result.stderr, result.exit_code)
            changed = observation != previous
            if changed:
                emit(f"Cloud-init status command exited {result.exit_code}.")
                if result.stderr.strip():
                    emit_stream("Cloud-init stderr", result.stderr)
                previous = observation
            try:
                status = json.loads(result.stdout)
            except json.JSONDecodeError:
                if result.exit_code != 0:
                    raise GuestError(f"Cloud-init status command failed (exit {result.exit_code}); expand deployment logs and check guest sudo access and cloud-init availability.") from None
                emit(f"Cloud-init stdout was not valid JSON ({len(result.stdout)} characters); raw output omitted.", "error")
                raise GuestError("Cloud-init returned invalid JSON; expand deployment logs for the command exit and diagnostics.") from None
            if not isinstance(status, dict) or not isinstance(status.get("errors"), list):
                raise GuestError("Cloud-init returned an unsupported status response; inspect its status on the guest.")
            state = status.get("status")
            boot = status.get("boot_status_code")
            if not isinstance(state, str):
                raise GuestError("Cloud-init returned an unsupported status response; inspect its status on the guest.")
            if changed:
                emit(f"Cloud-init status={state}; extended_status={status.get('extended_status')}; boot_status_code={boot}.")
            # A successful exit alone is insufficient: inspect every stage too.
            stages = {"overall": status, **{name: status.get(name, {}) for name in ("init-local", "init", "modules-config", "modules-final")}}
            has_errors = False
            for name, stage in stages.items():
                if not isinstance(stage, dict):
                    raise GuestError("Cloud-init returned an unsupported stage response; inspect its status on the guest.")
                if stage.get("errors") or stage.get("recoverable_errors"):
                    has_errors = True
                    # Report where errors occurred without dumping user-data
                    # or arbitrary configuration embedded in exception strings.
                    emit(f"Cloud-init {name}: errors_present={bool(stage.get('errors'))}; "
                         f"recoverable_errors_present={bool(stage.get('recoverable_errors'))}.", "error")
            if has_errors:
                raise GuestError("Cloud-init reported errors or recoverable errors; inspect sudo cloud-init status --long and the guest journal.")
            if result.exit_code != 0:
                raise GuestError(f"Cloud-init status command failed (exit {result.exit_code}); expand deployment logs for diagnostics.")
            if state in {"done", "disabled"}:
                if state == "done" and (status.get("extended_status") not in (None, "done") or status.get("stage") is not None):
                    raise GuestError("Cloud-init returned a conflicting completion state; inspect sudo cloud-init status --long.")
                if state == "disabled" and (
                    boot != "disabled-by-marker-file" or status.get("extended_status") != "disabled"
                    or status.get("stage") is not None
                ):
                    raise GuestError("Cloud-init is disabled for an unverified reason; inspect sudo cloud-init status --long.")
                if time.monotonic() >= deadline:
                    break
                self._check_cancelled()
                probe = self._exec_result(
                    "python3 -c " + shlex.quote(_READINESS_PROBE),
                    timeout=min(60, max(1, int(deadline - time.monotonic()))), sudo=True,
                )
                self._check_cancelled()
                if probe.stderr.strip():
                    emit_stream("OS readiness probe stderr", probe.stderr)
                if probe.exit_code != 0:
                    raise GuestError(f"OS readiness probe failed (exit {probe.exit_code}); check sudo access and expand deployment logs.")
                try:
                    evidence = json.loads(probe.stdout)
                except json.JSONDecodeError:
                    raise GuestError("OS readiness probe returned invalid JSON; check guest python3 and sudo access.") from None
                if not isinstance(evidence, dict):
                    raise GuestError("OS readiness probe returned an unsupported response.")
                if evidence != previous_evidence:
                    emit(
                        f"OS readiness: root={evidence.get('root_source')} ({evidence.get('root_fstype')}); "
                        f"sudo={evidence.get('sudo')}; boot_finished={evidence.get('boot_finished')}; "
                        f"disabled_marker={evidence.get('disabled_marker')}; installer_log={evidence.get('installer_log')}."
                    )
                    services = evidence.get("services")
                    for name, state_info in (services.items() if isinstance(services, dict) else []):
                        if name in {"ssh.service", "open-vm-tools.service"} and isinstance(state_info, dict):
                            emit(f"OS readiness {name}: load={state_info.get('LoadState')}; active={state_info.get('ActiveState')}; "
                                 f"substate={state_info.get('SubState')}; result={state_info.get('Result')}.")
                    previous_evidence = evidence
                if time.monotonic() >= deadline:
                    break
                pending = _installed_system_pending(evidence, disabled=state == "disabled")
                if pending is None:
                    reason = "Ubuntu installer disabled cloud-init after completion" if state == "disabled" else "cloud-init completed"
                    emit(f"OS readiness verified: {reason}; installed disk, sudo, SSH and VMware Tools checks passed.")
                    return
            elif state in {"running", "not started"}:
                pending = "cloud-init is " + state
            else:
                raise GuestError("Cloud-init did not report a supported successful or pending state; inspect its guest status.")
            if pending != last_pending:
                emit("Waiting for OS readiness: " + pending + ".")
                last_pending = pending
            self._wait(min(5, max(0, deadline - time.monotonic())))
        self._check_cancelled()
        raise GuestError(f"Timed out verifying OS readiness: {last_pending}. Expand deployment logs and check the guest console.")

    def _run_script(self, script: str, payload: dict, package: Path | None = None) -> dict:
        self._check_cancelled()
        if self.client is None:
            raise GuestError("SSH session is not connected.")
        remote = f"/home/{self.username}/.gdeploy-{secret_tools.token_hex(16)}"
        transferred = False
        try:
            with self.client.open_sftp() as sftp:
                self._check_cancelled()
                sftp.mkdir(remote, 0o700)
                for filename, contents in (("install.sh", script), ("payload.json", json.dumps(payload))):
                    self._check_cancelled()
                    with sftp.file(f"{remote}/{filename}", "w") as stream:
                        stream.write(contents)
                    sftp.chmod(f"{remote}/{filename}", 0o600)
                if package is not None:
                    self._check_cancelled()
                    sftp.put(str(package), f"{remote}/splunk.tgz", callback=lambda sent, total: self._check_cancelled())
                    sftp.chmod(f"{remote}/splunk.tgz", 0o600)
            quoted = shlex.quote(remote)
            # Scripts and their secret payload become root-owned before execution.
            # A trap removes all staging files on either success or failure.
            wrapper = (
                f"set -e; trap 'rm -rf -- {quoted}' EXIT; chown -R root:root -- {quoted}; /bin/bash {quoted}/install.sh"
            )
            self._check_cancelled()
            transferred = True
            # Once started, let this operation finish and return its credentials.
            # The caller persists the result before acknowledging a later stop.
            output = self._exec(wrapper, timeout=2400, sudo=True, cancellable=False)
            results = [
                line.removeprefix("GDEPLOY_RESULT=")
                for line in output.splitlines()
                if line.startswith("GDEPLOY_RESULT=")
            ]
            if len(results) != 1:
                raise GuestError("Application installation did not report a verified result.")
            return json.loads(results[0])
        except (OSError, paramiko.SSHException, json.JSONDecodeError) as exc:
            raise GuestError("Could not transfer or verify the guest installation files.") from exc
        finally:
            if not transferred:
                # Partial SFTP uploads can otherwise leave a plaintext seed behind.
                try:
                    self._exec(f"rm -rf -- {shlex.quote(remote)}", timeout=30, cancellable=False)
                except GuestError:
                    pass

    def install(
        self,
        role: str,
        secrets: dict,
        elastic: dict | None = None,
        splunk_package: Path | None = None,
        splunk_sha256: str | None = None,
        log: Callable[[str], None] = lambda message: None,
        fleetmanager: dict | None = None,
    ) -> dict:
        self._check_cancelled()
        self._secrets.update(value for value in secrets.values() if isinstance(value, str))
        payload: dict = {"ip": self.ip, "secrets": secrets}
        if role == "ubuntu":
            log("Ubuntu installation verified; no application selected.")
            return {"services": []}
        if role == "fleetmanager":
            from .fleet_guest import FleetInstallError, installer_script, requested_online_version, secret_values

            if isinstance(fleetmanager, dict) and fleetmanager.get("mode") == "offline":
                raise GuestError("OFFLINE Fleet Manager installation is no longer supported. Configure repository access in Setup and create a new deployment.")
            if not isinstance(fleetmanager, dict) or fleetmanager.get("mode") != "online":
                raise GuestError("Configure Fleet Manager installation settings in Setup before deploying.")
            for key in ("community_string", "license_pem", "license_sha256"):
                if not isinstance(fleetmanager.get(key), str) or not fleetmanager[key]:
                    raise GuestError("Fleet Manager requires a verified license PEM and community string from Setup.")
            if not re.fullmatch(r"[0-9a-f]{64}", fleetmanager["license_sha256"]):
                raise GuestError("Fleet Manager requires a verified license PEM SHA-256 from Setup.")
            if not isinstance(fleetmanager.get("repository_token"), str) or not fleetmanager["repository_token"]:
                raise GuestError("ONLINE Fleet Manager requires a customer repository token in Setup.")
            self._secrets.update(secret_values(fleetmanager))
            payload["fleetmanager"] = {key: fleetmanager.get(key) for key in (
                "mode", "community_string", "repository_token", "license_pem", "license_name", "license_sha256",
            )}
            try:
                payload["fleetmanager"]["online_version"] = requested_online_version(fleetmanager)
            except FleetInstallError as error:
                raise GuestError(str(error)) from None
            log("Installing Fleet Manager from its repository, applying its product identity license, allowing TCP 443/1443 through UFW, and verifying its services.")
            result = self._run_script(installer_script(), payload)
            if (
                not isinstance(result, dict) or result.get("username") != "admin"
                or not isinstance(result.get("password"), str) or not result["password"]
                or result.get("password_change_required") is not True or not result.get("version")
            ):
                raise GuestError("Fleet Manager did not return its verified initial administrator credentials.")
            self._secrets.add(result["password"])
            return {"services": [{
                "name": "Fleet Manager", "url": f"https://{self.ip}", "username": "admin", "password": result["password"],
                "password_change_required": True, "version": result["version"],
                "community_string": fleetmanager["community_string"],
            }]}
        if role == "elasticsearch":
            _safe_text(secrets["elastic_password"], "Elasticsearch password")
            payload["config"] = _elasticsearch_config(self.ip)
            log("Installing Elasticsearch 9.x with TLS and authenticated health checks.")
            result = self._run_script(_ELASTICSEARCH_SCRIPT, payload)
            if not all(result.get(key) for key in ("ip", "ca_pem", "service_token", "version")):
                raise GuestError("Elasticsearch did not return its TLS integration details.")
            self._secrets.add(result["service_token"])
            return {
                "services": [
                    {
                        "name": "Elasticsearch",
                        "url": f"https://{self.ip}:9200",
                        "username": "elastic",
                        "password": secrets["elastic_password"],
                    }
                ],
                "elastic": result,
            }
        if role == "kibana":
            if not elastic or not re.fullmatch(r"9\.\d+\.\d+(?:[-+~][A-Za-z0-9.+~:-]+)?", elastic.get("version", "")):
                raise GuestError("Kibana requires a verified Elasticsearch 9.x installation and exact package version.")
            if "-----BEGIN CERTIFICATE-----" not in elastic.get("ca_pem", ""):
                raise GuestError("Kibana requires the Elasticsearch TLS CA certificate.")
            self._secrets.add(elastic["service_token"])
            payload.update({"elastic": elastic, "config": _kibana_config(self.ip, elastic, secrets)})
            log("Installing matching Kibana version and configuring its authenticated TLS connection to Elasticsearch.")
            self._run_script(_KIBANA_SCRIPT, payload)
            return {
                "services": [
                    {
                        "name": "Kibana",
                        "url": f"https://{self.ip}:5601",
                        "username": "elastic",
                        "password": secrets["elastic_password"],
                    }
                ]
            }
        if role == "splunk":
            _safe_text(secrets["splunk_password"], "Splunk admin password")
            if splunk_package is None:
                raise GuestError("A Splunk Enterprise Linux x86_64 .tgz package must be configured.")
            if not isinstance(splunk_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", splunk_sha256):
                raise GuestError("A verified Splunk package SHA-256 is required before installation.")
            payload["package_sha256"] = splunk_sha256
            _validate_splunk_archive(Path(splunk_package))
            log("Installing the supplied Splunk package, enabling HTTPS and its systemd service.")
            self._run_script(_SPLUNK_SCRIPT, payload, Path(splunk_package))
            return {
                "services": [
                    {
                        "name": "Splunk",
                        "url": f"https://{self.ip}:8000",
                        "username": "admin",
                        "password": secrets["splunk_password"],
                    }
                ]
            }
        raise GuestError("Unknown application role.")
