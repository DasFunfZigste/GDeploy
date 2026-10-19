"""Named encrypted targets, legacy migration, immutable jobs and scoped capacity."""
import copy
from dataclasses import replace
from unittest.mock import Mock

import pytest

from gdeploy.db import Database
from gdeploy.models import TargetHostSettings
from gdeploy.service import DeploymentService
from gdeploy.targets import TargetHosts, TargetHostError
from test_media_deployments import iso_file


PAYLOAD = {'name': 'Lab Proxmox', 'hypervisor': 'proxmox', 'host': 'pve.test',
    'username': 'gdeploy@pve!automation', 'password': 'private-api-token', 'auth_type': 'token',
    'node': 'pve', 'iso_storage': 'local', 'default_network': 'vmbr0'}


class Inventory:
    def __init__(self, **kwargs):
        self.options = kwargs
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def inventory(self):
        return {'hypervisor': 'proxmox', 'node': 'pve', 'host': {'cpu_threads': 32, 'cpu_cores': 16, 'memory_gb': 128},
            'datastores': [{'name': 'local-lvm', 'free_gb': 1000}],
            'iso_storages': [{'name': 'local', 'free_gb': 100}],
            'networks': [{'name': 'vmbr0'}, {'name': 'vmbr1'}], 'vms': []}
    def preflight_permissions(self, spec):
        pass


@pytest.fixture
def target_service(config, spec, monkeypatch):
    import gdeploy.service as module
    checksum = iso_file(config.ubuntu_iso, b'verified-target-media')
    config = replace(config, ubuntu_sha256=checksum)
    db = Database(config.data_dir, config.secret_key)
    service = DeploymentService(db, config, proxmox_factory=Inventory)
    monkeypatch.setattr(module.shutil, 'which', lambda command: '/fixture/xorriso')
    monkeypatch.setattr(module, 'generate_ssh_key', lambda: ('private-key', 'public-key'))
    target = TargetHosts(db, service).save(TargetHostSettings(**PAYLOAD))
    spec['target_host_id'] = target['id']
    spec['vms'][0].update(datastore='local-lvm', network='vmbr0')
    return service, spec, target


def test_hosts_are_encrypted_and_only_public_metadata_is_returned(signed_in):
    response = signed_in.post('/api/hosts', json=PAYLOAD)
    assert response.status_code == 201
    host = response.json()
    assert host['hypervisor'] == 'proxmox' and 'password' not in host
    assert signed_in.get('/api/settings').json()['configured'] is True
    assert signed_in.get('/api/settings').json()['target_hosts'] == [host]
    db = signed_in.app.state.db
    assert db.target_host(host['id'])['password'] == PAYLOAD['password']
    with db.connect() as connection:
        assert PAYLOAD['password'] not in connection.execute('SELECT value FROM target_hosts').fetchone()[0]
    response = signed_in.put('/api/hosts/' + host['id'], json=PAYLOAD | {'name': 'Renamed', 'password': ''})
    assert response.status_code == 200
    assert db.target_host(host['id'])['password'] == PAYLOAD['password']
    assert db.target_host(host['id'])['name'] == 'Renamed'
    assert signed_in.put('/api/hosts/' + host['id'], json=PAYLOAD | {'host': 'other.test', 'password': ''}).status_code == 400
    assert PAYLOAD['password'] not in signed_in.get('/api/hosts').text
    assert signed_in.delete('/api/hosts/' + host['id']).status_code == 200
    assert db.target_host(host['id']) is None


@pytest.mark.parametrize('method,path', [('GET', '/api/hosts'), ('POST', '/api/hosts'),
    ('PUT', '/api/hosts/example'), ('DELETE', '/api/hosts/example'),
    ('POST', '/api/hosts/certificate/inspect'), ('POST', '/api/hosts/certificate/trust'),
    ('DELETE', '/api/hosts/certificate/trust')])
def test_host_routes_require_account_and_csrf(signed_in, method, path):
    data = PAYLOAD if '/certificate/' not in path else {'hypervisor': 'proxmox', 'host': 'pve.test',
        **({'fingerprint_sha256': ':'.join(['AA'] * 32)} if path.endswith('/trust') and method == 'POST' else {})}
    token = signed_in.headers.pop('X-CSRF-Token')
    if method != 'GET':
        assert signed_in.request(method, path, json=data).status_code == 403
    signed_in.headers['X-CSRF-Token'] = token
    with signed_in.app.state.db.connect() as connection:
        connection.execute('UPDATE administrator SET must_change_credentials=1')
    assert signed_in.request(method, path, json=data).status_code == 403
    signed_in.cookies.clear()
    assert signed_in.request(method, path, json=data).status_code == 401


def test_certificate_approval_uses_proxmox_port_and_is_endpoint_scoped(signed_in, monkeypatch):
    from test_certificate_api import certificate  # pytest fixture factory has a reusable wrapped implementation
    from gdeploy import tls
    pem = certificate.__wrapped__()()
    calls = []
    monkeypatch.setattr(tls, 'fetch_certificate', lambda host, port: calls.append((host, port)) or pem)
    target = {'host': 'esxi.lab.test', 'hypervisor': 'proxmox'}
    details = signed_in.post('/api/hosts/certificate/inspect', json=target).json()
    assert details['endpoint'] == 'https://esxi.lab.test:8006'
    assert not details['trusted']
    response = signed_in.post('/api/hosts/certificate/trust', json=target | {'fingerprint_sha256': details['fingerprint_sha256']})
    assert response.status_code == 200 and response.json()['trusted']
    assert calls == [('esxi.lab.test', 8006), ('esxi.lab.test', 8006)]
    assert signed_in.app.state.db.esxi_certificate('https://esxi.lab.test:443') is None
    assert signed_in.request('DELETE', '/api/hosts/certificate/trust', json=target).status_code == 200
    assert signed_in.app.state.db.esxi_certificate(details['endpoint']) is None


def test_same_endpoint_auto_node_alias_is_rejected(signed_in):
    assert signed_in.post('/api/hosts', json=PAYLOAD).status_code == 201
    assert signed_in.post('/api/hosts', json=PAYLOAD | {'node': ''}).status_code == 409
    assert signed_in.post('/api/hosts', json=PAYLOAD | {'node': 'second-node'}).status_code == 201


def test_legacy_esxi_is_available_without_rewriting_settings(signed_in):
    payload = {'host': 'esxi.test', 'username': 'root', 'password': 'old-password', 'verify_tls': True}
    signed_in.put('/api/settings', json=payload)
    host = signed_in.get('/api/hosts').json()[0]
    assert host['id'] == 'legacy-esxi' and host['hypervisor'] == 'esxi'
    assert signed_in.app.state.db.settings() == payload
    assert signed_in.post('/api/hosts', json=PAYLOAD | {'hypervisor': 'esxi', 'host': 'esxi.test',
        'username': 'root', 'auth_type': 'password', 'node': '', 'iso_storage': ''}).status_code == 409


def test_job_snapshot_survives_profile_edit_and_removal(target_service):
    service, spec, target = target_service
    job = service.enqueue(spec)
    private = service.db.get(job['id'], private=True)
    assert private['secrets']['esxi']['password'] == PAYLOAD['password']
    assert private['spec']['target']['hypervisor'] == 'proxmox'
    assert PAYLOAD['password'] not in str(job)
    with pytest.raises(TargetHostError, match='active deployments'):
        TargetHosts(service.db, service).remove(target['id'])
    service.db.set_target_host(target['id'], PAYLOAD | {'host': 'different.test', 'password': 'replacement-token'})
    service.db.update(job['id'], status='failed', stage='failed')
    TargetHosts(service.db, service).remove(target['id'])
    snapshot = service.db.get(job['id'], private=True)['secrets']['esxi']
    assert snapshot['host'] == 'pve.test' and snapshot['password'] == PAYLOAD['password']
    client = service.client(snapshot)
    assert client.options['host'] == 'pve.test'
    assert service.preflight(spec, snapshot, exclude_id=job['id'])['ok']


def test_names_and_resources_reserve_only_the_same_target(target_service):
    service, spec, target = target_service
    service.enqueue(spec)
    same = service.preflight(spec)
    assert next(item for item in same['checks'] if item['name'] == 'VM names')['ok'] is False
    other = TargetHosts(service.db, service).save(TargetHostSettings(**(PAYLOAD | {'host': 'other.test'})))
    second_spec = copy.deepcopy(spec) | {'target_host_id': other['id']}
    assert service.preflight(second_spec)['ok']


def test_disk_and_iso_storage_are_validated_independently(target_service):
    service, spec, _ = target_service
    assert service.preflight(spec)['ok']
    service.db.set_target_host(spec['target_host_id'], PAYLOAD | {'iso_storage': 'missing'})
    result = service.preflight(spec)
    assert next(item for item in result['checks'] if item['name'] == 'Proxmox ISO storage')['ok'] is False


def test_provider_dispatch_never_passes_profile_metadata_to_adapter(target_service):
    service, _, target = target_service
    service.proxmox_factory = Mock(return_value='proxmox-client')
    assert service.client(service.db.target_host(target['id'])) == 'proxmox-client'
    options = service.proxmox_factory.call_args.kwargs
    assert 'id' not in options and 'hypervisor' not in options and 'name' not in options
    assert options['verify_tls'] is True
    service.client_factory = Mock(return_value='esxi-client')
    assert service.client({'host': 'esxi.test', 'username': 'root', 'password': 'secret', 'hypervisor': 'esxi', 'id': 'legacy-esxi'}) == 'esxi-client'
    assert set(service.client_factory.call_args.kwargs) == {'host', 'username', 'password', 'verify_tls'}


def test_redeploy_uses_frozen_target_after_profile_removal(target_service):
    service, spec, target = target_service
    job = service.enqueue(spec)
    vm = job['vms'][0] | {'vm_id': '100'}
    service.db.update(job['id'], status='failed', stage='failed', vms=[vm],
        resources=[{'datastore': 'local-lvm', 'path': f"gdeploy/{job['id']}/{vm['name']}.iso"}])
    service.db.set_target_host(target['id'], PAYLOAD | {'host': 'changed.test', 'password': 'replacement-secret'})
    TargetHosts(service.db, service).remove(target['id'])
    calls = []
    class RecoveryInventory(Inventory):
        def find_owned_vms(self, owner):
            calls.append(('find', self.options['host'], owner))
            return [{'vm_id': '100', 'name': vm['name']}]
        def destroy_vm(self, vm_id, owner):
            calls.append(('destroy', self.options['host'], vm_id, owner))
        def delete_iso(self, store, path, owner):
            calls.append(('delete-iso', self.options['host'], owner))
    service.proxmox_factory = RecoveryInventory
    replacement = service.redeploy(job['id'], job['name'])
    snapshot = service.db.get(replacement['id'], private=True)['secrets']['esxi']
    assert snapshot['host'] == 'pve.test' and snapshot['password'] == PAYLOAD['password']
    assert [item[0] for item in calls] == ['find', 'destroy', 'delete-iso']
    assert all(item[1] == 'pve.test' for item in calls)
    assert service.db.get(job['id'])['status'] == 'reverted'
