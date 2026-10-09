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


def validate_boot_devices(boot_options, devices):
    """Model ESXi rejecting boot targets absent from the resulting VM hardware."""
    for entry in boot_options.bootOrder:
        if isinstance(entry, vim.vm.BootOptions.BootableDiskDevice):
            present = sum(isinstance(device, vim.vm.device.VirtualDisk) and device.key == entry.deviceKey for device in devices) == 1
        elif isinstance(entry, vim.vm.BootOptions.BootableCdromDevice):
            present = any(isinstance(device, vim.vm.device.VirtualCdrom) for device in devices)
        else:
            raise AssertionError("Unexpected boot-device type in test VM")
        if not present:
            raise vmodl.fault.InvalidArgument(
                invalidProperty="configSpec.bootOptions.bootOrder",
                msg="configSpec.bootOptions.bootOrder references a device missing from the VM.",
            )


class NetworkStub:
    def __init__(self, name="VM Network"):
        self.name = name

    def InvokeAccessor(self, obj, info):
        assert info.name == "name"
        return self.name


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


def sensor_spec():
    return dict(spec(), role="corelight_sensor", cpu=4, ram_gb=16, disk_gb=600, monitor_network="VM Network")


def test_sensor_vm_creates_two_manual_adapters_before_media_and_reserves_cpu_memory(client, monkeypatch):
    client._host.hardware.cpuInfo.numCpuCores = 8
    client._host.hardware.cpuInfo.hz = 2_700_000_000
    random = MagicMock(side_effect=[0, (1 << 22) - 1])
    monkeypatch.setattr(vmware.secrets, "randbelow", random)
    client._dc.vmFolder.CreateVM_Task.return_value = task(NS(_moId="sensor-1"))
    assert client.create_vm(sensor_spec(), None, OWNER) == "sensor-1"
    config = client._dc.vmFolder.CreateVM_Task.call_args.kwargs["config"]
    devices = [change.device for change in config.deviceChange]
    adapters = [device for device in devices if isinstance(device, vim.vm.device.VirtualEthernetCard)]
    assert [device.key for device in adapters] == [4000, 4001]
    assert all(device.addressType == "manual" for device in adapters)
    assert [device.macAddress for device in adapters] == ["00:50:56:00:00:00", "00:50:56:3f:ff:ff"]
    assert all(call.args == (1 << 22,) for call in random.call_args_list)
    assert not any(isinstance(device, vim.vm.device.VirtualCdrom) for device in devices)
    assert len(config.bootOptions.bootOrder) == 1
    assert isinstance(config.bootOptions.bootOrder[0], vim.vm.BootOptions.BootableDiskDevice)
    validate_boot_devices(config.bootOptions, devices)
    assert any(isinstance(device, vim.vm.device.VirtualAHCIController) for device in devices)
    assert config.cpuAllocation.reservation == 4 * 2700
    assert config.memoryAllocation.reservation == 16 * 1024
    assert config.memoryReservationLockedToMax is True


@pytest.mark.parametrize("monitor_network", ["VM Network", "Capture"])
def test_sensor_assigns_distinct_macs_by_adapter_role_even_on_same_portgroup(client, monkeypatch, monitor_network):
    client._host.hardware.cpuInfo.numCpuCores = 8
    client._host.hardware.cpuInfo.hz = 2_700_000_000
    client._host.network.append(vim.Network("network-2", NetworkStub("Capture")))
    monkeypatch.setattr(vmware.secrets, "randbelow", MagicMock(side_effect=[3, 4]))
    client._dc.vmFolder.CreateVM_Task.return_value = task(NS(_moId="sensor-1"))
    client.create_vm(dict(sensor_spec(), monitor_network=monitor_network), None, OWNER)
    config = client._dc.vmFolder.CreateVM_Task.call_args.kwargs["config"]
    adapters = [change.device for change in config.deviceChange if isinstance(change.device, vim.vm.device.VirtualEthernetCard)]
    assert [(device.key, device.macAddress, device.backing.deviceName) for device in adapters] == [
        (4000, "00:50:56:00:00:03", "VM Network"),
        (4001, "00:50:56:00:00:04", monitor_network),
    ]


@pytest.mark.parametrize("power_state", ["poweredOn", "poweredOff", "suspended"])
def test_sensor_mac_allocation_skips_registered_and_previously_selected_addresses(client, monkeypatch, power_state):
    client._host.hardware.cpuInfo.numCpuCores = 8
    client._host.hardware.cpuInfo.hz = 2_700_000_000
    existing = owned_vm()
    existing.runtime.powerState = power_state
    existing.config.hardware.device += [
        vim.vm.device.VirtualE1000(key=4000, macAddress="00:50:56:00:00:AB"),
        vim.vm.device.VirtualVmxnet3(key=4001, addressType="generated"),
    ]
    client._test_vms.append(existing)
    random = MagicMock(side_effect=[0xab, 1, 1, 2])
    monkeypatch.setattr(vmware.secrets, "randbelow", random)
    client._dc.vmFolder.CreateVM_Task.return_value = task(NS(_moId="sensor-1"))
    client.create_vm(sensor_spec(), None, OWNER)
    config = client._dc.vmFolder.CreateVM_Task.call_args.kwargs["config"]
    adapters = [change.device for change in config.deviceChange if isinstance(change.device, vim.vm.device.VirtualEthernetCard)]
    assert [device.macAddress for device in adapters] == ["00:50:56:00:00:01", "00:50:56:00:00:02"]
    assert random.call_count == 4


def test_sensor_mac_allocation_fails_after_bounded_repeated_collisions_before_creation(client, monkeypatch):
    client._host.hardware.cpuInfo.numCpuCores = 8
    client._host.hardware.cpuInfo.hz = 2_700_000_000
    random = MagicMock(return_value=1)
    monkeypatch.setattr(vmware.secrets, "randbelow", random)
    with pytest.raises(VMwareError, match="Could not allocate two unused sensor MAC addresses"):
        client.create_vm(sensor_spec(), None, OWNER)
    assert random.call_count == client.SENSOR_MAC_ALLOCATION_ATTEMPTS
    client._dc.vmFolder.CreateVM_Task.assert_not_called()
    client._content.fileManager.MakeDirectory.assert_not_called()


@pytest.mark.parametrize("problem", ["missing-config", "missing-hardware", "missing-devices", "denied", "invalid-mac"])
def test_sensor_mac_allocation_requires_readable_inventory_before_creation(client, problem):
    client._host.hardware.cpuInfo.numCpuCores = 8
    client._host.hardware.cpuInfo.hz = 2_700_000_000
    existing = owned_vm()
    if problem == "missing-config":
        existing.config = None
    elif problem == "missing-hardware":
        existing.config.hardware = None
    elif problem == "missing-devices":
        existing.config.hardware.device = None
    elif problem == "denied":
        class InaccessibleVM:
            name = "other-vm"

            @property
            def config(self):
                raise vim.fault.NoPermission(msg="Cannot access secret-password test-session-cookie")

        existing = InaccessibleVM()
    else:
        existing.config.hardware.device.append(vim.vm.device.VirtualVmxnet3(key=4000, macAddress="invalid-sensitive-data"))
    client._test_vms.append(existing)
    with pytest.raises(VMwareError, match="inspect|invalid registered VM MAC address") as error:
        client.create_vm(sensor_spec(), None, OWNER)
    assert "secret-password" not in str(error.value)
    assert "test-session-cookie" not in str(error.value)
    assert "invalid-sensitive-data" not in str(error.value)
    client._dc.vmFolder.CreateVM_Task.assert_not_called()
    client._content.fileManager.MakeDirectory.assert_not_called()


@pytest.mark.parametrize("change", [{"cpu": 2}, {"ram_gb": 8}, {"disk_gb": 540}, {"monitor_network": "missing"}, {"cpu": 16}])
def test_sensor_vm_rejects_insufficient_dedicated_specs_before_creation(client, change):
    client._host.hardware.cpuInfo.numCpuCores = 8
    client._host.hardware.cpuInfo.hz = 2_700_000_000
    with pytest.raises(VMwareError):
        client.create_vm(dict(sensor_spec(), **change), None, OWNER)
    client._dc.vmFolder.CreateVM_Task.assert_not_called()


def test_sensor_requires_physical_cpu_frequency_metadata_for_reservation(client):
    with pytest.raises(VMwareError, match="physical CPU capacity"):
        client.create_vm(sensor_spec(), None, OWNER)
    client._dc.vmFolder.CreateVM_Task.assert_not_called()


def test_ordinary_vm_still_requires_iso(client):
    with pytest.raises(VMwareError, match="media is required"):
        client.create_vm(spec(), None, OWNER)


def test_inventory_reports_physical_cores_frequency_and_free_reservation_capacity(client):
    client._host.hardware.cpuInfo.numCpuCores = 8
    client._host.hardware.cpuInfo.hz = 2_700_000_000
    client._host.parent.resourcePool.runtime = NS(cpu=NS(unreservedForVm=10800), memory=NS(unreservedForVm=20 * 1024**3))
    host = client.inventory()["host"]
    assert host["cpu_cores"] == 8
    assert host["cpu_mhz"] == 2700
    assert host["free_cpu_reservation_mhz"] == 10800
    assert host["free_memory_reservation_gb"] == 20


def sensor_vm():
    vm = owned_vm()
    vm.config.hardware.device[0].controllerKey = 1000
    vm.config.hardware.device[0].unitNumber = 0
    vm.config.hardware.device = [
        vm.config.hardware.device[0],
        vim.vm.device.VirtualAHCIController(key=15000, busNumber=0),
        vim.vm.device.ParaVirtualSCSIController(key=1000, busNumber=0, sharedBus="noSharing"),
        vim.vm.device.VirtualVmxnet3(key=4000, addressType="generated", macAddress="00:50:56:aa:bb:01"),
        vim.vm.device.VirtualVmxnet3(key=4001, addressType="generated", macAddress="00:50:56:aa:bb:02"),
    ]
    return vm


def test_sensor_network_macs_use_original_device_keys_not_inventory_order(client):
    vm = sensor_vm()
    vm.config.hardware.device.reverse()
    client._test_vms.append(vm)
    assert client.network_macs(vm._moId, OWNER) == {"management_mac": "00:50:56:aa:bb:01", "monitor_mac": "00:50:56:aa:bb:02"}


@pytest.mark.parametrize("failure", ["other-owner", "missing", "extra", "invalid", "multicast", "zero", "duplicate", "wrong-key", "powered-on", "wrong-hardware"])
def test_sensor_macs_reject_changed_ownership_or_hardware(client, failure):
    vm = sensor_vm()
    if failure == "other-owner":
        vm.config.annotation = f"GDeploy:{OTHER}"
    if failure == "missing":
        vm.config.hardware.device.pop()
    if failure == "extra":
        vm.config.hardware.device.append(vim.vm.device.VirtualVmxnet3(key=4002, macAddress="00:50:56:aa:bb:03"))
    if failure in {"invalid", "multicast", "zero", "duplicate"}:
        vm.config.hardware.device[-1].macAddress = {"invalid": "bad", "multicast": "01:50:56:aa:bb:02", "zero": "00:00:00:00:00:00", "duplicate": "00:50:56:aa:bb:01"}[failure]
    if failure == "wrong-key":
        vm.config.hardware.device[-1].key = 4002
    if failure == "powered-on":
        vm.runtime.powerState = "poweredOn"
    if failure == "wrong-hardware":
        vm.config.hardware.device[-1] = vim.vm.device.VirtualE1000(key=4001, macAddress="00:50:56:aa:bb:02")
    client._test_vms.append(vm)
    with pytest.raises(VMwareError):
        client.network_macs(vm._moId, OWNER)


@pytest.mark.parametrize("key, role", [(4000, "management"), (4001, "monitoring")])
@pytest.mark.parametrize("mac, description", [(None, "no"), ("", "no"), ("invalid-sensitive-data", "an invalid")])
def test_sensor_mac_readback_errors_identify_adapter_without_echoing_invalid_value(client, key, role, mac, description):
    vm = sensor_vm()
    adapter = next(device for device in vm.config.hardware.device if device.key == key)
    adapter.macAddress = mac
    client._test_vms.append(vm)
    with pytest.raises(VMwareError) as error:
        client.network_macs(vm._moId, OWNER)
    assert f"ESXi returned {description} MAC address for the sensor {role} adapter (device key {key})" in str(error.value)
    assert "invalid-sensitive-data" not in str(error.value)
    vm.PowerOnVM_Task.assert_not_called()
    vm.ReconfigVM_Task.assert_not_called()


@pytest.mark.parametrize("role,key", [("management", 4000), ("monitoring", 4001)])
def test_sensor_mac_readback_rejects_new_conflict_with_another_registered_vm(client, role, key):
    sensor = sensor_vm()
    existing = owned_vm()
    existing._moId = "other-vm"
    sensor_mac = next(device.macAddress for device in sensor.config.hardware.device if device.key == key)
    existing.config.hardware.device.append(vim.vm.device.VirtualVmxnet3(key=4000, macAddress=sensor_mac.upper()))
    client._test_vms.extend([existing, sensor])
    with pytest.raises(VMwareError, match=f"sensor {role} adapter MAC address is already assigned"):
        client.network_macs(sensor._moId, OWNER)
    sensor.PowerOnVM_Task.assert_not_called()
    sensor.ReconfigVM_Task.assert_not_called()


def test_sensor_mac_readback_requires_other_vm_inventory_to_remain_readable(client):
    sensor = sensor_vm()
    client._test_vms.extend([sensor, NS(_moId="other-vm", config=None)])
    with pytest.raises(VMwareError, match="inspect all registered VM network adapters"):
        client.network_macs(sensor._moId, OWNER)
    sensor.PowerOnVM_Task.assert_not_called()


def test_attach_iso_adds_only_owned_media_to_powered_off_sensor(client):
    vm = sensor_vm()
    client._test_vms.append(vm)
    client.attach_iso(vm._moId, f"[datastore1] {ISO}", OWNER)
    configuration = vm.ReconfigVM_Task.call_args.kwargs["spec"]
    changes = configuration.deviceChange
    assert len(changes) == 1 and changes[0].operation == "add"
    assert changes[0].device.backing.fileName == f"[datastore1] {ISO}"
    assert changes[0].device.controllerKey == 15000
    assert changes[0].device.connectable.startConnected is True
    assert len(configuration.bootOptions.bootOrder) == 2
    assert isinstance(configuration.bootOptions.bootOrder[0], vim.vm.BootOptions.BootableDiskDevice)
    assert configuration.bootOptions.bootOrder[0].deviceKey == vm.config.hardware.device[0].key
    assert isinstance(configuration.bootOptions.bootOrder[1], vim.vm.BootOptions.BootableCdromDevice)
    validate_boot_devices(configuration.bootOptions, vm.config.hardware.device + [changes[0].device])


@pytest.mark.parametrize("failure", ["other-owner", "external-iso", "external-vm", "other-datastore", "powered-on", "cdrom", "no-sata", "not-iso"])
def test_attach_iso_rejects_unowned_media_or_mutated_vm_without_reconfigure(client, failure):
    vm = sensor_vm()
    path = f"[datastore1] {ISO}"
    if failure == "other-owner":
        vm.config.annotation = f"GDeploy:{OTHER}"
    if failure == "external-iso":
        path = f"[datastore1] gdeploy/{OTHER}/sensor.iso"
    if failure == "external-vm":
        vm.config.files.vmPathName = "[datastore1] unmanaged/vm.vmx"
    if failure == "other-datastore":
        path = f"[other-store] {ISO}"
    if failure == "powered-on":
        vm.runtime.powerState = "poweredOn"
    if failure == "cdrom":
        vm.config.hardware.device.append(vim.vm.device.VirtualCdrom(key=16000))
    if failure == "no-sata":
        vm.config.hardware.device.pop(1)
    if failure == "not-iso":
        path = f"[datastore1] {BASE}/something.vmdk"
    client._test_vms.append(vm)
    with pytest.raises(VMwareError):
        client.attach_iso(vm._moId, path, OWNER)
    vm.ReconfigVM_Task.assert_not_called()


@pytest.mark.parametrize("role", ["ubuntu", "splunk", "elasticsearch", "kibana", "fleetmanager", "corelight_sensor"])
def test_creation_with_media_keeps_existing_disk_then_cd_boot_order(client, role):
    client._host.hardware.cpuInfo.numCpuCores = 8
    client._host.hardware.cpuInfo.hz = 2_700_000_000
    specification = sensor_spec() if role == "corelight_sensor" else dict(spec(), role=role)

    def create(config, **kwargs):
        devices = [change.device for change in config.deviceChange]
        adapters = [device for device in devices if isinstance(device, vim.vm.device.VirtualEthernetCard)]
        if role == "corelight_sensor":
            assert len(adapters) == 2
            assert all(device.addressType == "manual" and device.macAddress for device in adapters)
        else:
            assert len(adapters) == 1
            assert adapters[0].addressType == "generated" and adapters[0].macAddress is None
        validate_boot_devices(config.bootOptions, devices)
        assert len(config.bootOptions.bootOrder) == 2
        assert isinstance(config.bootOptions.bootOrder[0], vim.vm.BootOptions.BootableDiskDevice)
        assert isinstance(config.bootOptions.bootOrder[1], vim.vm.BootOptions.BootableCdromDevice)
        return task(NS(_moId="created-with-media"))

    client._dc.vmFolder.CreateVM_Task.side_effect = create
    assert client.create_vm(specification, f"[datastore1] {ISO}", OWNER) == "created-with-media"


def test_sensor_creates_disk_only_then_attaches_media_and_valid_boot_order_before_power_on(client, monkeypatch):
    client._host.hardware.cpuInfo.numCpuCores = 8
    client._host.hardware.cpuInfo.hz = 2_700_000_000
    monkeypatch.setattr(vmware.secrets, "randbelow", MagicMock(side_effect=[1, 2]))
    events = []
    vm = owned_vm()

    def create(config, **kwargs):
        devices = [change.device for change in config.deviceChange]
        validate_boot_devices(config.bootOptions, devices)
        assert len(config.bootOptions.bootOrder) == 1
        assert not any(isinstance(device, vim.vm.device.VirtualCdrom) for device in devices)
        vm.config = NS(annotation=config.annotation, files=config.files, hardware=NS(device=devices), bootOptions=config.bootOptions)
        for device in devices:
            if isinstance(device, vim.vm.device.VirtualEthernetCard):
                # Standalone ESXi leaves generated MACs unset until first
                # power-on. Honor explicitly configured manual MACs only.
                if device.addressType == "generated":
                    device.macAddress = None
                else:
                    assert device.addressType == "manual" and device.macAddress
        # Managed-device keys are read from the created VM on the later call.
        # A fixed key in attach_iso would configure an absent boot device here.
        disk = next(device for device in devices if isinstance(device, vim.vm.device.VirtualDisk))
        disk.key = 2007
        vm.config.bootOptions.bootOrder[0].deviceKey = disk.key
        client._test_vms.append(vm)
        events.append("create-powered-off-disk-only")
        return task(vm)

    def reconfigure(spec):
        assert vm.runtime.powerState == "poweredOff"
        assert len(spec.deviceChange) == 1 and spec.deviceChange[0].operation == "add"
        cdrom = spec.deviceChange[0].device
        assert isinstance(cdrom, vim.vm.device.VirtualCdrom)
        assert cdrom.backing.fileName == f"[datastore1] {ISO}"
        devices = vm.config.hardware.device + [cdrom]
        options = spec.bootOptions or vm.config.bootOptions
        validate_boot_devices(options, devices)
        assert len(options.bootOrder) == 2
        assert isinstance(options.bootOrder[0], vim.vm.BootOptions.BootableDiskDevice)
        assert options.bootOrder[0].deviceKey == 2007
        assert isinstance(options.bootOrder[1], vim.vm.BootOptions.BootableCdromDevice)
        vm.config.hardware.device = devices
        vm.config.bootOptions.bootOrder = options.bootOrder
        events.append("attach-iso-and-disk-cd-order")
        return task()

    def power_on():
        validate_boot_devices(vm.config.bootOptions, vm.config.hardware.device)
        assert len(vm.config.bootOptions.bootOrder) == 2
        assert events[-1] == "attach-iso-and-disk-cd-order"
        vm.runtime.powerState = "poweredOn"
        events.append("power-on")
        return task()

    client._dc.vmFolder.CreateVM_Task.side_effect = create
    vm.ReconfigVM_Task.side_effect = reconfigure
    vm.PowerOnVM_Task.side_effect = power_on
    vm_id = client.create_vm(sensor_spec(), None, OWNER)
    assert client.network_macs(vm_id, OWNER) == {"management_mac": "00:50:56:00:00:01", "monitor_mac": "00:50:56:00:00:02"}
    vm.PowerOnVM_Task.assert_not_called()
    client.attach_iso(vm_id, f"[datastore1] {ISO}", OWNER)
    vm.PowerOnVM_Task.assert_not_called()
    client.power_on(vm_id, OWNER)
    assert events == ["create-powered-off-disk-only", "attach-iso-and-disk-cd-order", "power-on"]


@pytest.mark.parametrize("failure", ["no-disk", "two-disks", "zero-disk-key", "negative-disk-key", "foreign-disk", "duplicate-sata", "occupied-sata"])
def test_attach_iso_rejects_ambiguous_or_mutated_boot_devices_before_esxi_reconfiguration(client, failure):
    vm = sensor_vm()
    devices = vm.config.hardware.device
    if failure == "no-disk":
        devices.pop(0)
    elif failure == "two-disks":
        devices.append(vim.vm.device.VirtualDisk(
            key=2007, controllerKey=1000, unitNumber=1,
            backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(fileName=f"[datastore1] {BASE}/lab/extra.vmdk"),
        ))
    elif failure in {"zero-disk-key", "negative-disk-key"}:
        devices[0].key = 0 if failure == "zero-disk-key" else -1
    elif failure == "foreign-disk":
        devices[0].backing.fileName = f"[datastore1] gdeploy/{OTHER}/lab/disk.vmdk"
    elif failure == "duplicate-sata":
        devices.append(vim.vm.device.VirtualAHCIController(key=15000, busNumber=1))
    else:
        devices[0].controllerKey = 15000
        devices[0].unitNumber = 0
    client._test_vms.append(vm)
    with pytest.raises(VMwareError):
        client.attach_iso(vm._moId, f"[datastore1] {ISO}", OWNER)
    vm.ReconfigVM_Task.assert_not_called()
    vm.PowerOnVM_Task.assert_not_called()
