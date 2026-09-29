"""Synchronous, bounded operations against a standalone ESXi host.

Call this module from the deployment worker or ``asyncio.to_thread``.  Operations
reject calls made on a running event loop so network I/O cannot freeze the API.
Deletion requires both the deployment annotation and deployment-scoped storage.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import ssl
import time
import uuid
from functools import wraps
from http.cookies import SimpleCookie
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import requests
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl


class VMwareError(RuntimeError):
    """A sanitized ESXi operation or validation failure."""


_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_PATH_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DATASTORE_PATH = re.compile(r"\[([^\[\]\r\n]+)\] (.+)\Z")


def _guarded(action: str) -> Callable:
    def decorate(method: Callable) -> Callable:
        @wraps(method)
        def wrapped(self: "ESXiClient", *args: Any, **kwargs: Any) -> Any:
            try:
                self._require_worker()
                return method(self, *args, **kwargs)
            except VMwareError:
                raise
            except Exception as exc:
                raise VMwareError(f"{action} failed: {self._safe_error(exc)}") from None

        return wrapped

    return decorate


def _owner(owner_id: str) -> str:
    try:
        if not isinstance(owner_id, str) or str(uuid.UUID(owner_id)) != owner_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise VMwareError("Deployment ownership must be a canonical lowercase UUID.") from None
    return owner_id


def _datastore_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name.strip()
        or any(c in name for c in "[]/\\\r\n\x00")
        or any(ord(c) < 32 for c in name)
    ):
        raise VMwareError("The datastore name is invalid.")
    return name


def _relative_path(path: str, owner_id: str | None = None, *, iso: bool = False) -> str:
    if not isinstance(path, str):
        raise VMwareError("The datastore path must be a deployment-owned relative path.")
    parts = path.split("/")
    if (
        len(parts) < 3
        or parts[0] != "gdeploy"
        or any(p in ("", ".", "..") for p in parts)
        or any(not _PATH_PART.fullmatch(p) for p in parts)
        or str(PurePosixPath(path)) != path
    ):
        raise VMwareError("The datastore path must remain inside gdeploy/<deployment UUID>/.")
    actual_owner = _owner(parts[1])
    if owner_id is not None and actual_owner != _owner(owner_id):
        raise VMwareError("The datastore path belongs to a different deployment.")
    if iso and not parts[-1].endswith(".iso"):
        raise VMwareError("Installation media must have an .iso filename.")
    return path


def _split_datastore_path(path: str, owner_id: str) -> tuple[str, str]:
    match = _DATASTORE_PATH.fullmatch(path) if isinstance(path, str) else None
    if not match:
        raise VMwareError("Resource storage is not an owned datastore path; refusing the operation.")
    datastore, relative = match.groups()
    return _datastore_name(datastore), _relative_path(relative, owner_id)


class ESXiClient:
    """An authenticated, short-lived connection to exactly one ESXi host."""

    SOCKET_TIMEOUT = 60
    TASK_TIMEOUT = 900
    POLL_INTERVAL = 1.0
    UPLOAD_TIMEOUT = (15, 120)

    def __init__(self, host: str, username: str, password: str, verify_tls: bool = True):
        self.host, self.port, self._origin = self._parse_host(host)
        if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
            raise VMwareError("An ESXi username and password are required.")
        if not isinstance(verify_tls, bool):
            raise VMwareError("TLS verification must be true or false.")
        self.username = username
        self._password = password
        self.verify_tls = verify_tls
        self._si: Any = None
        self._content: Any = None
        self._host: Any = None
        self._dc: Any = None
        self._http: requests.Session | None = None

    @staticmethod
    def _parse_host(value: str) -> tuple[str, int, str]:
        try:
            if not isinstance(value, str) or not value or value != value.strip() or any(c.isspace() for c in value):
                raise ValueError
            target = value
            if "://" not in value:
                try:
                    address = ipaddress.ip_address(value)
                except ValueError:
                    address = None
                if isinstance(address, ipaddress.IPv6Address):
                    target = f"[{address.compressed}]"
                target = f"https://{target}"
            parsed = urlsplit(target)
            hostname = parsed.hostname
            port = 443 if parsed.port is None else parsed.port
            if (
                parsed.scheme != "https"
                or not hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
                or not 1 <= port <= 65535
            ):
                raise ValueError
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                if len(hostname) > 253 or not all(
                    re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                    for part in hostname.rstrip(".").split(".")
                ):
                    raise ValueError
            authority = f"[{hostname}]" if ":" in hostname else hostname
            return hostname, port, f"https://{authority}:{port}"
        except (ValueError, TypeError, AttributeError):
            raise VMwareError(
                "Use an ESXi hostname or IP address, optionally with an HTTPS port; omit credentials and paths."
            ) from None

    @staticmethod
    def _require_worker() -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise VMwareError("Blocking ESXi operations must run in a worker thread.")

    def _safe_error(self, exc: BaseException) -> str:
        if isinstance(exc, (requests.exceptions.SSLError, ssl.SSLError)):
            return "TLS certificate verification or negotiation failed. Install the ESXi CA certificate or review the TLS setting."
        if isinstance(exc, vim.fault.InvalidLogin):
            return "ESXi rejected the configured credentials."
        if isinstance(exc, (requests.exceptions.Timeout, TimeoutError)):
            return "The ESXi connection timed out. Check host connectivity."
        if isinstance(exc, requests.exceptions.RequestException):
            return "The HTTPS connection to ESXi failed. Check the host address, certificate, and connectivity."
        details = str(getattr(exc, "msg", "") or "").replace("\n", " ").replace("\r", " ")
        # Only vSphere's explicit fault message is useful here; never stringify
        # arbitrary exceptions, which may contain request headers or arguments.
        if not isinstance(exc, vmodl.MethodFault):
            return f"{type(exc).__name__}; check ESXi connectivity and host permissions."
        details = details.replace(self._password, "[redacted]")
        if self._si is not None:
            cookie = getattr(getattr(self._si, "_stub", None), "cookie", "")
            if isinstance(cookie, str):
                for secret in re.findall(r'vmware_soap_session="?([^"; ]+)', cookie):
                    details = details.replace(secret, "[redacted]")
        details = re.sub(r"(?i)(password|authorization|cookie|token)\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", details)
        return f"{type(exc).__name__}: {details[:400]}" if details else type(exc).__name__

    def _require_connection(self) -> None:
        if self._si is None or self._content is None:
            raise VMwareError("The ESXi client is not connected.")

    def _objects(self, object_type: Any) -> list[Any]:
        self._require_connection()
        view = self._content.viewManager.CreateContainerView(self._content.rootFolder, [object_type], True)
        try:
            return list(view.view)
        finally:
            view.Destroy()

    @_guarded("Connect to ESXi")
    def connect(self) -> "ESXiClient":
        if self._si is not None:
            return self
        context = ssl.create_default_context() if self.verify_tls else ssl._create_unverified_context()
        try:
            self._si = SmartConnect(
                host=self.host,
                port=self.port,
                user=self.username,
                pwd=self._password,
                sslContext=context,
                httpConnectionTimeout=self.SOCKET_TIMEOUT,
            )
            self._content = self._si.RetrieveContent()
            if self._content.about.apiType != "HostAgent":
                raise VMwareError(
                    "GDeploy requires a direct connection to a standalone ESXi host; vCenter is unsupported."
                )
            hosts = self._objects(vim.HostSystem)
            datacenters = self._objects(vim.Datacenter)
            if len(hosts) != 1 or len(datacenters) != 1:
                raise VMwareError("Expected exactly one ESXi host and its standalone datacenter.")
            self._host, self._dc = hosts[0], datacenters[0]
            if self._host.runtime.connectionState != "connected":
                raise VMwareError("The ESXi host is not connected.")
            if self._host.runtime.inMaintenanceMode:
                raise VMwareError("The ESXi host is in maintenance mode.")
            self._http = requests.Session()
            self._http.trust_env = False
            return self
        except Exception:
            self.disconnect()
            raise

    def disconnect(self) -> None:
        self._require_worker()
        try:
            if self._si is not None:
                try:
                    Disconnect(self._si)
                except Exception:
                    # Closing a session must not conceal an installation error.
                    pass
        finally:
            if self._http is not None:
                self._http.close()
            self._si = self._content = self._host = self._dc = self._http = None

    def __enter__(self) -> "ESXiClient":
        return self.connect()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.disconnect()

    def _datastore(self, name: str) -> Any:
        _datastore_name(name)
        self._require_connection()
        matches = [ds for ds in self._host.datastore if ds.name == name]
        if len(matches) != 1:
            raise VMwareError("The selected datastore does not exist uniquely on this ESXi host.")
        datastore = matches[0]
        if not datastore.summary.accessible:
            raise VMwareError("The selected datastore is inaccessible.")
        maintenance = getattr(datastore.summary, "maintenanceMode", "normal")
        if maintenance and maintenance != "normal":
            raise VMwareError("The selected datastore is in maintenance mode.")
        return datastore

    @_guarded("Read ESXi inventory")
    def inventory(self) -> dict:
        self._require_connection()
        host = self._host
        memory_bytes = host.hardware.memorySize
        host_info = {
            "name": host.name,
            "cpu_threads": int(host.hardware.cpuInfo.numCpuThreads),
            "memory_gb": round(memory_bytes / 1024**3, 3),
        }
        usage_mb = getattr(host.summary.quickStats, "overallMemoryUsage", None)
        if isinstance(usage_mb, (int, float)):
            host_info["free_memory_gb"] = round(max(0, memory_bytes - usage_mb * 1024**2) / 1024**3, 3)
        datastores = [
            {
                "name": ds.name,
                "free_gb": round(ds.summary.freeSpace / 1024**3, 3),
                "capacity_gb": round(ds.summary.capacity / 1024**3, 3),
            }
            for ds in host.datastore
            if ds.summary.accessible and getattr(ds.summary, "maintenanceMode", "normal") in (None, "normal")
        ]
        return {
            "host": host_info,
            "datastores": sorted(datastores, key=lambda row: row["name"]),
            "networks": [{"name": name} for name in sorted({net.name for net in host.network})],
            "vms": [{"name": name} for name in sorted(vm.name for vm in self._objects(vim.VirtualMachine))],
        }

    def _wait_task(self, task: Any, operation: str, *, missing_ok: bool = False) -> Any:
        deadline = time.monotonic() + self.TASK_TIMEOUT
        while True:
            info = task.info
            if info.state == vim.TaskInfo.State.success:
                return info.result
            if info.state == vim.TaskInfo.State.error:
                error = info.error
                if missing_ok and isinstance(error, (vim.fault.FileNotFound, vmodl.fault.ManagedObjectNotFound)):
                    return None
                raise VMwareError(f"{operation} failed: {self._safe_error(error)}")
            if time.monotonic() >= deadline:
                raise VMwareError(
                    f"{operation} timed out. ESXi may still be processing the task; inspect and clean up this deployment before retrying."
                )
            time.sleep(self.POLL_INTERVAL)

    def _make_directory(self, datastore: str, relative_path: str) -> None:
        try:
            self._content.fileManager.MakeDirectory(
                name=f"[{datastore}] {relative_path}", datacenter=self._dc, createParentDirectories=True
            )
        except vim.fault.FileAlreadyExists:
            pass

    @_guarded("Upload installation ISO")
    def upload_iso(self, datastore: str, remote_path: str, local_path: Path) -> None:
        """Upload media. The caller must persist its remote path BEFORE calling."""
        self._datastore(datastore)
        _relative_path(remote_path, iso=True)
        local_path = Path(local_path)
        if not local_path.is_file() or local_path.stat().st_size == 0:
            raise VMwareError("The remastered installation ISO is missing or empty.")
        cookie = SimpleCookie()
        cookie.load(self._si._stub.cookie)
        session = cookie.get("vmware_soap_session")
        if session is None or any(c in session.value for c in "\r\n"):
            raise VMwareError("ESXi did not provide a valid datastore upload session.")
        headers = {
            "Cookie": f"vmware_soap_session={session.coded_value}",
            "Content-Type": "application/octet-stream",
        }
        self._make_directory(datastore, str(PurePosixPath(remote_path).parent))
        assert self._http is not None
        with local_path.open("rb") as source:
            response = self._http.put(
                f"{self._origin}/folder/{quote(remote_path, safe='/')}",
                params={"dcPath": self._dc.name, "dsName": datastore},
                data=source,
                headers=headers,
                verify=self.verify_tls,
                timeout=self.UPLOAD_TIMEOUT,
                allow_redirects=False,
            )
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                raise VMwareError(
                    "ESXi redirected the datastore upload; redirects are disabled to protect session credentials."
                )
            if response.status_code not in (200, 201, 204):
                raise VMwareError(
                    f"ESXi rejected the datastore upload (HTTP {response.status_code}). Check datastore space and upload permissions."
                )
        finally:
            response.close()

    @_guarded("Create virtual machine")
    def create_vm(self, spec: dict, iso_path: str, owner_id: str) -> str:
        owner_id = _owner(owner_id)
        self._require_connection()
        name = spec.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise VMwareError("The VM name must be a lowercase hostname of 1–63 characters.")
        for key, maximum in (("cpu", 128), ("ram_gb", 2048), ("disk_gb", 65536)):
            value = spec.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise VMwareError(f"VM {key} must be a positive integer no greater than {maximum}.")
        datastore = spec.get("datastore")
        self._datastore(datastore)
        iso_datastore, iso_relative = _split_datastore_path(iso_path, owner_id)
        _relative_path(iso_relative, owner_id, iso=True)
        if iso_datastore != datastore:
            raise VMwareError("Installation media must be on the VM's selected datastore.")
        networks = [net for net in self._host.network if net.name == spec.get("network")]
        if len(networks) != 1 or isinstance(networks[0], vim.dvs.DistributedVirtualPortgroup):
            raise VMwareError("Choose a standard ESXi port group that exists uniquely on this host.")
        if any(vm.name == name for vm in self._objects(vim.VirtualMachine)):
            raise VMwareError(
                "A VM with this name already exists on ESXi. Choose another name or clean up the prior deployment."
            )
        directory = f"gdeploy/{owner_id}/{name}"
        self._make_directory(datastore, directory)

        scsi = vim.vm.device.ParaVirtualSCSIController(key=1000, busNumber=0, sharedBus="noSharing")
        disk = vim.vm.device.VirtualDisk(
            key=2000,
            controllerKey=scsi.key,
            unitNumber=0,
            capacityInKB=spec["disk_gb"] * 1024**2,
            backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
                fileName=f"[{datastore}] {directory}/{name}.vmdk", diskMode="persistent", thinProvisioned=True
            ),
        )
        sata = vim.vm.device.VirtualAHCIController(key=15000, busNumber=0)
        cdrom = vim.vm.device.VirtualCdrom(
            key=16000,
            controllerKey=sata.key,
            unitNumber=0,
            backing=vim.vm.device.VirtualCdrom.IsoBackingInfo(fileName=iso_path),
            connectable=vim.vm.device.VirtualDevice.ConnectInfo(
                startConnected=True, connected=True, allowGuestControl=False
            ),
        )
        nic = vim.vm.device.VirtualVmxnet3(
            key=4000,
            addressType="generated",
            backing=vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(
                deviceName=networks[0].name, network=networks[0]
            ),
            connectable=vim.vm.device.VirtualDevice.ConnectInfo(
                startConnected=True, connected=True, allowGuestControl=False
            ),
        )
        device_changes = []
        for device in (scsi, disk, sata, cdrom, nic):
            change = vim.vm.device.VirtualDeviceSpec(operation="add", device=device)
            if device is disk:
                change.fileOperation = "create"
            device_changes.append(change)
        config = vim.vm.ConfigSpec(
            name=name,
            annotation=f"GDeploy:{owner_id}",
            guestId="ubuntu64Guest",
            version="vmx-21",
            firmware="efi",
            numCPUs=spec["cpu"],
            memoryMB=spec["ram_gb"] * 1024,
            files=vim.vm.FileInfo(
                vmPathName=f"[{datastore}] {directory}/{name}.vmx",
                logDirectory=f"[{datastore}] {directory}",
                snapshotDirectory=f"[{datastore}] {directory}",
                suspendDirectory=f"[{datastore}] {directory}",
            ),
            deviceChange=device_changes,
            bootOptions=vim.vm.BootOptions(
                bootDelay=1000,
                bootRetryEnabled=True,
                bootRetryDelay=10000,
                efiSecureBootEnabled=False,
                # A blank disk falls through to the installer. After Ubuntu's
                # first reboot it boots the installed disk, avoiding reinstall.
                bootOrder=[
                    vim.vm.BootOptions.BootableDiskDevice(deviceKey=disk.key),
                    vim.vm.BootOptions.BootableCdromDevice(),
                ],
            ),
        )
        vm = self._wait_task(
            self._dc.vmFolder.CreateVM_Task(config=config, pool=self._host.parent.resourcePool, host=self._host),
            "Create virtual machine",
        )
        if vm is None or not getattr(vm, "_moId", None):
            raise VMwareError(
                "ESXi completed VM creation without returning its identifier. Recover the VM using its deployment ownership tag."
            )
        return str(vm._moId)

    def _find_vm(self, vm_id: str) -> Any | None:
        if not isinstance(vm_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", vm_id):
            raise VMwareError("The ESXi VM identifier is invalid.")
        return next((vm for vm in self._objects(vim.VirtualMachine) if vm._moId == vm_id), None)

    @staticmethod
    def _assert_owned(vm: Any, owner_id: str) -> None:
        owner_id = _owner(owner_id)
        if getattr(getattr(vm, "config", None), "annotation", None) != f"GDeploy:{owner_id}":
            raise VMwareError("VM ownership does not match this deployment; refusing the operation.")

    def _owned_vm(self, vm_id: str, owner_id: str) -> Any:
        _owner(owner_id)
        vm = self._find_vm(vm_id)
        if vm is None:
            raise VMwareError("The deployment's VM is no longer present on ESXi.")
        self._assert_owned(vm, owner_id)
        return vm

    @_guarded("Power on virtual machine")
    def power_on(self, vm_id: str, owner_id: str) -> None:
        vm = self._owned_vm(vm_id, owner_id)
        if vm.runtime.powerState != "poweredOn":
            self._wait_task(vm.PowerOnVM_Task(), "Power on virtual machine")

    @_guarded("Read guest IP address")
    def guest_ip(self, vm_id: str) -> str | None:
        vm = self._find_vm(vm_id)
        if vm is None:
            raise VMwareError("The deployment's VM is no longer present on ESXi.")
        guest = vm.guest
        if guest is None:
            return None
        candidates = [getattr(guest, "ipAddress", None)]
        for nic in getattr(guest, "net", None) or []:
            candidates.extend(getattr(nic, "ipAddress", None) or [])
        for candidate in candidates:
            try:
                address = ipaddress.ip_address(candidate)
            except (ValueError, TypeError):
                continue
            if isinstance(address, ipaddress.IPv4Address) and not (
                address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_unspecified
                or address.is_reserved
            ):
                return str(address)
        return None

    @_guarded("Detach installation media")
    def detach_iso(self, vm_id: str, owner_id: str) -> None:
        vm = self._owned_vm(vm_id, owner_id)
        changes = []
        for device in vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualCdrom):
                if isinstance(device.backing, vim.vm.device.VirtualCdrom.IsoBackingInfo):
                    _split_datastore_path(device.backing.fileName, owner_id)
                changes.append(vim.vm.device.VirtualDeviceSpec(operation="remove", device=device))
        if changes:
            self._wait_task(
                vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(deviceChange=changes)), "Detach installation media"
            )

    @staticmethod
    def _assert_owned_storage(vm: Any, owner_id: str) -> None:
        config = vm.config
        _split_datastore_path(config.files.vmPathName, owner_id)
        for attribute in ("logDirectory", "snapshotDirectory", "suspendDirectory", "ftMetadataDirectory"):
            value = getattr(config.files, attribute, None)
            if value:
                _split_datastore_path(value.rstrip("/"), owner_id)
        for device in config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualDisk):
                backing = device.backing
                seen = set()
                while backing is not None:
                    if not isinstance(backing, vim.vm.device.VirtualDisk.FlatVer2BackingInfo):
                        raise VMwareError("The VM has an unsupported disk backing; refusing deletion.")
                    if id(backing) in seen:
                        raise VMwareError("The VM has an unrecognized disk backing chain; refusing deletion.")
                    seen.add(id(backing))
                    _split_datastore_path(getattr(backing, "fileName", None), owner_id)
                    backing = getattr(backing, "parent", None)
        # Snapshot, swap and auxiliary files must also remain within this
        # deployment. A manually attached external disk makes cleanup stop.
        layout = getattr(vm, "layoutEx", None)
        for file in getattr(layout, "file", None) or []:
            _split_datastore_path(file.name, owner_id)

    @_guarded("Delete virtual machine")
    def destroy_vm(self, vm_id: str, owner_id: str) -> None:
        _owner(owner_id)
        vm = self._find_vm(vm_id)
        if vm is None:
            return
        self._assert_owned(vm, owner_id)
        self._assert_owned_storage(vm, owner_id)
        if vm.runtime.powerState != "poweredOff":
            self._wait_task(vm.PowerOffVM_Task(), "Power off virtual machine")
        # Re-read after power-off; reject ownership/storage changes before the
        # irreversible ESXi Destroy_Task call.
        self._assert_owned(vm, owner_id)
        self._assert_owned_storage(vm, owner_id)
        self._wait_task(vm.Destroy_Task(), "Delete virtual machine", missing_ok=True)

    @_guarded("Find deployment virtual machines")
    def find_owned_vms(self, owner_id: str) -> list[dict]:
        owner_id = _owner(owner_id)
        return [
            {"vm_id": str(vm._moId), "name": vm.name}
            for vm in self._objects(vim.VirtualMachine)
            if getattr(getattr(vm, "config", None), "annotation", None) == f"GDeploy:{owner_id}"
        ]

    @_guarded("Delete installation ISO")
    def delete_iso(self, datastore: str, remote_path: str, owner_id: str) -> None:
        _owner(owner_id)
        _relative_path(remote_path, owner_id, iso=True)
        self._datastore(datastore)
        try:
            task = self._content.fileManager.DeleteDatastoreFile_Task(
                name=f"[{datastore}] {remote_path}", datacenter=self._dc
            )
            self._wait_task(task, "Delete installation ISO", missing_ok=True)
        except vim.fault.FileNotFound:
            return
