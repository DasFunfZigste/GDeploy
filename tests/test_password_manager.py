"""Encrypted credentials, deployment adoption, immutable provisioning and RBAC."""
import re
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from gdeploy.db import Database
from gdeploy.password_manager import CredentialUpdate, CredentialError, PasswordManager

TEMP = 'temporary-user-passphrase'
CHOSEN = 'chosen-user-passphrase'
ENTRY = {'title': 'Lab desktop', 'application': 'Ubuntu', 'username': 'lab-user', 'password': ' manual-secret ',
         'host': '192.0.2.20', 'port': 22, 'connection_type': 'SSH', 'notes': 'private lab note', 'private_key': 'PRIVATE-KEY-FIXTURE'}


def create_user(admin, role='credential_viewer', username='lab-viewer'):
    response = admin.post('/api/users', json={'username': username, 'role': role, 'password': TEMP})
    assert response.status_code == 201, response.text
    return response.json()['id']


def user_client(admin, role='credential_viewer', username='lab-viewer', setup=True):
    identifier = create_user(admin, role, username)
    visitor = TestClient(admin.app)
    response = visitor.post('/api/login', json={'username': username, 'password': TEMP})
    assert response.status_code == 200
    visitor.headers['X-CSRF-Token'] = response.json()['csrf_token']
    assert response.json()['must_change_credentials'] is True
    if setup:
        response = visitor.post('/api/account/setup', json={'username': username, 'password': CHOSEN, 'password_confirm': CHOSEN})
        assert response.status_code == 200, response.text
        response = visitor.post('/api/login', json={'username': username, 'password': CHOSEN})
        visitor.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return visitor, identifier


def test_crud_masks_secrets_encrypts_every_field_and_preserves_password_whitespace(signed_in, config):
    response = signed_in.post('/api/password-manager', json=ENTRY)
    assert response.status_code == 201
    item = response.json()
    assert item['has_password'] and item['has_private_key']
    listing = signed_in.get('/api/password-manager')
    for secret in ('manual-secret', 'PRIVATE-KEY-FIXTURE', 'private lab note'):
        assert secret not in response.text and secret not in listing.text
    details = signed_in.post(f"/api/password-manager/{item['id']}/reveal")
    assert details.json()['password'] == ENTRY['password']
    assert details.headers['Cache-Control'] == 'no-store'
    update = {**ENTRY, 'title': 'Renamed host', 'password': 'changed-vault-password', 'connection_type': 'RDP', 'port': 3389, 'revision': item['revision']}
    response = signed_in.put(f"/api/password-manager/{item['id']}", json=update)
    assert response.status_code == 200
    assert signed_in.put(f"/api/password-manager/{item['id']}", json=update).status_code == 409
    assert signed_in.delete(f"/api/password-manager/{item['id']}?revision=1").status_code == 409
    reopened = Database(config.data_dir, config.secret_key)
    assert PasswordManager(reopened).reveal(item['id'], 'admin')['password'] == 'changed-vault-password'
    with reopened.connect() as c:
        audit = ' '.join(row[0] for row in c.execute('SELECT action FROM audit'))
        stored = ' '.join(row[0] for row in c.execute('SELECT value FROM credential_entries'))
    for sensitive in (*ENTRY.values(), 'changed-vault-password'):
        if isinstance(sensitive, str) and sensitive:
            assert sensitive not in stored
    assert 'changed-vault-password' not in audit and ENTRY['private_key'] not in audit
    assert 'entry revealed' in audit and 'actor=admin' in audit
    assert signed_in.delete(f"/api/password-manager/{item['id']}?revision=2").status_code == 200
    assert signed_in.get('/api/password-manager').json()['entries'] == []


@pytest.mark.parametrize('role,can_edit,can_admin', [('credential_viewer', False, False), ('credential_editor', True, False), ('administrator', True, True)])
def test_role_permissions_are_enforced_by_the_api(signed_in, role, can_edit, can_admin):
    entry = signed_in.post('/api/password-manager', json=ENTRY).json()
    visitor, _ = user_client(signed_in, role)
    session = visitor.get('/api/session').json()
    assert session['role'] == role and not session['is_primary']
    assert visitor.get('/api/password-manager').json()['can_edit'] is can_edit
    assert visitor.post(f"/api/password-manager/{entry['id']}/reveal").json()['password'] == ENTRY['password']
    response = visitor.post('/api/password-manager', json=ENTRY)
    assert response.status_code == (201 if can_edit else 403)
    assert visitor.put(f"/api/password-manager/{entry['id']}", json={**ENTRY, 'revision': 1}).status_code == (200 if can_edit else 403)
    assert visitor.delete(f"/api/password-manager/{entry['id']}?revision={2 if can_edit else 1}").status_code == (200 if can_edit else 403)
    assert visitor.get('/api/users').status_code == (200 if can_admin else 403)
    assert visitor.get('/api/settings').status_code == (200 if can_admin else 403)


@pytest.mark.parametrize('role', ['credential_editor', 'credential_viewer'])
def test_restricted_users_cannot_call_any_administrator_endpoint(signed_in, role):
    visitor, _ = user_client(signed_in, role)
    public = {'/api/session', '/api/health', '/api/login', '/api/logout', '/api/account/setup', '/api/account/password'}
    for route in signed_in.app.routes:
        if not route.path.startswith('/api/') or route.path in public or route.path.startswith('/api/password-manager'):
            continue
        path = re.sub(r'\{[^}]+\}', lambda _: str(uuid4()), route.path)
        for method in route.methods:
            if method in {'HEAD', 'OPTIONS'}:
                continue
            result = visitor.request(method, path, json={})
            assert result.status_code == 403, (role, method, path, result.text)


def test_pending_users_csrf_validation_and_revocation(signed_in):
    visitor, identifier = user_client(signed_in, setup=False)
    assert visitor.get('/api/password-manager').status_code == 403
    assert visitor.get('/api/users').status_code == 403
    assert visitor.post('/api/account/setup', json={'username': 'lab-viewer', 'password': TEMP, 'password_confirm': TEMP}).status_code == 422
    assert visitor.post('/api/account/setup', json={'username': 'renamed-user', 'password': CHOSEN, 'password_confirm': CHOSEN}).status_code == 422
    assert visitor.post('/api/account/setup', json={'username': 'lab-viewer', 'password': CHOSEN, 'password_confirm': CHOSEN}).status_code == 200
    assert visitor.get('/api/session').json() == {'authenticated': False}
    response = visitor.post('/api/login', json={'username': 'lab-viewer', 'password': CHOSEN})
    visitor.headers['X-CSRF-Token'] = response.json()['csrf_token']
    item = signed_in.post('/api/password-manager', json=ENTRY).json()
    assert visitor.post(f"/api/password-manager/{item['id']}/reveal", headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert signed_in.put('/api/users/' + identifier, json={'username': 'lab-viewer', 'role': 'credential_editor', 'enabled': False}).status_code == 200
    assert visitor.get('/api/password-manager').status_code == 401
    assert visitor.post('/api/login', json={'username': 'lab-viewer', 'password': CHOSEN}).status_code == 401
    assert signed_in.delete('/api/users/' + identifier).status_code == 200
    assert signed_in.get('/api/users').json()['users'][0]['id'] == 'primary'


def test_password_reset_role_changes_and_account_password_rotation(signed_in):
    visitor, identifier = user_client(signed_in, 'credential_editor')
    assert signed_in.put('/api/users/' + identifier, json={'username': 'lab-viewer', 'role': 'credential_viewer', 'password': 'reset-temporary-passphrase'}).status_code == 200
    assert visitor.get('/api/password-manager').status_code == 401
    assert visitor.post('/api/login', json={'username': 'lab-viewer', 'password': CHOSEN}).status_code == 401
    result = visitor.post('/api/login', json={'username': 'lab-viewer', 'password': 'reset-temporary-passphrase'})
    assert result.json()['must_change_credentials'] is True and result.json()['role'] == 'credential_viewer'
    result = signed_in.post('/api/account/password', json={'current_password': 'test-admin-passphrase', 'password': 'new-instance-passphrase', 'password_confirm': 'new-instance-passphrase'})
    assert result.status_code == 200
    assert signed_in.get('/api/password-manager').status_code == 401
    assert signed_in.post('/api/login', json={'username': 'admin', 'password': 'new-instance-passphrase'}).status_code == 200


def test_reserved_users_duplicates_self_changes_and_hashes(signed_in):
    identifier = create_user(signed_in)
    assert signed_in.post('/api/users', json={'username': 'LAB-VIEWER', 'password': TEMP}).status_code == 409
    assert signed_in.post('/api/users', json={'username': 'admin', 'password': TEMP}).status_code == 409
    assert signed_in.post('/api/users', json={'username': 'new-admin', 'password': ''}).status_code == 422
    assert signed_in.post('/api/users', json={'username': 'new-admin', 'password': TEMP, 'role': 'superuser'}).status_code == 422
    visitor, admin_id = user_client(signed_in, 'administrator', 'second-admin')
    assert visitor.delete('/api/users/' + admin_id).status_code == 409
    assert visitor.put('/api/users/' + admin_id, json={'username': 'second-admin', 'role': 'credential_viewer'}).status_code == 409
    assert signed_in.put('/api/users/primary', json={'username': 'primary', 'role': 'credential_viewer'}).status_code == 422
    assert TEMP not in signed_in.get('/api/users').text
    with signed_in.app.state.db.connect() as c:
        row = c.execute('SELECT password_hash FROM gui_users WHERE id=?', (identifier,)).fetchone()
        assert TEMP not in row[0] and row[0].startswith('scrypt$')


def test_provisioning_sync_preserves_saved_edits_and_original_guest_material(signed_in, spec, config):
    db = signed_in.app.state.db
    identifier = str(uuid4())
    secrets = {'vm_credentials': {'lab-elasticsearch': {'username': 'gdeploy', 'password': 'original-guest-password', 'private_key': 'AUTOMATION-KEY', 'services': []}},
               'software': {'elastic_password': 'original-elastic-password'}}
    db.create(identifier, spec, secrets)
    entries = signed_in.get('/api/password-manager').json()['entries']
    assert len(entries) == 2
    os_entry = next(item for item in entries if item['connection_type'] == 'SSH')
    details = signed_in.post(f"/api/password-manager/{os_entry['id']}/reveal").json()
    values = {key: details[key] for key in ENTRY}
    values.update(password='edited-copy', title='My SSH login', port=2222, revision=details['revision'])
    assert signed_in.put(f"/api/password-manager/{os_entry['id']}", json=values).status_code == 200
    job = db.get(identifier, private=True)
    job['vms'][0].update(ip='192.0.2.30', status='completed')
    secrets['elastic'] = {'service_token': 'generated-kibana-service-token'}
    secrets['vm_credentials']['lab-elasticsearch']['services'] = [{'name': 'Elasticsearch', 'url': 'https://192.0.2.30:9200', 'username': 'elastic', 'password': 'original-elastic-password'}]
    db.update(identifier, vms=job['vms'], secrets=secrets, status='completed')
    details = signed_in.post(f"/api/password-manager/{os_entry['id']}/reveal").json()
    assert (details['password'], details['title'], details['host'], details['port']) == ('edited-copy', 'My SSH login', '192.0.2.30', 2222)
    assert db.get(identifier, private=True)['secrets']['vm_credentials']['lab-elasticsearch']['password'] == 'original-guest-password'
    assert len(signed_in.get('/api/password-manager').json()['entries']) == 3
    db.set_visibility(identifier, True)
    with db.connect() as c:  # Simulate upgrading an existing volume, including hidden jobs.
        c.execute('DELETE FROM credential_entries')
    reopened = Database(config.data_dir, config.secret_key)
    vault = PasswordManager(reopened)
    assert len(vault.list('admin')) == 3
    item = next(item for item in vault.list('admin') if item['connection_type'] == 'SSH')
    vault.delete(item['id'], item['revision'], 'admin')
    reopened = Database(config.data_dir, config.secret_key)
    assert len(PasswordManager(reopened).list('admin')) == 2
    assert reopened.get(identifier, private=True)['secrets']['vm_credentials']['lab-elasticsearch']['password'] == 'original-guest-password'


def test_concurrent_edits_do_not_overwrite_each_other(signed_in):
    item = signed_in.post('/api/password-manager', json=ENTRY).json()
    vault = signed_in.app.state.password_manager
    def change(password):
        try:
            vault.save(CredentialUpdate(**{**ENTRY, 'password': password, 'revision': 1}), 'admin', item['id'])
            return 'saved'
        except CredentialError as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(change, ['first-change', 'second-change']), key=str) == [409, 'saved']


@pytest.mark.parametrize('payload', [{'port': True}, {'port': 65536}, {'host': 'https://host/path'}, {'title': '  '}, {'connection_type': 'javascript'}, {'password': 'x'*8193}, {'title': 'line\nbreak'}])
def test_invalid_payload_does_not_echo_secrets(signed_in, payload):
    response = signed_in.post('/api/password-manager', json={**ENTRY, **payload})
    assert response.status_code == 422
    assert ENTRY['password'] not in response.text and ENTRY['private_key'] not in response.text
    assert signed_in.get('/api/password-manager').json()['entries'] == []


def test_new_endpoints_block_anonymous_and_pending_instance_accounts(client, signed_in):
    signed_in.cookies.clear()
    assert client.get('/api/password-manager').status_code == 401
    assert client.get('/api/users').status_code == 401


def test_all_application_logins_are_collected_on_worker_completion(signed_in):
    db = signed_in.app.state.db
    roles = ['ubuntu', 'splunk', 'elasticsearch', 'kibana', 'fleetmanager', 'corelight_sensor']
    vms = [{'name': 'lab-' + role, 'role': role} for role in roles]
    spec = {'name': 'All application roles', 'vms': vms}
    secret_data = {'vm_credentials': {vm['name']: {'username': 'gdeploy', 'password': vm['role'] + '-os-pass'} for vm in vms},
                   'software': {'elastic_password': 'elastic-login', 'splunk_password': 'splunk-login'},
                   'fleetmanager': {'repository_token': 'never-a-login-token', 'license_pem': 'never-a-login-license'}}
    identifier = str(uuid4())
    db.create(identifier, spec, secret_data)
    db.claim()
    vms = db.get(identifier)['vms']
    for index, vm in enumerate(vms):
        vm['ip'] = f'192.0.2.{index + 30}'
    secret_data['elastic'] = {'service_token': 'kibana-service-token', 'ca_pem': 'not-a-login-certificate'}
    secret_data['vm_credentials']['lab-fleetmanager']['services'] = [{
        'name': 'Fleet Manager', 'url': 'https://192.0.2.34', 'username': 'admin', 'password': 'fleet-vendor-password',
        'community_string': 'fleet-community', 'password_change_required': True}]
    secret_data['vm_credentials']['lab-corelight_sensor']['services'] = [{
        'name': 'Corelight Software Sensor API', 'url': 'https://192.0.2.35', 'community_string': 'sensor-community'}]
    db.finish(identifier, 'completed', vms=vms, resources=[], secrets=secret_data)
    vault = signed_in.app.state.password_manager
    entries = [vault.reveal(item['id'], 'admin') for item in vault.list('admin')]
    assert len(entries) == 13  # Six OS, three Splunk/Elastic, three Corelight, one service token.
    assert all(item['host'] for item in entries)
    passwords = {item['password'] for item in entries}
    assert {'elastic-login', 'splunk-login', 'fleet-vendor-password', 'fleet-community', 'sensor-community', 'kibana-service-token'} <= passwords
    assert not {'never-a-login-token', 'never-a-login-license', 'not-a-login-certificate'} & passwords
    fleet = next(item for item in entries if item['password'] == 'fleet-vendor-password')
    assert fleet['port'] == 443 and fleet['connection_type'] == 'HTTPS'
    assert 'first sign-in' in fleet['notes']
    assert next(item for item in entries if item['password'] == 'fleet-community')['connection_type'] == 'API'
    assert next(item for item in entries if item['password'] == 'sensor-community')['connection_type'] == 'API'
