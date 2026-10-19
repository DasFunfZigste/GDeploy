"""Proxmox API contracts, TLS policy, streamed ISO bytes and ownership barriers."""
import hashlib
import json
from unittest.mock import Mock

import pytest
import requests

from gdeploy.proxmox import ProxmoxClient, ProxmoxError, _ISOBody

OWNER = '067a159a-4055-4bfe-b5ed-3b34644b6ebc'
REMOTE = f'gdeploy/{OWNER}/lab.iso'


class Response:
    def __init__(self, data=None, status=200, raw=None):
        self.status_code = status
        self.body = json.dumps({'data': data}).encode() if raw is None else raw
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]


@pytest.fixture
def connected():
    client = ProxmoxClient('pve.test', 'gdeploy@pve!automation', 'private-token-secret', node='pve', iso_storage='local')
    client._http = Mock(headers={}, cookies=requests.cookies.RequestsCookieJar())
    return client


@pytest.mark.parametrize('value,host,port', [('pve.test', 'pve.test', 8006), ('https://pve.test', 'pve.test', 8006),
    ('pve.test:9443', 'pve.test', 9443), ('https://pve.test:8006/', 'pve.test', 8006),
    ('2001:db8::1', '2001:db8::1', 8006), ('https://[2001:db8::1]', '2001:db8::1', 8006)])
def test_endpoint_defaults(value, host, port):
    assert ProxmoxClient._parse_host(value)[:2] == (host, port)


@pytest.mark.parametrize('value', ['http://pve.test', 'https://user:pass@pve.test', 'pve.test/path',
    'https://pve.test?token=secret', 'https://pve.test/#fragment', 'pve.test:0'])
def test_endpoint_rejects_credentials_paths_or_plaintext(value):
    with pytest.raises(ProxmoxError):
        ProxmoxClient._parse_host(value)


def test_token_auth_and_node_selection_use_verified_no_redirect_requests(monkeypatch):
    session = Mock(headers={}, trust_env=True)
    session.request.side_effect = [Response({'version': '9.2.1'}), Response([{'node': 'pve', 'status': 'online'}])]
    monkeypatch.setattr(requests, 'Session', lambda: session)
    with ProxmoxClient('pve.test', 'user@pve!token', 'secret') as client:
        assert client.node == 'pve'
        assert session.headers['Authorization'] == 'PVEAPIToken=user@pve!token=secret'
        assert session.trust_env is False
    for call in session.request.call_args_list:
        assert call.kwargs['allow_redirects'] is False and call.kwargs['verify'] is True
        assert call.kwargs['stream'] is True
        assert call.args[1].startswith('https://pve.test:8006/api2/json/')
    session.close.assert_called_once()


def test_password_ticket_uses_cookie_csrf_and_renewal(connected, monkeypatch):
    connected.auth_type = 'password'
    connected.username = 'user@pve'
    connected._http.request.side_effect = [Response({'ticket': 'ticket-a', 'CSRFPreventionToken': 'csrf-a'}), Response([])]
    monkeypatch.setattr('gdeploy.proxmox.time.monotonic', lambda: 5500)
    assert connected._request('GET', '/nodes') == []
    assert connected._http.cookies.get('PVEAuthCookie') == 'ticket-a'
    assert connected._http.headers['CSRFPreventionToken'] == 'csrf-a'
    assert connected._http.request.call_args_list[0].args[1].endswith('/access/ticket')


@pytest.mark.parametrize('status', [302, 401, 403, 500])
def test_api_failures_do_not_echo_server_credentials(connected, status):
    connected._http.request.return_value = Response(status=status, raw=b'private-token-secret customer-license')
    with pytest.raises(ProxmoxError) as error:
        connected._request('GET', '/version')
    assert 'private-token-secret' not in str(error.value)
    assert 'customer-license' not in str(error.value)


def test_api_responses_are_bounded_and_agent_boot_is_pending(connected):
    connected._http.request.return_value = Response(raw=b' ' * (4 * 1024**2 + 1))
    with pytest.raises(ProxmoxError, match='oversized'):
        connected._request('GET', '/nodes')
    connected._http.request.return_value = Response(status=500)
    assert connected._request('GET', '/agent', agent_pending=True) is None
    connected._http.request.return_value = Response(status=403)
    with pytest.raises(ProxmoxError):
        connected._request('GET', '/agent', agent_pending=True)


def test_multipart_stream_matches_declared_length_checksum_and_iso(tmp_path):
    source = tmp_path / 'source.iso'
    source.write_bytes(bytes(range(256)) * 20000)
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    with source.open('rb') as stream:
        body = _ISOBody(stream, 'gdeploy-test.iso', checksum)
        size = len(body)
        chunks = []
        while chunk := body.read(32000):
            chunks.append(chunk)
        raw = b''.join(chunks)
    assert len(raw) == size and body.remaining == 0
    assert all(len(chunk) <= 32000 for chunk in chunks)
    assert raw.count(source.read_bytes()) == 1
    assert checksum.encode() in raw and b'name="checksum-algorithm"\r\n\r\nsha256' in raw
    assert b'filename="gdeploy-test.iso"' in raw
    assert raw.endswith(b'--\r\n')


def vm_config():
    return {'description': 'GDeploy deployment ' + OWNER, 'digest': 'first', 'name': 'lab',
        'scsi0': 'local-lvm:vm-100-disk-0,size=40G', 'net0': 'virtio=02:00:00:00:00:01,bridge=vmbr0',
        'net1': 'virtio=02:00:00:00:00:02,bridge=vmbr1',
        'ide2': f'local:iso/gdeploy-{OWNER}-lab.iso,media=cdrom'}


def test_create_sensor_uses_two_fixed_macs_and_valid_boot_order(connected):
    calls = []
    config = vm_config()
    def api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == '/cluster/nextid':
            return 100
        if path.endswith('/config'):
            return config
        return 'UPID:pve:create'
    connected._request = api
    connected._task = Mock()
    vm = {'role': 'corelight_sensor', 'name': 'sensor-lab', 'cpu': 4, 'ram_gb': 8, 'disk_gb': 100,
        'network': 'vmbr0', 'monitor_network': 'vmbr1', 'datastore': 'local-lvm'}
    assert connected.create_vm(vm, None, OWNER) == '100'
    submitted = calls[1][2]['data']
    assert submitted['boot'] == 'order=scsi0' and 'ide2' not in submitted
    assert submitted['balloon'] == 0 and submitted['cpu'] == 'host' and submitted['agent'] == 'enabled=1'
    assert submitted['net0'].split(',')[0] != submitted['net1'].split(',')[0]
    assert submitted['description'] == 'GDeploy deployment ' + OWNER
    assert connected.network_macs('100', OWNER) == {'management_mac': '02:00:00:00:00:01', 'monitor_mac': '02:00:00:00:00:02'}


def test_ip_discovery_uses_management_mac_only(connected):
    connected._request = Mock(side_effect=[{'result': [
        {'hardware-address': '02:00:00:00:00:02', 'ip-addresses': [{'ip-address': '192.0.2.99'}]},
        {'hardware-address': '02:00:00:00:00:01', 'ip-addresses': [{'ip-address': 'fe80::1'}, {'ip-address': '192.0.2.10'}]},
    ]}, vm_config()])
    assert connected.guest_ip('100') == '192.0.2.10'


@pytest.mark.parametrize('extra', [{'description': 'Foreign VM'}, {'scsi1': 'local-lvm:vm-100-disk-1'},
    {'scsi0': 'local-lvm:vm-999-disk-0'}, {'ide2': 'local:iso/vendor.iso,media=cdrom'}])
def test_recovery_refuses_foreign_ownership_or_disks(connected, extra):
    connected._request = Mock(return_value=vm_config() | extra)
    with pytest.raises(ProxmoxError):
        connected.destroy_vm('100', OWNER)
    assert not any(call.args[0] == 'DELETE' for call in connected._request.call_args_list)


def test_recovery_checks_config_after_stop_and_never_deletes_unreferenced_disks(connected):
    connected._request = Mock(side_effect=[vm_config(), {'status': 'stopped'}, vm_config(), 'UPID:pve:delete'])
    connected._task = Mock()
    connected.destroy_vm('100', OWNER)
    assert connected._request.call_args.args[0] == 'DELETE'
    assert connected._request.call_args.kwargs['data'] == {'purge': 0, 'destroy-unreferenced-disks': 0}
    connected._request = Mock(side_effect=[vm_config(), {'status': 'stopped'}, vm_config() | {'digest': 'changed'}])
    with pytest.raises(ProxmoxError, match='changed'):
        connected.destroy_vm('100', OWNER)


def test_shared_iso_is_not_deleted_when_attached_on_another_node(connected):
    connected._request = Mock(side_effect=[{'/vms': {'VM.Audit': 1}}, [{'type': 'qemu', 'vmid': 200, 'node': 'other-node'}], vm_config()])
    with pytest.raises(ProxmoxError, match='attached'):
        connected.delete_iso('local-lvm', REMOTE, OWNER)
    assert '/nodes/other-node/qemu/200/config' in connected._request.call_args.args
    assert not any(call.args[0] == 'DELETE' for call in connected._request.call_args_list)


@pytest.mark.parametrize('path', ['../vendor.iso', f'gdeploy/{OWNER}/../vendor.iso', REMOTE.replace(OWNER, 'other')])
def test_iso_cleanup_rejects_unowned_paths_before_network(connected, path):
    connected._request = Mock()
    with pytest.raises(Exception):
        connected.delete_iso('local-lvm', path, OWNER)
    connected._request.assert_not_called()


def test_permission_preflight_fails_closed_with_actionable_privileges(connected):
    connected._request = Mock(return_value={})
    with pytest.raises(ProxmoxError, match='Sys.Audit'):
        connected.preflight_permissions({'vms': [{'datastore': 'local-lvm', 'network': 'vmbr0'}]})


def test_task_failure_and_foreign_node_task_are_rejected(connected):
    connected._request = Mock(return_value={'status': 'stopped', 'exitstatus': 'Disk allocation failed'})
    with pytest.raises(ProxmoxError, match='Disk allocation failed'):
        connected._task('UPID:pve:task', 'Create VM')
    with pytest.raises(ProxmoxError, match='configured Proxmox node'):
        connected._task('UPID:foreign:task', 'Create VM')


def test_recovery_refuses_partial_vm_visibility(connected):
    connected._request = Mock(return_value={"/vms": {}})
    with pytest.raises(ProxmoxError, match="VM.Audit"):
        connected.find_owned_vms(OWNER)
    assert connected._request.call_count == 1
