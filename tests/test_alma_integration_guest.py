"""Dispatch and persistence boundaries for the alternate OS installer."""
import hashlib
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from gdeploy.guest import GuestError, GuestSession
from gdeploy.main import create_app
from gdeploy.os_profiles import ALMA10, UBUNTU
from test_fleetmanager_api import payload as fleet_payload


def session():
    return GuestSession('192.0.2.20', 'gdeploy', '', 'private-os-password', os_profile=ALMA10)


def test_alma_fleet_settings_preserve_independent_debian_and_rpm_choices(signed_in, config):
    endpoint = '/api/settings/fleetmanager'
    original = {**fleet_payload(), 'online_version': '29.2.2-1~ubuntu24.04', 'online_version_rpm': '0:29.2.1-1.x86_64'}
    response = signed_in.put(endpoint, json=original)
    assert response.status_code == 200 and response.json()['ready']
    assert response.json()['online_version_rpm'] == original['online_version_rpm']
    assert signed_in.put(endpoint, json={'online_version_rpm': ''}).json()['online_version'] == original['online_version']
    assert signed_in.put(endpoint, json={'online_version_rpm': original['online_version_rpm']}).status_code == 200
    with TestClient(create_app(config, start_worker=False)) as restarted:
        login = restarted.post('/api/login', json={'username': 'admin', 'password': 'test-admin-passphrase'})
        restarted.headers['X-CSRF-Token'] = login.json()['csrf_token']
        data = restarted.get(endpoint).json()
        assert data['online_version'] == original['online_version']
        assert data['online_version_rpm'] == original['online_version_rpm']
        for key in ('repository_token', 'community_string', 'license_pem'):
            assert original[key] not in str(data)


@pytest.mark.parametrize('invalid', ['29.2.1-1~ubuntu24.04', '29.2.1', '--version', '1:29.2-1.aarch64', '29.2.1-1.x86_64\n', '0:29.*-1.x86_64'])
def test_rpm_choice_rejects_other_architectures_and_debian_versions(signed_in, invalid):
    response = signed_in.put('/api/settings/fleetmanager', json={**fleet_payload(), 'online_version_rpm': invalid})
    assert response.status_code == 422
    assert signed_in.app.state.db.fleetmanager_settings() is None


def test_version_lookup_routes_to_the_requested_os_without_exposing_token(signed_in, monkeypatch):
    rpm = Mock(return_value=['0:29.2.1-1.x86_64'])
    deb = Mock(return_value=['29.2.1-1~ubuntu24.04'])
    monkeypatch.setattr('gdeploy.rpm_repository.available_versions', rpm)
    monkeypatch.setattr('gdeploy.fleet_repository.available_versions', deb)
    endpoint = '/api/settings/fleetmanager/versions'
    token = 'private-token'
    assert signed_in.post(endpoint, json={'repository_token': token, 'os_profile': ALMA10}).json() == {'versions': rpm.return_value}
    assert signed_in.post(endpoint, json={'repository_token': token, 'os_profile': UBUNTU}).json() == {'versions': deb.return_value}
    rpm.assert_called_once_with(token)
    deb.assert_called_once_with(token)
    assert signed_in.app.state.db.fleetmanager_settings() is None


def test_alma_fleet_dispatch_uses_rpm_version_and_preserves_verified_credentials(monkeypatch):
    guest = session()
    fleet = {**fleet_payload(), 'online_version': '29.2.2-1~ubuntu24.04', 'online_version_rpm': '0:29.2.1-1.x86_64'}
    fleet['license_sha256'] = hashlib.sha256(fleet['license_pem'].encode()).hexdigest()
    runner = Mock(return_value={'username': 'admin', 'password': 'vendor-initial-password', 'password_change_required': True, 'version': '0:29.2.1-1.x86_64'})
    monkeypatch.setattr(guest, '_run_script', runner)
    log = []
    result = guest.install('fleetmanager', {}, fleetmanager=fleet, log=log.append)
    script, sent = runner.call_args.args
    assert 'configure_online_repository = _alma_configure_fleet_repository' in script
    assert 'online_version' not in sent['fleetmanager']
    assert sent['fleetmanager']['online_version_rpm'] == fleet['online_version_rpm']
    assert result['services'][0]['password'] == 'vendor-initial-password'
    assert any('Experimental AlmaLinux 10' in entry for entry in log)
    assert fleet['repository_token'] in guest._secrets


def test_alma_fleet_without_rpm_choice_never_inherits_debian_version(monkeypatch):
    guest = session()
    fleet = {**fleet_payload(), 'online_version': '29.2.2-1~ubuntu24.04', 'license_sha256': 'a' * 64}
    runner = Mock(return_value={'username': 'admin', 'password': 'vendor-initial-password', 'password_change_required': True, 'version': '0:29.2.2-1.x86_64'})
    monkeypatch.setattr(guest, '_run_script', runner)
    guest.install('fleetmanager', {}, fleetmanager=fleet)
    assert runner.call_args.args[1]['fleetmanager']['online_version_rpm'] == ''


def test_os_only_alma_has_no_application_commands(monkeypatch):
    guest = session()
    runner = Mock(side_effect=AssertionError('OS-only must not install an application'))
    monkeypatch.setattr(guest, '_run_script', runner)
    logs = []
    assert guest.install('ubuntu', {}, log=logs.append) == {'services': []}
    assert 'AlmaLinux 10 installation verified' in logs[0]


def test_alma_elastic_dispatch_returns_exact_rpm_build_for_kibana(monkeypatch):
    guest = session()
    elastic = {'ip': '192.0.2.20', 'ca_pem': '-----BEGIN CERTIFICATE-----\nfixture', 'service_token': 'private-service-token', 'version': '9.2.0', 'rpm_release': '1'}
    runner = Mock(return_value=elastic)
    monkeypatch.setattr(guest, '_run_script', runner)
    result = guest.install('elasticsearch', {'elastic_password': 'private-elastic-password'})
    assert result['elastic']['rpm_release'] == '1'
    assert 'dnf' in runner.call_args.args[0]
    guest.install('kibana', {'elastic_password': 'private-elastic-password', **{key: 'p' * 40 for key in ('kibana_encryption_key', 'kibana_security_key', 'kibana_reporting_key')}}, elastic=elastic)
    assert runner.call_args.args[1]['elastic']['rpm_release'] == '1'
    assert 'install_kibana_rpm' in runner.call_args.args[0]


def test_alma_sensor_dispatch_preserves_pairing_and_monitor_identity(monkeypatch):
    guest = session()
    snapshot = {'repository_token': 'sensor-private-token', 'community_string': 'private-community', 'license_key': 'private-license', 'pairing_token': 'private-pairing-token', 'fleet_url': 'https://fleet.example.test:1443/pair', 'server_sslname': 'fleet.example.test', 'management_mac': '00:50:56:01:02:03', 'monitor_mac': '00:50:56:01:02:04', 'api_network': '192.0.2.0/24'}
    runner = Mock(return_value={'healthy': True, 'version': '0:29.2.1-1.x86_64'})
    monkeypatch.setattr(guest, '_run_script', runner)
    guest.install('corelight_sensor', {}, corelight_sensor=snapshot)
    script, payload = runner.call_args.args
    assert 'host_checks = _alma_sensor_host_checks' in script and 'el/9/$basearch' in script
    assert payload['corelight_sensor']['pairing_token'] == snapshot['pairing_token']
    assert payload['corelight_sensor']['monitor_mac'] == snapshot['monitor_mac']
    assert runner.call_args.kwargs['timeout'] == 5400


def test_alma_fleet_invalid_rpm_choice_fails_before_guest_script(monkeypatch):
    guest = session()
    runner = Mock()
    monkeypatch.setattr(guest, '_run_script', runner)
    with pytest.raises(GuestError, match='exact Fleet Manager RPM'):
        guest.install('fleetmanager', {}, fleetmanager={**fleet_payload(), 'license_sha256': 'a' * 64, 'online_version_rpm': '29.2.1-1~ubuntu24.04'})
    runner.assert_not_called()
