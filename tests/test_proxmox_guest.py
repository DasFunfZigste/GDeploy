# ruff: noqa: F811
"""Both OS installers select QGA without weakening installer-completion checks."""
import copy

import pytest

from gdeploy import alma_guest, alma_readiness, guest
from gdeploy.os_profiles import ALMA10
from test_guest_readiness import (DISABLED_STATUS, connected_session, diagnostic_replies, ready_evidence)  # noqa: F401
from test_alma_readiness import evidence  # noqa: F401
from test_alma_guest import keys  # noqa: F401


def test_ubuntu_proxmox_media_installs_and_enables_qemu_agent():
    spec = {'name': 'pve-ubuntu', 'role': 'ubuntu', 'ip_mode': 'dhcp', 'hypervisor': 'proxmox'}
    data = guest._autoinstall_data(spec, 'gdeploy', 'valid-secret', 'ssh-ed25519 AAAATEST')
    assert 'qemu-guest-agent' in data['autoinstall']['packages']
    assert 'open-vm-tools' not in data['autoinstall']['packages']
    assert any('systemctl enable qemu-guest-agent ssh' in command for command in data['autoinstall']['late-commands'])


def test_alma_proxmox_media_installs_and_enables_qemu_agent(keys):
    text, _ = alma_guest.kickstart_data({'name': 'pve-alma', 'role': 'ubuntu', 'ip_mode': 'dhcp', 'hypervisor': 'proxmox'},
        'gdeploy', 'valid-secret', keys[0])
    assert '\nqemu-guest-agent\n' in text and 'open-vm-tools' not in text and 'vmtoolsd' not in text
    assert 'services --enabled=sshd,qemu-guest-agent,NetworkManager,firewalld' in text
    assert 'systemctl enable sshd.service qemu-guest-agent.service' in text
    assert 'os-install-complete.json' in text


def test_proxmox_ubuntu_readiness_requires_qga_and_completion(monkeypatch, connected_session, ready_evidence):
    connected_session.hypervisor = 'proxmox'
    ready_evidence['services']['qemu-guest-agent.service'] = ready_evidence['services'].pop('open-vm-tools.service')
    calls = diagnostic_replies(monkeypatch, connected_session, [DISABLED_STATUS], ready_evidence)
    logs = []
    connected_session.wait_ready(timeout=20, log=lambda text, level: logs.append(text))
    assert 'qemu-guest-agent.service' in calls[-1]
    assert any('QEMU Guest Agent checks passed' in text for text in logs)
    invalid = copy.deepcopy(ready_evidence)
    invalid['disabled_marker'] = False
    with pytest.raises(guest.GuestError, match='completion evidence'):
        guest._installed_system_pending(invalid, disabled=True, hypervisor='proxmox')
    invalid = copy.deepcopy(ready_evidence)
    invalid['services']['qemu-guest-agent.service']['ActiveState'] = 'failed'
    with pytest.raises(guest.GuestError, match='failed qemu'):
        guest._installed_system_pending(invalid, disabled=True, hypervisor='proxmox')


def test_proxmox_alma_readiness_requires_qga_and_kickstart(evidence):
    evidence['services']['qemu-guest-agent.service'] = evidence['services'].pop('vmtoolsd.service')
    assert alma_readiness.pending_reason(evidence, 'alma-test', 'proxmox') is None
    evidence['installer_marker'] = None
    assert 'Kickstart completion' in alma_readiness.pending_reason(evidence, 'alma-test', 'proxmox')
    evidence['services'].pop('qemu-guest-agent.service')
    with pytest.raises(guest.GuestError, match='qemu-guest-agent'):
        alma_readiness.pending_reason(evidence, 'alma-test', 'proxmox')


@pytest.mark.parametrize('profile', ['ubuntu-autoinstall', ALMA10])
def test_proxmox_worker_installs_each_role_after_os_readiness(config, spec, monkeypatch, profile):
    from unittest.mock import MagicMock, Mock
    from gdeploy.db import Database
    from gdeploy import service as module
    spec['vms'].append(spec['vms'][0] | {'name': 'lab-kibana', 'role': 'kibana'})
    spec['os_profile'] = profile
    client = MagicMock()
    client.__enter__.return_value = client
    client.create_vm.side_effect = ['100', '101']
    client.guest_ip.side_effect = lambda vm_id: '192.0.2.' + ('20' if vm_id == '100' else '21')
    db = Database(config.data_dir, config.secret_key)
    service = module.DeploymentService(db, config, proxmox_factory=lambda **options: client)
    monkeypatch.setattr(service, 'preflight', lambda *args, **kwargs: {'ok': True, 'checks': []})
    monkeypatch.setattr(service.media, 'selected', lambda *args: None)
    monkeypatch.setattr(module, 'generate_ssh_key', lambda: ('private-key', 'public-key'))
    prepared = []
    def build(source, output, vm, *args, **kwargs):
        assert vm['hypervisor'] == 'proxmox'
        prepared.append(vm['name'])
        output.write_bytes(b'prepared-iso')
    monkeypatch.setattr(module, 'build_seed_iso', build)
    sessions = []
    elastic = {'ip': '192.0.2.20', 'service_token': 'application-token'}
    def session(*args):
        value = MagicMock()
        value.__enter__.return_value = value
        value.host_key = 'ssh-ed25519 pinned-key'
        def install(role, *args, **kwargs):
            assert len(prepared) == 2
            if role == 'elasticsearch':
                return {'elastic': elastic, 'services': []}
            assert kwargs['elastic'] == elastic
            return {'services': []}
        value.install = Mock(side_effect=install)
        sessions.append(value)
        return value
    monkeypatch.setattr(module, 'GuestSession', session)
    queued = service.enqueue(spec, {'hypervisor': 'proxmox', 'host': 'pve.test', 'node': 'pve',
        'iso_storage': 'local', 'auth_type': 'token', 'username': 'user@pve!token', 'password': 'secret'})
    # Provide a profile snapshot without requiring genuine ISO bytes in this worker test.
    private = db.get(queued['id'], private=True)['secrets']
    private['os_media'] = {'profile': profile, 'path': str(config.ubuntu_iso)}
    db.update(queued['id'], secrets=private)
    monkeypatch.setattr(service.media, 'validate_snapshot', lambda *args: config.ubuntu_iso)
    db.claim()
    service.run(queued['id'])
    result = db.get(queued['id'])
    assert result['status'] == 'completed', result.get('error')
    assert client.create_vm.call_count == client.detach_iso.call_count == client.delete_iso.call_count == 2
    assert all(value.hypervisor == 'proxmox' and value.os_profile == profile for value in sessions)
    assert all(vm['hypervisor'] == 'proxmox' for vm in result['vms'])
