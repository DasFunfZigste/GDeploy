from __future__ import annotations

import asyncio
import ssl
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
import requests
from pyVmomi import vim, vmodl

from gdeploy import vmware
from gdeploy.vmware import ESXiClient, VMwareError


OWNER = "067a159a-4055-4bfe-b5ed-3b34644b6ebc"
OTHER = "e0e58a72-3430-49bc-b8fd-bec0e1ba710b"
BASE = f"gdeploy/{OWNER}"
ISO = f"{BASE}/ubuntu.iso"


def task(result=None, error=None, state=None):
    return NS(info=NS(state=state or ("error" if error else "success"), result=result, error=error))


class NetworkStub:
    def InvokeAccessor(self, obj, info):
        assert info.name == "name"
        return "VM Network"


@pytest.fixture
def client(monkeypatch):
    result = ESXiClient("esxi.example.test", "root", "secret-password")
    result._si = NS(_stub=NS(cookie='vmware_soap_session="test-session-cookie"; Path=/; HttpOnly; Secure'))
    result._content = NS(fileManager=MagicMock())
    result._host = NS(
        name="esxi.example.test",
        datastore=[
            NS(
                name="datastore1",
                summary=NS(accessible=True, maintenanceMode="normal", freeSpace=100 * 1024**3, capacity=500 * 1024**3),
            )
        ],
        network=[vim.Network("network-1", NetworkStub())],
        hardware=NS(memorySize=32 * 1024**3, cpuInfo=NS(numCpuThreads=16)),
        summary=NS(quickStats=NS(overallMemoryUsage=4096)),
        parent=NS(resourcePool=NS()),
    )
    result._dc = NS(name="ha-datacenter", vmFolder=MagicMock())
    result._http = MagicMock()
    result._http.put.return_value.status_code = 201
    result._test_vms = []
    monkeypatch.setattr(result, "_objects", lambda kind: list(result._test_vms))
    return result


def owned_vm():
    disk = vim.vm.device.VirtualDisk(
        key=2000,
        backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(fileName=f"[datastore1] {BASE}/lab/lab.vmdk"),
    )
    cdrom = vim.vm.device.VirtualCdrom(
        key=16000, backing=vim.vm.device.VirtualCdrom.IsoBackingInfo(fileName=f"[datastore1] {ISO}")
    )
    result = NS(
        _moId="42",
        name="lab",
        config=NS(
            annotation=f"GDeploy:{OWNER}",
            files=NS(vmPathName=f"[datastore1] {BASE}/lab/lab.vmx"),
            hardware=NS(device=[disk, cdrom]),
        ),
        runtime=NS(powerState="poweredOff"),
        layoutEx=NS(file=[NS(name=f"[datastore1] {BASE}/lab/lab.vmdk")]),
        guest=NS(ipAddress=None, net=[]),
        PowerOnVM_Task=MagicMock(return_value=task()),
        PowerOffVM_Task=MagicMock(return_value=task()),
        Destroy_Task=MagicMock(return_value=task()),
        ReconfigVM_Task=MagicMock(return_value=task()),
    )
    return result


@pytest.mark.parametrize(
    "host",
    [
        "http://esxi",
        "https://root:pw@esxi",
        "esxi/sdk",
        "https://esxi?secret=pw",
        "https://esxi#fragment",
        " esxi",
        "esxi\n",
        "esxi:0",
        "esxi:70000",
        "https://",
        "https://esxi\\evil",
        "https://esxi/%2e%2e",
        None,
    ],
)
def test_rejects_unsafe_host_targets(host):
    with pytest.raises(VMwareError, match="hostname or IP"):
        ESXiClient(host, "root", "password")


@pytest.mark.parametrize(
    "host, expected",
    [
        ("esxi", ("esxi", 443)),
        ("https://esxi:8443/", ("esxi", 8443)),
        ("192.168.1.2", ("192.168.1.2", 443)),
        ("[2001:db8::1]:8443", ("2001:db8::1", 8443)),
        ("2001:db8::1", ("2001:db8::1", 443)),
        ("2001:0db8:0000:0000:0000:0000:0000:0001", ("2001:db8::1", 443)),
    ],
)
def test_accepts_explicit_https_host_targets(host, expected):
    instance = ESXiClient(host, "root", "password")
    assert (instance.host, instance.port) == expected


def test_bare_ipv6_host_uses_brackets_in_upload_origin():
    instance = ESXiClient("2001:db8::1", "root", "password")
    assert instance._origin == "https://[2001:db8::1]:443"


def test_connect_uses_certificate_verification_and_finite_socket_timeout(monkeypatch):
    instance = ESXiClient("esxi", "root", "password")
    host = NS(runtime=NS(connectionState="connected", inMaintenanceMode=False))
    dc = NS()
    content = NS(about=NS(apiType="HostAgent"))
    si = MagicMock()
    si.RetrieveContent.return_value = content
    connect = MagicMock(return_value=si)
    disconnect = MagicMock()
    monkeypatch.setattr(vmware, "SmartConnect", connect)
    monkeypatch.setattr(vmware, "Disconnect", disconnect)
    monkeypatch.setattr(instance, "_objects", lambda kind: [host] if kind is vim.HostSystem else [dc])
    with instance:
        kwargs = connect.call_args.kwargs
        assert kwargs["sslContext"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["sslContext"].check_hostname is True
        assert 0 < kwargs["httpConnectionTimeout"] <= 60
        assert instance._http.trust_env is False
    disconnect.assert_called_once_with(si)
    assert instance._si is None


def test_vcenter_rejected_and_session_closed(monkeypatch):
    instance = ESXiClient("esxi", "root", "password")
    si = MagicMock()
    si.RetrieveContent.return_value.about.apiType = "VirtualCenter"
    disconnect = MagicMock()
    monkeypatch.setattr(vmware, "SmartConnect", MagicMock(return_value=si))
    monkeypatch.setattr(vmware, "Disconnect", disconnect)
    with pytest.raises(VMwareError, match="standalone"):
        instance.connect()
    disconnect.assert_called_once_with(si)
    assert instance._si is None


def test_async_event_loop_cannot_run_blocking_calls(client):
    async def invoke():
        with pytest.raises(VMwareError, match="worker thread"):
            client.inventory()
        with pytest.raises(VMwareError, match="worker thread"):
            client.connect()

    asyncio.run(invoke())


def test_inventory_exposes_free_memory_and_accessible_datastores(client):
    client._test_vms.append(owned_vm())
    client._host.datastore.append(NS(name="offline", summary=NS(accessible=False)))
    result = client.inventory()
    assert result["host"]["free_memory_gb"] == 28
    assert result["host"]["cpu_threads"] == 16
    assert result["datastores"] == [{"name": "datastore1", "free_gb": 100, "capacity_gb": 500}]
    assert result["networks"] == [{"name": "VM Network"}]
    assert result["vms"] == [{"name": "lab"}]


@pytest.mark.parametrize(
    "path",
    [
        "outside.iso",
        f"/gdeploy/{OWNER}/test.iso",
        f"gdeploy/{OWNER}/../test.iso",
        f"gdeploy/{OWNER}//test.iso",
        f"gdeploy/{OWNER}/%2e%2e/test.iso",
        f"gdeploy/{OWNER}/a\\test.iso",
        f"gdeploy/{OWNER}/test.iso?x",
        f"gdeploy/{OWNER}/test.vmdk",
        "gdeploy/not-a-uuid/test.iso",
        f"gdeploy/{OWNER.upper()}/test.iso",
    ],
)
def test_upload_rejects_paths_before_writing(client, tmp_path, path):
    source = tmp_path / "source.iso"
    source.write_bytes(b"media")
    with pytest.raises(VMwareError):
        client.upload_iso("datastore1", path, source)
    client._http.put.assert_not_called()
    client._content.fileManager.MakeDirectory.assert_not_called()


def test_upload_stays_on_authenticated_https_origin(client, tmp_path):
    source = tmp_path / "source.iso"
    source.write_bytes(b"media")
    client.upload_iso("datastore1", ISO, source)
    args, kwargs = client._http.put.call_args
    assert args == (f"https://esxi.example.test:443/folder/{ISO}",)
    assert kwargs["params"] == {"dcPath": "ha-datacenter", "dsName": "datastore1"}
    assert kwargs["headers"]["Cookie"] == 'vmware_soap_session="test-session-cookie"'
    assert "secret-password" not in str(kwargs["headers"])
    assert kwargs["verify"] is True
    assert kwargs["allow_redirects"] is False
    assert all(0 < value <= 120 for value in kwargs["timeout"])
    assert kwargs["data"].closed
    client._http.put.return_value.close.assert_called_once()
    client._content.fileManager.MakeDirectory.assert_called_once_with(
        name=f"[datastore1] {BASE}", datacenter=client._dc, createParentDirectories=True
    )


def test_upload_refuses_redirect_without_reading_response_body(client, tmp_path):
    source = tmp_path / "source.iso"
    source.write_bytes(b"media")
    client._http.put.return_value.status_code = 307
    client._http.put.return_value.text = "secret-password"
    with pytest.raises(VMwareError, match="redirect") as error:
        client.upload_iso("datastore1", ISO, source)
    assert "secret-password" not in str(error.value)


def test_upload_hides_request_errors_and_response_payload(client, tmp_path):
    source = tmp_path / "source.iso"
    source.write_bytes(b"media")
    client._http.put.side_effect = requests.ConnectionError("secret-password; test-session-cookie")
    with pytest.raises(VMwareError, match="HTTPS connection") as error:
        client.upload_iso("datastore1", ISO, source)
    assert "secret-password" not in str(error.value)
    assert "test-session-cookie" not in str(error.value)


def spec():
    return {
        "name": "lab-ubuntu",
        "cpu": 2,
        "ram_gb": 4,
        "disk_gb": 40,
        "datastore": "datastore1",
        "network": "VM Network",
    }


def test_create_builds_valid_vmware_objects_with_scoped_storage_and_disk_first_boot(client):
    client._dc.vmFolder.CreateVM_Task.return_value = task(NS(_moId="123"))
    assert client.create_vm(spec(), f"[datastore1] {ISO}", OWNER) == "123"
    config = client._dc.vmFolder.CreateVM_Task.call_args.kwargs["config"]
    assert isinstance(config, vim.vm.ConfigSpec)
    assert config.annotation == f"GDeploy:{OWNER}"
    assert config.firmware == "efi"
    assert config.guestId == "ubuntu64Guest"
    assert config.memoryMB == 4096
    assert config.numCPUs == 2
    assert config.files.vmPathName == f"[datastore1] {BASE}/lab-ubuntu/lab-ubuntu.vmx"
    assert isinstance(config.bootOptions.bootOrder[0], vim.vm.BootOptions.BootableDiskDevice)
    assert isinstance(config.bootOptions.bootOrder[1], vim.vm.BootOptions.BootableCdromDevice)
    disks = [change for change in config.deviceChange if isinstance(change.device, vim.vm.device.VirtualDisk)]
    assert len(disks) == 1
    assert disks[0].fileOperation == "create"
    assert disks[0].device.capacityInKB == 40 * 1024**2
    assert disks[0].device.backing.thinProvisioned is True
    assert f"{BASE}/lab-ubuntu/" in disks[0].device.backing.fileName
    assert any(isinstance(change.device, vim.vm.device.VirtualVmxnet3) for change in config.deviceChange)


def test_create_supports_api_memory_limit_in_real_sdk_config(client):
    client._dc.vmFolder.CreateVM_Task.return_value = task(NS(_moId="123"))
    client.create_vm(dict(spec(), ram_gb=2048), f"[datastore1] {ISO}", OWNER)
    config = client._dc.vmFolder.CreateVM_Task.call_args.kwargs["config"]
    assert config.memoryMB == 2048 * 1024


@pytest.mark.parametrize(
    "path",
    [
        f"[datastore1] gdeploy/{OTHER}/test.iso",
        "[datastore1] unowned.iso",
        f"[another-store] {ISO}",
        f"[datastore1] {BASE}/disk.vmdk",
    ],
)
def test_create_rejects_unowned_or_mismatched_installation_media(client, path):
    with pytest.raises(VMwareError):
        client.create_vm(spec(), path, OWNER)
    client._dc.vmFolder.CreateVM_Task.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "../existing"),
        ("name", "Uppercase"),
        ("cpu", True),
        ("cpu", 0),
        ("ram_gb", 1.5),
        ("disk_gb", -1),
        ("network", "missing"),
        ("datastore", "missing"),
    ],
)
def test_create_rejects_invalid_specs(client, field, value):
    invalid = spec()
    invalid[field] = value
    with pytest.raises(VMwareError):
        client.create_vm(invalid, f"[datastore1] {ISO}", OWNER)
    client._dc.vmFolder.CreateVM_Task.assert_not_called()


def test_create_does_not_overwrite_vm_with_same_name(client):
    vm = owned_vm()
    vm.name = spec()["name"]
    client._test_vms.append(vm)
    with pytest.raises(VMwareError, match="already exists"):
        client.create_vm(spec(), f"[datastore1] {ISO}", OWNER)
    client._dc.vmFolder.CreateVM_Task.assert_not_called()


@pytest.mark.parametrize("method", ["destroy_vm", "power_on", "detach_iso"])
@pytest.mark.parametrize("annotation", [None, "", "GDeploy", f"GDeploy:{OTHER}", f"GDeploy:{OWNER}:extra"])
def test_vm_mutations_require_exact_ownership(client, method, annotation):
    vm = owned_vm()
    vm.config.annotation = annotation
    client._test_vms.append(vm)
    with pytest.raises(VMwareError, match="ownership"):
        getattr(client, method)(vm._moId, OWNER)
    vm.Destroy_Task.assert_not_called()
    vm.PowerOnVM_Task.assert_not_called()
    vm.PowerOffVM_Task.assert_not_called()
    vm.ReconfigVM_Task.assert_not_called()


def test_destroy_deletes_only_owned_vm_after_poweroff(client):
    vm = owned_vm()
    vm.runtime.powerState = "poweredOn"
    client._test_vms.append(vm)
    calls = MagicMock()
    calls.attach_mock(vm.PowerOffVM_Task, "power_off")
    calls.attach_mock(vm.Destroy_Task, "destroy")
    client.destroy_vm(vm._moId, OWNER)
    assert [call[0] for call in calls.mock_calls] == ["power_off", "destroy"]


@pytest.mark.parametrize("target", ["vmx", "disk", "disk_parent", "snapshot_directory", "layout"])
def test_destroy_refuses_any_storage_outside_owned_namespace(client, target):
    vm = owned_vm()
    outside = "[datastore1] someone-elses-vm/data.vmdk"
    if target == "vmx":
        vm.config.files.vmPathName = outside
    elif target == "disk":
        vm.config.hardware.device[0].backing.fileName = outside
    elif target == "disk_parent":
        vm.config.hardware.device[0].backing.parent = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(fileName=outside)
    elif target == "snapshot_directory":
        vm.config.files.snapshotDirectory = "[datastore1] someone-elses-vm"
    else:
        vm.layoutEx.file.append(NS(name=outside))
    client._test_vms.append(vm)
    with pytest.raises(VMwareError):
        client.destroy_vm(vm._moId, OWNER)
    vm.Destroy_Task.assert_not_called()
    vm.PowerOffVM_Task.assert_not_called()


def test_destroy_rechecks_ownership_after_poweroff(client):
    vm = owned_vm()
    vm.runtime.powerState = "poweredOn"

    def change_ownership():
        vm.config.annotation = f"GDeploy:{OTHER}"
        return task()

    vm.PowerOffVM_Task.side_effect = change_ownership
    client._test_vms.append(vm)
    with pytest.raises(VMwareError, match="ownership"):
        client.destroy_vm(vm._moId, OWNER)
    vm.Destroy_Task.assert_not_called()


def test_missing_vm_destroy_is_idempotent(client):
    client.destroy_vm("123", OWNER)


def test_find_owned_vms_matches_exact_annotations(client):
    ours, theirs, undecorated = owned_vm(), owned_vm(), owned_vm()
    theirs.config.annotation = f"GDeploy:{OTHER}"
    undecorated.config.annotation = ""
    client._test_vms.extend([ours, theirs, undecorated])
    assert client.find_owned_vms(OWNER) == [{"vm_id": "42", "name": "lab"}]


def test_detach_removes_installation_cd_without_deleting_its_backing(client):
    vm = owned_vm()
    client._test_vms.append(vm)
    client.detach_iso(vm._moId, OWNER)
    changes = vm.ReconfigVM_Task.call_args.kwargs["spec"].deviceChange
    assert len(changes) == 1
    assert changes[0].operation == "remove"
    assert changes[0].fileOperation is None
    client._content.fileManager.DeleteDatastoreFile_Task.assert_not_called()


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "169.254.2.3", "0.0.0.0", "224.0.0.1", "255.255.255.255", "::1", "2001:db8::1", "bad", None],
)
def test_guest_ip_rejects_nonroutable_and_nonipv4_addresses(client, address):
    vm = owned_vm()
    vm.guest.ipAddress = address
    client._test_vms.append(vm)
    assert client.guest_ip(vm._moId) is None


def test_guest_ip_finds_private_ipv4_on_tools_nic(client):
    vm = owned_vm()
    vm.guest.ipAddress = "fe80::1"
    vm.guest.net = [NS(ipAddress=["169.254.1.2", "192.168.1.45"])]
    client._test_vms.append(vm)
    assert client.guest_ip(vm._moId) == "192.168.1.45"


@pytest.mark.parametrize(
    "path",
    [
        f"gdeploy/{OTHER}/test.iso",
        f"gdeploy/{OWNER}/../other.iso",
        f"gdeploy/{OWNER}/test.vmdk",
        f"gdeploy/{OWNER}/",
        f"gdeploy/{OWNER}",
        f"gdeploy/{OWNER}/%2e%2e/test.iso",
    ],
)
def test_delete_iso_rejects_unowned_or_unsafe_paths(client, path):
    with pytest.raises(VMwareError):
        client.delete_iso("datastore1", path, OWNER)
    client._content.fileManager.DeleteDatastoreFile_Task.assert_not_called()


@pytest.mark.parametrize("fault", [None, vim.fault.FileNotFound(msg="already removed")])
def test_delete_iso_waits_for_owned_file_deletion_and_is_idempotent(client, fault):
    client._content.fileManager.DeleteDatastoreFile_Task.return_value = task(error=fault)
    client.delete_iso("datastore1", ISO, OWNER)
    client._content.fileManager.DeleteDatastoreFile_Task.assert_called_once_with(
        name=f"[datastore1] {ISO}", datacenter=client._dc
    )


def test_task_errors_are_sanitized_and_preserve_fault_type(client):
    fault = vim.fault.NoPermission(msg="permission rejected secret-password test-session-cookie")
    with pytest.raises(VMwareError, match="NoPermission") as error:
        client._wait_task(task(error=fault), "Create VM")
    assert "secret-password" not in str(error.value)
    assert "test-session-cookie" not in str(error.value)


def test_task_timeout_is_finite_and_explains_possible_orphans(client, monkeypatch):
    monkeypatch.setattr(vmware.time, "monotonic", MagicMock(side_effect=[100, 100 + client.TASK_TIMEOUT]))
    sleep = MagicMock()
    monkeypatch.setattr(vmware.time, "sleep", sleep)
    with pytest.raises(VMwareError, match="still be processing"):
        client._wait_task(task(state="running"), "Create VM")
    sleep.assert_not_called()


def test_poweroff_failure_prevents_destroy(client):
    vm = owned_vm()
    vm.runtime.powerState = "poweredOn"
    vm.PowerOffVM_Task.return_value = task(error=vim.fault.NoPermission(msg="PowerOff privilege required"))
    client._test_vms.append(vm)
    with pytest.raises(VMwareError, match="NoPermission"):
        client.destroy_vm(vm._moId, OWNER)
    vm.Destroy_Task.assert_not_called()


def test_missing_object_task_fault_is_only_ignored_when_explicit(client):
    failure = task(error=vmodl.fault.ManagedObjectNotFound(msg="missing VM"))
    with pytest.raises(VMwareError, match="ManagedObjectNotFound"):
        client._wait_task(failure, "Update VM")
    assert client._wait_task(failure, "Destroy VM", missing_ok=True) is None
