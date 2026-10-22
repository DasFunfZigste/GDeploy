"""Shared credential organization, encryption, compatibility and concurrency."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from gdeploy.db import Database
from gdeploy.password_manager import CredentialError, OrganizationUpdate, PasswordManager
from test_password_manager import ENTRY, user_client


def section(client, name='Lab servers'):
    response = client.post('/api/password-manager/sections', json={'name': name})
    assert response.status_code == 201, response.text
    return response.json()


def credential(client, **values):
    response = client.post('/api/password-manager', json={**ENTRY, **values})
    assert response.status_code == 201, response.text
    return response.json()


def test_sections_tags_crud_masking_and_secret_free_organization(signed_in, config):
    saved = section(signed_in, '  Lab   servers  ')
    assert saved['name'] == 'Lab servers'
    entry = credential(signed_in, section_id=saved['id'], tags=[' Linux ', 'LINUX', 'home   lab'])
    assert entry['tags'] == ['linux', 'home lab']
    catalog = signed_in.get('/api/password-manager').json()
    assert catalog['sections'][0]['count'] == 1 and catalog['tags'] == ['home lab', 'linux']
    assert ENTRY['password'] not in str(catalog) and 'private_key' not in catalog['entries'][0]
    assert signed_in.put('/api/password-manager/sections/' + saved['id'], json={'name': 'Network', 'revision': 1}).status_code == 200
    assert signed_in.put('/api/password-manager/sections/' + saved['id'], json={'name': 'Stale', 'revision': 1}).status_code == 409
    organized = signed_in.patch(f"/api/password-manager/{entry['id']}/organization", json={'revision': 1, 'tags': ['Edge']})
    assert organized.status_code == 200
    assert organized.json()['section_id'] == saved['id'] and organized.json()['tags'] == ['edge']
    assert ENTRY['password'] not in organized.text and ENTRY['notes'] not in organized.text
    assert signed_in.delete(f"/api/password-manager/sections/{saved['id']}?revision=1").status_code == 409
    removed = signed_in.delete(f"/api/password-manager/sections/{saved['id']}?revision=2")
    assert removed.status_code == 200 and removed.json()['entries_retained'] == 1
    details = signed_in.post(f"/api/password-manager/{entry['id']}/reveal").json()
    assert details['section_id'] is None and details['tags'] == ['edge'] and details['revision'] == 3
    assert all(details[key] == ENTRY[key] for key in ENTRY)
    assert PasswordManager(Database(config.data_dir, config.secret_key)).list('admin')[0]['tags'] == ['edge']
    with signed_in.app.state.db.connect() as c:
        assert sum('entry revealed' in row[0] for row in c.execute('SELECT action FROM audit')) == 1


def test_organization_stays_encrypted_and_out_of_audits(signed_in, config):
    saved = section(signed_in, 'Private organization label')
    entry = credential(signed_in, section_id=saved['id'], tags=['private-tag-value'])
    db = Database(config.data_dir, config.secret_key)
    with db.connect() as c:
        values = ' '.join(row[0] for row in c.execute('SELECT value FROM credential_entries'))
        sections = ' '.join(row[0] for row in c.execute('SELECT value FROM credential_sections'))
        audit = ' '.join(row[0] for row in c.execute('SELECT action FROM audit'))
    assert 'private-tag-value' not in values and 'Private organization label' not in sections
    assert 'private-tag-value' not in audit and 'Private organization label' not in audit
    assert PasswordManager(db).catalog('admin')['sections'][0]['id'] == saved['id']
    assert PasswordManager(db).list('admin')[0]['id'] == entry['id']


@pytest.mark.parametrize('role,can_edit', [('administrator', True), ('credential_editor', True), ('credential_viewer', False)])
def test_roles_enforce_organization_permissions(signed_in, role, can_edit):
    saved = section(signed_in)
    entry = credential(signed_in)
    visitor, _ = user_client(signed_in, role)
    assert visitor.get('/api/password-manager').json()['sections'][0]['name'] == 'Lab servers'
    expected = 200 if can_edit else 403
    assert visitor.post('/api/password-manager/sections', json={'name': 'Other'}).status_code == (201 if can_edit else 403)
    assert visitor.put('/api/password-manager/sections/' + saved['id'], json={'name': 'Renamed', 'revision': 1}).status_code == expected
    assert visitor.patch(f"/api/password-manager/{entry['id']}/organization", json={'revision': 1, 'section_id': saved['id'], 'tags': ['new']}).status_code == expected
    assert visitor.delete(f"/api/password-manager/sections/{saved['id']}?revision={2 if can_edit else 1}").status_code == expected


def test_new_mutations_require_authentication_csrf_and_finished_account_setup(signed_in, config):
    saved = section(signed_in)
    entry = credential(signed_in)
    mutations = [('POST', '/api/password-manager/sections', {'name': 'New'}),
                 ('PUT', '/api/password-manager/sections/' + saved['id'], {'name': 'New', 'revision': 1}),
                 ('DELETE', f"/api/password-manager/sections/{saved['id']}?revision=1", None),
                 ('PATCH', f"/api/password-manager/{entry['id']}/organization", {'revision': 1, 'tags': []})]
    with TestClient(signed_in.app) as anonymous:
        for method, path, body in mutations:
            assert anonymous.request(method, path, json=body).status_code == 401
    visitor, _ = user_client(signed_in, 'credential_editor', setup=False)
    for method, path, body in mutations:
        assert visitor.request(method, path, json=body).status_code == 403
        assert signed_in.request(method, path, json=body, headers={'X-CSRF-Token': 'wrong'}).status_code == 403


@pytest.mark.parametrize('values', [{'tags': ['']}, {'tags': ['x' * 41]}, {'tags': ['x,y']}, {'tags': ['x\ny']},
                                  {'tags': [str(i) for i in range(21)]}, {'tags': 'linux'}, {'tags': [1]}, {'section_id': 'invalid'}])
def test_invalid_organization_is_rejected_without_echoing_secrets(signed_in, values):
    response = signed_in.post('/api/password-manager', json={**ENTRY, **values})
    assert response.status_code == 422 and ENTRY['password'] not in response.text
    assert signed_in.get('/api/password-manager').json()['entries'] == []


@pytest.mark.parametrize('name', ['', '   ', 'Unsectioned', ' ALL   SECTIONS ', 'line\nbreak', 'x' * 81])
def test_invalid_and_reserved_section_names(signed_in, name):
    assert signed_in.post('/api/password-manager/sections', json={'name': name}).status_code == 422


def test_duplicate_missing_and_stale_sections(signed_in):
    saved = section(signed_in)
    assert signed_in.post('/api/password-manager/sections', json={'name': ' lab SERVERS '}).status_code == 409
    other = section(signed_in, 'Network')
    assert signed_in.put('/api/password-manager/sections/' + other['id'], json={'name': 'Lab servers', 'revision': 1}).status_code == 409
    missing = str(uuid4())
    assert signed_in.post('/api/password-manager', json={**ENTRY, 'section_id': missing}).status_code == 409
    entry = credential(signed_in)
    assert signed_in.patch(f"/api/password-manager/{entry['id']}/organization", json={'revision': 1, 'section_id': missing}).status_code == 409
    assert signed_in.delete(f"/api/password-manager/sections/{missing}?revision=1").status_code == 404
    assert signed_in.delete(f"/api/password-manager/sections/{saved['id']}?revision=1").status_code == 200
    assert signed_in.patch(f"/api/password-manager/{entry['id']}/organization", json={'revision': 1, 'section_id': saved['id']}).status_code == 409


def test_provisioning_and_older_clients_preserve_organization(signed_in, spec, config):
    db = signed_in.app.state.db
    identifier = str(uuid4())
    name = spec['vms'][0]['name']
    secrets = {'vm_credentials': {name: {'username': 'gdeploy', 'password': 'original-os-password'}}}
    db.create(identifier, spec, secrets)
    entry = signed_in.get('/api/password-manager').json()['entries'][0]
    saved = section(signed_in)
    response = signed_in.patch(f"/api/password-manager/{entry['id']}/organization", json={'revision': entry['revision'], 'section_id': saved['id'], 'tags': ['LAB']})
    assert response.status_code == 200
    details = signed_in.post(f"/api/password-manager/{entry['id']}/reveal").json()
    old_fields = {key: details[key] for key in ENTRY}
    assert signed_in.put(f"/api/password-manager/{entry['id']}", json={**old_fields, 'revision': details['revision']}).status_code == 200
    vms = db.get(identifier)['vms']
    vms[0]['ip'] = '192.0.2.99'
    db.update(identifier, vms=vms)
    restarted = PasswordManager(Database(config.data_dir, config.secret_key)).reveal(entry['id'], 'admin')
    assert restarted['section_id'] == saved['id'] and restarted['tags'] == ['lab'] and restarted['host'] == '192.0.2.99'
    assert db.get(identifier, private=True)['secrets'] == secrets


def test_legacy_entries_have_unsectioned_untagged_defaults(signed_in):
    db = signed_in.app.state.db
    entry = credential(signed_in)
    with db.connect() as c:
        row = c.execute('SELECT value FROM credential_entries WHERE id=?', (entry['id'],)).fetchone()
        value = db.unseal(row[0])
        value.pop('section_id')
        value.pop('tags')
        c.execute('UPDATE credential_entries SET value=? WHERE id=?', (db.seal(value), entry['id']))
    catalog = signed_in.get('/api/password-manager').json()
    assert catalog['entries'][0]['section_id'] is None and catalog['entries'][0]['tags'] == []
    assert catalog['tags'] == [] and catalog['sections'] == []


def test_concurrent_section_removal_and_organization_leave_no_dangling_reference(signed_in):
    db = signed_in.app.state.db
    saved = section(signed_in)
    entry = credential(signed_in)
    vault = PasswordManager(db)
    def assign():
        try:
            vault.organize(entry['id'], OrganizationUpdate(revision=1, section_id=saved['id'], tags=['safe']), 'admin')
        except CredentialError as exc:
            assert exc.status_code == 409
    with ThreadPoolExecutor(max_workers=2) as pool:
        assigning = pool.submit(assign)
        deleting = pool.submit(vault.delete_section, saved['id'], 1, 'admin')
        assigning.result()
        deleting.result()
    assert vault.catalog('admin')['sections'] == []
    assert vault.reveal(entry['id'], 'admin')['section_id'] is None


def test_clearing_organization_keeps_secrets_and_removes_unused_tags(signed_in):
    saved = section(signed_in)
    item = credential(signed_in, section_id=saved['id'], tags=['remove-me'])
    response = signed_in.patch(f"/api/password-manager/{item['id']}/organization", json={'revision': 1, 'tags': [], 'section_id': None})
    assert response.status_code == 200 and response.json()['tags'] == [] and response.json()['section_id'] is None
    assert signed_in.patch(f"/api/password-manager/{item['id']}/organization", json={'revision': 1, 'tags': ['stale']}).status_code == 409
    catalog = signed_in.get('/api/password-manager').json()
    assert catalog['tags'] == [] and catalog['sections'][0]['count'] == 0
    details = signed_in.post(f"/api/password-manager/{item['id']}/reveal").json()
    assert all(details[key] == ENTRY[key] for key in ENTRY)
