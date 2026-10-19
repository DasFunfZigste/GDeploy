"""Bounded Proxmox VE 9.x QEMU provisioning over the verified HTTPS API.

VMs carry a deployment UUID in their description. ISO names carry that same
UUID; cleanup checks both ownership and current attachments before deletion.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from .tls import CertificateError, PinnedCertificateAdapter, certificate_context
from .vmware import ESXiClient, VMwareError, _owner, _relative_path


class ProxmoxError(VMwareError):
    """A credential-free Proxmox operation failure."""


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ProxmoxError("Invalid Proxmox node, bridge or storage identifier.")
    return value


def _segment(value):
    return quote(str(value), safe="")


class ProxmoxClient:
    TASK_TIMEOUT = 900
    POLL_INTERVAL = 1

    def __init__(self, host, username, password, verify_tls=True, *, node="", iso_storage="", auth_type="token",
                 trusted_certificate=None):
        self.host, self.port, self.origin = self._parse_host(host)
        if verify_tls is not True:
            raise ProxmoxError("Proxmox TLS verification must remain enabled.")
        if auth_type not in ("token", "password"):
            raise ProxmoxError("Choose API token or password authentication for Proxmox.")
        pattern = r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+![A-Za-z0-9_.-]+" if auth_type == "token" else r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+"
        if not isinstance(username, str) or not re.fullmatch(pattern, username) or not isinstance(password, str) or not password:
            raise ProxmoxError("Supply a Proxmox user@realm!token ID and token secret, or user@realm and password.")
        if auth_type == "token" and (not password.isascii() or any(c.isspace() or ord(c) < 32 for c in password)):
            raise ProxmoxError("Invalid Proxmox API token secret.")
        self.username, self._password, self.auth_type = username, password, auth_type
        self.node = _name(node) if node else ""
        self.iso_storage = _name(iso_storage) if iso_storage else ""
        self._trusted_certificate = trusted_certificate
        self._http = None
        self._tls_context = None
        self._ticket_at = 0
        self.version = ""

    @staticmethod
    def _parse_host(value):
        try:
            if isinstance(value, str) and "://" not in value:
                try:
                    address = ipaddress.ip_address(value)
                except ValueError:
                    address = None
                if isinstance(address, ipaddress.IPv6Address):
                    value = f"https://[{address.compressed}]:8006"
                elif ":" not in value:
                    value = f"https://{value}:8006"
            elif isinstance(value, str) and urlsplit(value).port is None:
                parsed = urlsplit(value)
                # Keep validation of paths, credentials and schemes in one place.
                value = value.replace(parsed.netloc, parsed.netloc + ":8006", 1)
            return ESXiClient._parse_host(value)
        except (VMwareError, ValueError):
            raise ProxmoxError("Use a Proxmox hostname or IP, optionally with its HTTPS port (default 8006); omit paths and credentials.") from None

    def _request(self, method, path, *, data=None, params=None, files=None, headers=None, missing_ok=False, agent_pending=False):
        ESXiClient._require_worker()
        if self._http is None:
            raise ProxmoxError("The Proxmox client is not connected.")
        if not path.startswith("/") or any(c in path for c in ("?", "#", "\\")):
            raise ProxmoxError("Invalid Proxmox API operation.")
        if self._tls_context:
            self._tls_context.ensure_valid()
        # Password tickets expire after two hours. Renew before long multi-VM
        # jobs reach that boundary; API tokens need no session renewal.
        if self.auth_type == "password" and path != "/access/ticket" and time.monotonic() - self._ticket_at >= 5400:
            self._login()
        try:
            response = self._http.request(method, self.origin + "/api2/json" + path,
                data=data, params=params, files=files, headers=headers, timeout=(15, 120 if files or isinstance(data, _ISOBody) else 60),
                allow_redirects=False, verify=True if self._tls_context else (os.environ.get("REQUESTS_CA_BUNDLE") or True), stream=True)
            with response:
                if missing_ok and response.status_code in (404,):
                    return None
                # QGA is expected to be unavailable while the installer boots.
                if agent_pending and response.status_code == 500:
                    return None
                if response.status_code in (401, 403):
                    raise ProxmoxError("Proxmox rejected authentication or access. Check the account/token and required permissions.")
                if response.status_code >= 300:
                    raise ProxmoxError(f"Proxmox API operation failed (HTTP {response.status_code}); check node, storage, bridge and account permissions.")
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 4 * 1024**2:
                        raise ProxmoxError("Proxmox returned an oversized API response.")
                result = json.loads(body)
                if not isinstance(result, dict) or "data" not in result:
                    raise ProxmoxError("Proxmox returned an invalid API response.")
                return result["data"]
        except ProxmoxError:
            raise
        except (requests.exceptions.SSLError, CertificateError):
            raise ProxmoxError("Proxmox TLS verification failed. Retrieve and approve the current host certificate in Setup, or install its CA.") from None
        except requests.exceptions.Timeout:
            raise ProxmoxError("The Proxmox operation timed out; inspect the host task history before retrying provisioning.") from None
        except (requests.exceptions.RequestException, ValueError, OSError):
            raise ProxmoxError("The Proxmox HTTPS operation failed. Check connectivity and host task history.") from None

    def _login(self):
        self._ticket_at = time.monotonic()
        result = self._request("POST", "/access/ticket", data={"username": self.username, "password": self._password})
        if not isinstance(result, dict) or not all(isinstance(result.get(k), str) and result[k] for k in ("ticket", "CSRFPreventionToken")):
            raise ProxmoxError("Proxmox did not return a valid authentication ticket.")
        self._http.cookies.set("PVEAuthCookie", result["ticket"])
        self._http.headers["CSRFPreventionToken"] = result["CSRFPreventionToken"]

    def connect(self):
        ESXiClient._require_worker()
        if self._http is not None:
            return self
        self._http = requests.Session()
        self._http.trust_env = False
        try:
            if self._trusted_certificate:
                self._tls_context = certificate_context(self._trusted_certificate, hostname=self.host, port=self.port)
                adapter = PinnedCertificateAdapter(self._tls_context)
                self._http.mount("https://", adapter)
                self._http.mount("http://", adapter)
            if self.auth_type == "token":
                self._http.headers["Authorization"] = f"PVEAPIToken={self.username}={self._password}"
            else:
                self._login()
            version = self._request("GET", "/version")
            self.version = version.get("version", "") if isinstance(version, dict) else ""
            if not re.fullmatch(r"9\.\d+(?:[.-][A-Za-z0-9]+)*", self.version):
                raise ProxmoxError("This deployment integration requires Proxmox VE 9.x (target: 9.2).")
            nodes = self._request("GET", "/nodes")
            if not isinstance(nodes, list):
                raise ProxmoxError("Proxmox node inventory is invalid.")
            available = [item.get("node") for item in nodes if item.get("status") == "online"]
            if not self.node and len(available) == 1:
                self.node = _name(available[0])
            if self.node not in available:
                raise ProxmoxError("Select one online Proxmox node in this host's Setup configuration.")
            return self
        except BaseException:
            self.disconnect()
            raise

    def disconnect(self):
        if self._http is not None:
            self._http.close()
        self._http = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *args):
        self.disconnect()

    def _node_path(self, suffix=""):
        return "/nodes/" + _segment(_name(self.node)) + suffix

    def _vm_path(self, vm_id, suffix=""):
        if not isinstance(vm_id, str) or not re.fullmatch(r"[1-9][0-9]{2,8}", vm_id):
            raise ProxmoxError("Invalid Proxmox VM identifier.")
        return self._node_path("/qemu/" + vm_id + suffix)

    def _task(self, upid, operation):
        if not isinstance(upid, str) or not upid.startswith("UPID:" + self.node + ":"):
            raise ProxmoxError(f"{operation} did not return a task on the configured Proxmox node.")
        deadline = time.monotonic() + self.TASK_TIMEOUT
        while time.monotonic() < deadline:
            result = self._request("GET", self._node_path("/tasks/" + _segment(upid) + "/status"))
            if isinstance(result, dict) and result.get("status") == "stopped":
                if result.get("exitstatus") != "OK":
                    detail = re.sub(r"[\x00-\x1f\x7f]", " ", str(result.get("exitstatus", "Unknown task failure")))[:500]
                    raise ProxmoxError(f"{operation} failed: {detail}. Inspect the Proxmox task log for this node.")
                return
            time.sleep(self.POLL_INTERVAL)
        raise ProxmoxError(f"{operation} timed out. Proxmox may still be processing it; inspect the node task before recovery.")

    def inventory(self):
        status = self._request("GET", self._node_path("/status"))
        storage = self._request("GET", self._node_path("/storage"), params={"enabled": 1})
        networks = self._request("GET", self._node_path("/network"))
        vms = self._request("GET", self._node_path("/qemu"))
        if not isinstance(status, dict) or not all(isinstance(value, list) for value in (storage, networks, vms)):
            raise ProxmoxError("Proxmox returned incomplete node inventory.")
        cpu, memory = status.get("cpuinfo", {}), status.get("memory", {})
        active = [item for item in storage if item.get("active") and item.get("enabled", 1)]
        def stores(content):
            return [{"name": _name(item["storage"]), "free_gb": item.get("avail", 0) / 1024**3,
                     "capacity_gb": item.get("total", 0) / 1024**3}
                    for item in active if content in item.get("content", "").split(",")]
        return {
            "hypervisor": "proxmox", "node": self.node, "version": self.version,
            "host": {"name": self.node, "cpu_threads": int(cpu.get("cpus", 0)),
                     "cpu_cores": int(cpu.get("cores", 0)) * int(cpu.get("sockets", 1)),
                     "memory_gb": memory.get("total", 0) / 1024**3,
                     "free_memory_gb": memory.get("free", 0) / 1024**3},
            "datastores": stores("images"), "iso_storages": stores("iso"),
            "networks": [{"name": _name(item["iface"])} for item in networks
                         if item.get("type") in ("bridge", "OVSBridge") and item.get("active")],
            "vms": [{"name": item.get("name", ""), "vm_id": str(item["vmid"]),
                     "cpu": item.get("cpus", 0), "ram_gb": item.get("maxmem", 0) / 1024**3} for item in vms],
        }

    def _require_vm_audit(self):
        permissions = self._request("GET", "/access/permissions", params={"path": "/vms"})
        if not isinstance(permissions, dict) or not permissions.get("/vms", {}).get("VM.Audit"):
            raise ProxmoxError("Recovery requires propagated VM.Audit on /vms to inspect all VMs before deleting resources.")

    def preflight_permissions(self, spec):
        """Require node-wide visibility so recovery cannot silently miss VMs."""
        required = {
            "/nodes/" + self.node: {"Sys.Audit"},
            "/vms": {"VM.Audit", "VM.Allocate", "VM.PowerMgmt", "VM.Config.CPU", "VM.Config.Memory",
                     "VM.Config.Disk", "VM.Config.Network", "VM.Config.CDROM", "VM.Config.Options", "VM.Config.HWType"},
        }
        for store in {vm["datastore"] for vm in spec["vms"]}:
            required.setdefault("/storage/" + _name(store), set()).update({"Datastore.Audit", "Datastore.AllocateSpace"})
        required.setdefault("/storage/" + _name(self.iso_storage), set()).update({"Datastore.Audit", "Datastore.AllocateTemplate"})
        for bridge in {vm[key] for vm in spec["vms"] for key in ("network", "monitor_network") if vm.get(key)}:
            required["/sdn/zones/localnetwork/" + _name(bridge)] = {"SDN.Use"}
        for path, privileges in required.items():
            result = self._request("GET", "/access/permissions", params={"path": path})
            effective = result.get(path, {}) if isinstance(result, dict) else {}
            missing = privileges - {key for key, value in effective.items() if value}
            if path == "/vms" and not any(effective.get(key) for key in ("VM.GuestAgent.Audit", "VM.GuestAgent.Unrestricted")):
                missing.add("VM.GuestAgent.Audit")
            if missing:
                raise ProxmoxError("Proxmox permissions missing at " + path + ": " + ", ".join(sorted(missing)) +
                                   ". Grant propagated access to the user and, for separated tokens, the token too.")

    @staticmethod
    def _iso_filename(path, owner_id=None):
        path = _relative_path(path, owner_id, iso=True)
        _, owner, filename = path.split("/")
        return f"gdeploy-{owner}-{filename}"

    def _iso_volume(self, path, owner_id=None):
        store = _name(self.iso_storage)
        return store + ":iso/" + self._iso_filename(path, owner_id)

    def upload_iso(self, datastore, remote, local):
        _name(datastore)
        filename = self._iso_filename(remote)
        volume = self._iso_volume(remote)
        existing = self._request("GET", self._node_path("/storage/" + _segment(self.iso_storage) + "/content"), params={"content": "iso"})
        if any(item.get("volid") == volume for item in existing):
            raise ProxmoxError("Deployment ISO already exists on Proxmox; refusing to overwrite it.")
        with Path(local).open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        # Stream multipart bytes rather than allowing requests to read a DVD
        # into memory. The encoder has a deterministic content length.
        with Path(local).open("rb") as stream:
            body = _ISOBody(stream, filename, checksum)
            upid = self._request("POST", self._node_path("/storage/" + _segment(self.iso_storage) + "/upload"),
                                 data=body, headers={"Content-Type": body.content_type})
        self._task(upid, "Upload Proxmox installation ISO")

    def create_vm(self, spec, iso_path, owner_id):
        owner = _owner(owner_id)
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]", spec.get("name", "")):
            raise ProxmoxError("Invalid virtual machine name.")
        for key, minimum, maximum in (("cpu", 1, 128), ("ram_gb", 2, 2048), ("disk_gb", 25, 65536)):
            if type(spec.get(key)) is not int or not minimum <= spec[key] <= maximum:
                raise ProxmoxError("Invalid virtual machine resource configuration.")
        vmid = str(self._request("GET", "/cluster/nextid"))
        self._vm_path(vmid)
        bridge, store = _name(spec["network"]), _name(spec["datastore"])
        # Explicit locally administered MACs also let a sensor's unattended
        # installation bind management/capture adapters before the first boot.
        macs = ["02:" + ":".join(f"{byte:02x}" for byte in secrets.token_bytes(5)) for _ in range(2)]
        config = {"vmid": vmid, "name": spec["name"], "description": "GDeploy deployment " + owner,
                  "tags": "gdeploy", "ostype": "l26", "bios": "seabios", "machine": "q35", "cpu": "host",
                  "sockets": 1, "cores": spec["cpu"], "memory": spec["ram_gb"] * 1024, "balloon": 0,
                  "agent": "enabled=1", "scsihw": "virtio-scsi-single", "scsi0": f"{store}:{spec['disk_gb']}",
                  "net0": f"virtio={macs[0]},bridge={bridge}", "boot": "order=scsi0", "onboot": 0}
        if spec["role"] == "corelight_sensor":
            config["net1"] = f"virtio={macs[1]},bridge={_name(spec['monitor_network'])}"
        if iso_path:
            config["ide2"] = self._parse_iso_reference(iso_path, owner)
            config["boot"] = "order=scsi0;ide2"
        self._task(self._request("POST", self._node_path("/qemu"), data=config), "Create Proxmox VM")
        self._owned(vmid, owner)
        return vmid

    def _owned(self, vmid, owner, *, missing_ok=False):
        owner = _owner(owner)
        config = self._request("GET", self._vm_path(vmid, "/config"), missing_ok=missing_ok)
        if config is None and missing_ok:
            return None
        if not isinstance(config, dict) or config.get("description") != "GDeploy deployment " + owner:
            raise ProxmoxError("Proxmox VM ownership does not match this deployment; refusing the operation.")
        return config

    def _parse_iso_reference(self, reference, owner):
        match = re.fullmatch(r"\[([^\[\]]+)\] (.+)", reference or "")
        if not match:
            raise ProxmoxError("Installation media is not a deployment-owned ISO reference.")
        _name(match[1])
        return self._iso_volume(match[2], owner) + ",media=cdrom"

    def network_macs(self, vm_id, owner_id):
        config = self._owned(vm_id, owner_id)
        macs = []
        for key in ("net0", "net1"):
            match = re.search(r"(?:^|,)virtio=([0-9a-fA-F:]{17})(?:,|$)", config.get(key, ""))
            if not match or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", match[1]):
                raise ProxmoxError("Proxmox did not return both sensor adapter MAC addresses.")
            macs.append(match[1].lower())
        if macs[0] == macs[1] or any(int(mac[:2], 16) & 1 for mac in macs):
            raise ProxmoxError("Proxmox sensor adapter MAC addresses must be distinct unicast addresses.")
        return dict(zip(("management_mac", "monitor_mac"), macs))

    def attach_iso(self, vm_id, iso_path, owner_id):
        config = self._owned(vm_id, owner_id)
        state = self._request("GET", self._vm_path(vm_id, "/status/current"))
        if state.get("status") != "stopped":
            raise ProxmoxError("Stop the owned Proxmox VM before attaching installation media.")
        self._task(self._request("POST", self._vm_path(vm_id, "/config"), data={
            "ide2": self._parse_iso_reference(iso_path, owner_id), "boot": "order=scsi0;ide2", "digest": config["digest"],
        }), "Attach Proxmox installation ISO")

    def power_on(self, vm_id, owner_id):
        self._owned(vm_id, owner_id)
        self._task(self._request("POST", self._vm_path(vm_id, "/status/start")), "Start Proxmox VM")

    def guest_ip(self, vm_id):
        result = self._request("GET", self._vm_path(vm_id, "/agent/network-get-interfaces"), agent_pending=True)
        interfaces = result.get("result", []) if isinstance(result, dict) else []
        config = self._request("GET", self._vm_path(vm_id, "/config"))
        match = re.search(r"virtio=([0-9a-fA-F:]{17})", config.get("net0", ""))
        if not match:
            raise ProxmoxError("Could not identify the Proxmox management adapter.")
        for interface in interfaces:
            if str(interface.get("hardware-address", "")).lower() != match[1].lower():
                continue
            for value in interface.get("ip-addresses", []):
                try:
                    address = ipaddress.IPv4Address(value.get("ip-address"))
                except (ValueError, TypeError):
                    continue
                if not (address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified):
                    return str(address)
        return None

    def detach_iso(self, vm_id, owner_id):
        config = self._owned(vm_id, owner_id)
        media = config.get("ide2", "")
        if media and media != "none,media=cdrom":
            pattern = re.escape(_name(self.iso_storage) + ":iso/gdeploy-" + _owner(owner_id) + "-")
            if not re.match(pattern + r"[a-z0-9-]+\.iso,media=cdrom(?:,|$)", media):
                raise ProxmoxError("The Proxmox CD drive contains media owned outside this deployment.")
        self._task(self._request("POST", self._vm_path(vm_id, "/config"), data={
            "ide2": "none,media=cdrom", "boot": "order=scsi0", "digest": config["digest"],
        }), "Detach Proxmox installation ISO")

    def delete_iso(self, datastore, remote, owner_id):
        _name(datastore)
        volume = self._iso_volume(remote, owner_id)
        self._require_vm_audit()
        for vm in self._request("GET", "/cluster/resources", params={"type": "vm"}):
            if vm.get("type") != "qemu":
                continue
            node = _name(vm["node"])
            vmid = str(vm["vmid"])
            self._vm_path(vmid)  # validate ID before building the other-node path
            config = self._request("GET", "/nodes/" + _segment(node) + "/qemu/" + vmid + "/config", params={"current": 1})
            if any(isinstance(value, str) and value.split(",", 1)[0] == volume for value in config.values()):
                raise ProxmoxError("Proxmox ISO is still attached to a VM; refusing deletion.")
        content = self._request("GET", self._node_path("/storage/" + _segment(self.iso_storage) + "/content"), params={"content": "iso"})
        if not any(item.get("volid") == volume for item in content):
            return
        self._task(self._request("DELETE", self._node_path("/storage/" + _segment(self.iso_storage) + "/content/" + _segment(volume))), "Delete Proxmox installation ISO")

    def find_owned_vms(self, owner_id):
        self._require_vm_audit()
        result = []
        for vm in self._request("GET", self._node_path("/qemu")):
            vmid = str(vm["vmid"])
            config = self._request("GET", self._vm_path(vmid, "/config"))
            if config.get("description") == "GDeploy deployment " + _owner(owner_id):
                result.append({"vm_id": vmid, "name": config.get("name", "")})
        return result

    def destroy_vm(self, vm_id, owner_id):
        config = self._owned(vm_id, owner_id, missing_ok=True)
        if config is None:
            return
        # Refuse foreign or extra disks. Normal deletion removes only this
        # VM's own disk; never request deletion of unreferenced storage.
        for key, value in config.items():
            if re.fullmatch(r"(?:efidisk|tpmstate)\d+|vmstate", key):
                raise ProxmoxError("Owned VM contains additional state storage; inspect it before recovery.")
            if re.fullmatch(r"(?:scsi|sata|virtio|ide|unused)\d+", key) and value != "none,media=cdrom":
                volume = str(value).split(",", 1)[0]
                if key == "ide2":
                    if not re.fullmatch(re.escape(self.iso_storage + ":iso/gdeploy-" + _owner(owner_id) + "-") + r"[a-z0-9-]+\.iso", volume):
                        raise ProxmoxError("Owned VM contains a foreign ISO; detach it before recovery.")
                elif key != "scsi0" or not re.fullmatch(r"[A-Za-z0-9_.-]+:(?:[0-9]+/)?vm-" + re.escape(vm_id) + r"-disk-[0-9]+(?:\.(?:raw|qcow2|vmdk))?", volume):
                    raise ProxmoxError("Owned VM contains an unexpected or foreign disk; inspect it before recovery.")
        state = self._request("GET", self._vm_path(vm_id, "/status/current"))
        if state.get("status") != "stopped":
            self._task(self._request("POST", self._vm_path(vm_id, "/status/stop")), "Stop owned Proxmox VM for deletion")
        # Recheck ownership and use the config digest after power-state changes.
        current = self._owned(vm_id, owner_id)
        if current.get("digest") != config.get("digest"):
            raise ProxmoxError("The VM configuration changed during cleanup. Inspect it and retry recovery.")
        self._task(self._request("DELETE", self._vm_path(vm_id), data={"purge": 0, "destroy-unreferenced-disks": 0}), "Delete owned Proxmox VM")


class _ISOBody:
    """A constant-memory, fixed-length multipart upload of a remastered ISO."""

    def __init__(self, stream, filename, checksum):
        boundary = "gdeploy" + secrets.token_hex(24)
        fields = {"content": "iso", "checksum": checksum, "checksum-algorithm": "sha256"}
        prefix = b"".join((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n").encode() for key, value in fields.items())
        prefix += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"filename\"; filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        self.stream, self.prefix, self.suffix = stream, prefix, f"\r\n--{boundary}--\r\n".encode()
        self.remaining = len(prefix) + Path(stream.name).stat().st_size + len(self.suffix)
        self.length = self.remaining
        self.content_type = "multipart/form-data; boundary=" + boundary

    def __len__(self):
        return self.length

    def read(self, size=1024**2):
        size = min(size if size is not None and size > 0 else 1024**2, 1024**2)
        output = self.prefix[:size]
        self.prefix = self.prefix[len(output):]
        if len(output) < size:
            chunk = self.stream.read(size - len(output))
            output += chunk
            if len(output) < size:
                take = self.suffix[:size - len(output)]
                self.suffix = self.suffix[len(take):]
                output += take
        self.remaining -= len(output)
        return output
