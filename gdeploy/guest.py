"""Ubuntu autoinstall media and SSH application provisioning.

Secret material is transferred using SFTP, never command arguments. The first
successful SSH connection uses trust on first use; callers persist ``host_key``
and supply it on every later connection to the VM.
"""

from __future__ import annotations

import io
import hashlib
import ipaddress
import json
import posixpath
import re
import secrets as secret_tools
import shlex
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Callable

import paramiko
import yaml
from passlib.hash import sha512_crypt


class GuestError(RuntimeError):
    """A sanitized failure provisioning the guest."""


class GuestConnectionError(GuestError):
    """A transient connection/authentication failure while the guest boots."""


class GuestHostKeyError(GuestError):
    """A fatal mismatch with a previously observed SSH host identity."""


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


def _autoinstall_data(spec: dict, username: str, password: str, ssh_public_key: str) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", username):
        raise GuestError("Ubuntu username must start with a letter and be at most 32 characters.")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", spec["name"]):
        raise GuestError("Invalid Ubuntu hostname.")
    _safe_text(password, "OS password")
    _safe_text(ssh_public_key, "SSH public key")
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
    return {
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
            "ssh": {"install-server": True, "allow-pw": True, "authorized-keys": [ssh_public_key]},
            "packages": ["open-vm-tools", "openssh-server", "python3", "ca-certificates"],
            "updates": "security",
            "late-commands": [
                "curtin in-target --target=/target -- systemctl enable open-vm-tools ssh",
            ],
            "shutdown": "reboot",
        }
    }


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


def build_seed_iso(
    source_iso: Path, output_iso: Path, spec: dict, username: str, password: str, ssh_public_key: str
) -> None:
    """Replay the original ISO's boot metadata while adding a NoCloud seed."""
    source_iso, output_iso = Path(source_iso), Path(output_iso)
    if not source_iso.is_file() or source_iso.resolve() == output_iso.resolve():
        raise GuestError("A separate readable Ubuntu 24.04 live-server ISO is required.")
    if output_iso.exists():
        raise GuestError("Refusing to overwrite an existing deployment ISO.")
    data = _autoinstall_data(spec, username, password, ssh_public_key)
    output_iso.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="gdeploy-iso-", dir=output_iso.parent) as folder:
            root = Path(folder)
            files = {"/boot/grub/grub.cfg": root / "grub.cfg", "/boot/grub/loopback.cfg": root / "loopback.cfg"}
            for iso_path, local in files.items():
                subprocess.run(
                    ["xorriso", "-osirrox", "on", "-indev", str(source_iso), "-extract", iso_path, str(local)],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
                local.write_text(_patch_grub(local.read_text()))
            user_data, meta_data = root / "user-data", root / "meta-data"
            user_data.write_text("#cloud-config\n" + yaml.safe_dump(data, sort_keys=False))
            user_data.chmod(0o600)
            meta_data.write_text(
                yaml.safe_dump({"instance-id": f"gdeploy-{secret_tools.token_hex(16)}", "local-hostname": spec["name"]})
            )
            files.update({"/nocloud/user-data": user_data, "/nocloud/meta-data": meta_data})
            manifest = root / "md5sum.txt"
            subprocess.run(
                ["xorriso", "-osirrox", "on", "-indev", str(source_iso), "-extract", "/md5sum.txt", str(manifest)],
                check=True,
                capture_output=True,
                timeout=120,
            )
            manifest.write_text(_update_md5_manifest(manifest.read_text(), files))
            files["/md5sum.txt"] = manifest
            command = ["xorriso", "-indev", str(source_iso), "-outdev", str(output_iso), "-boot_image", "any", "replay"]
            for iso_path, local in files.items():
                command.extend(["-map", str(local), iso_path])
            command.extend(["-commit", "-end"])
            subprocess.run(command, check=True, capture_output=True, timeout=1800)
            output_iso.chmod(0o600)
    except (OSError, subprocess.SubprocessError) as exc:
        # xorriso output can contain the seed; do not put raw subprocess errors in logs.
        if output_iso.exists():
            output_iso.unlink()
        raise GuestError(
            "Could not build Ubuntu installation media; check xorriso, ISO format, and free disk space."
        ) from exc


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


def _validate_splunk_archive(package: Path) -> None:
    """Reject unsafe tar paths and non-x86_64 packages before upload/extraction."""
    try:
        seen = set()
        links: dict[str, str] = {}
        machine = None
        with tarfile.open(package, "r:gz") as archive:
            for member in archive:
                name = member.name.removeprefix("./")
                normalized = posixpath.normpath(name)
                if name.startswith("/") or normalized.split("/")[0] != "splunk" or ".." in name.split("/"):
                    raise GuestError("Splunk archive contains an unsafe file path.")
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
        if "splunk/bin/splunk" not in seen or machine != 62:
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

    def __enter__(self) -> GuestSession:
        self._connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.client is not None:
            self.client.close()
            self.client = None

    def _connect(self) -> None:
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

    def _exec(self, command: str, *, timeout: int = 1800, sudo: bool = False) -> str:
        if self.client is None:
            raise GuestError("SSH session is not connected.")
        if sudo:
            command = "sudo -k -S -p '' -- /bin/bash -c " + shlex.quote(command)
        channel = None
        try:
            transport = self.client.get_transport()
            channel = transport.open_session(timeout=15)
            channel.set_combine_stderr(True)
            channel.exec_command(command)
            if sudo:
                channel.sendall((self.password + "\n").encode())
            channel.shutdown_write()
            deadline = time.monotonic() + timeout
            tail = bytearray()
            while True:
                while channel.recv_ready():
                    tail.extend(channel.recv(32768))
                    del tail[:-131072]
                if channel.exit_status_ready() and not channel.recv_ready():
                    status = channel.recv_exit_status()
                    output = tail.decode("utf-8", "replace")
                    if status != 0:
                        safe = self._sanitize(output).strip()[-3000:]
                        raise GuestError(f"Guest command failed (exit {status}). {safe}")
                    return output
                if time.monotonic() >= deadline:
                    raise GuestError("Guest command exceeded its time limit; check the guest console and service logs.")
                time.sleep(0.1)
        except (OSError, paramiko.SSHException) as exc:
            raise GuestError("SSH connection was lost during guest provisioning.") from exc
        finally:
            if channel is not None:
                channel.close()

    def wait_ready(self, timeout: int = 1800) -> None:
        """Connect if needed, then require successful cloud-init completion."""
        deadline = time.monotonic() + timeout
        while self.client is None:
            try:
                self._connect()
            except GuestConnectionError:
                if time.monotonic() + 5 >= deadline:
                    raise
                time.sleep(5)
        output = self._exec("cloud-init status --wait --format json", timeout=max(1, int(deadline - time.monotonic())))
        try:
            status = json.loads(output)
        except json.JSONDecodeError as exc:
            raise GuestError("Could not verify Ubuntu cloud-init completion.") from exc
        if status.get("status") != "done" or status.get("errors"):
            raise GuestError("Ubuntu cloud-init did not finish successfully; inspect the guest installation logs.")

    def _run_script(self, script: str, payload: dict, package: Path | None = None) -> dict:
        if self.client is None:
            raise GuestError("SSH session is not connected.")
        remote = f"/home/{self.username}/.gdeploy-{secret_tools.token_hex(16)}"
        transferred = False
        try:
            with self.client.open_sftp() as sftp:
                sftp.mkdir(remote, 0o700)
                for filename, contents in (("install.sh", script), ("payload.json", json.dumps(payload))):
                    with sftp.file(f"{remote}/{filename}", "w") as stream:
                        stream.write(contents)
                    sftp.chmod(f"{remote}/{filename}", 0o600)
                if package is not None:
                    sftp.put(str(package), f"{remote}/splunk.tgz")
                    sftp.chmod(f"{remote}/splunk.tgz", 0o600)
            transferred = True
            quoted = shlex.quote(remote)
            # Scripts and their secret payload become root-owned before execution.
            # A trap removes all staging files on either success or failure.
            wrapper = (
                f"set -e; trap 'rm -rf -- {quoted}' EXIT; chown -R root:root -- {quoted}; /bin/bash {quoted}/install.sh"
            )
            output = self._exec(wrapper, timeout=2400, sudo=True)
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
                    self._exec(f"rm -rf -- {shlex.quote(remote)}", timeout=30)
                except GuestError:
                    pass

    def install(
        self,
        role: str,
        secrets: dict,
        elastic: dict | None = None,
        splunk_package: Path | None = None,
        log: Callable[[str], None] = lambda message: None,
    ) -> dict:
        self._secrets.update(value for value in secrets.values() if isinstance(value, str))
        payload: dict = {"ip": self.ip, "secrets": secrets}
        if role == "ubuntu":
            log("Ubuntu installation verified; no application selected.")
            return {"services": []}
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
