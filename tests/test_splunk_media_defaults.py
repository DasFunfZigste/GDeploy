"""OS-matched verified defaults, legacy migration, and queued snapshots."""
import asyncio
import hashlib
from pathlib import Path

import pytest

from gdeploy.db import Database, PackageStateError
from gdeploy.os_profiles import ALMA10, UBUNTU
from gdeploy.packages import PackageError, SplunkPackageManager
from test_splunk_native import Request, deb_bytes, rpm_bytes


@pytest.fixture
def manager(config):
    return SplunkPackageManager(Database(config.data_dir, config.secret_key), config)


def upload(manager, fmt):
    body = deb_bytes() if fmt == 'deb' else rpm_bytes()
    profile = UBUNTU if fmt == 'deb' else ALMA10
    return asyncio.run(manager.upload(Request(body), 'splunk.' + fmt, hashlib.sha256(body).hexdigest(), os_profile=profile))['selected']


@pytest.mark.parametrize('order', [('deb', 'rpm'), ('rpm', 'deb')])
def test_both_defaults_survive_restart_and_are_protected(manager, config, order):
    choices = {fmt: upload(manager, fmt) for fmt in order}
    reopened = SplunkPackageManager(Database(config.data_dir, config.secret_key), config)
    assert reopened.selected() == choices[order[-1]]
    for profile, fmt in [(UBUNTU, 'deb'), (ALMA10, 'rpm')]:
        assert reopened.selected(profile) == choices[fmt]
        catalog = reopened.catalog(profile)
        assert catalog['ready'] and catalog['selected'] == choices[fmt]
        assert all(item['saved_default'] and not item['can_delete'] for item in catalog['items'])
        with pytest.raises(PackageStateError, match='saved selection'):
            reopened.delete(choices[fmt]['id'])
    with reopened.db.connect() as connection:
        encrypted = ''.join(row[0] for row in connection.execute('SELECT value FROM splunk_package_format_settings'))
        assert str(config.data_dir) not in encrypted
    reopened.clear_selection('rpm')
    assert reopened.selected(UBUNTU) == choices['deb']
    assert reopened.selected(ALMA10) is None
    reopened.delete(choices['rpm']['id'])
    assert Path(choices['deb']['path']).exists()
    reopened.clear_selection()
    reopened.delete(choices['deb']['id'])
    assert reopened.catalog()['items'] == []


def test_legacy_singleton_is_retained_when_other_format_is_saved(manager, config):
    deb = upload(manager, 'deb')
    deb.pop('format')
    with manager.db.connect() as connection:
        connection.execute('DELETE FROM splunk_package_format_settings')
        connection.execute('UPDATE splunk_package_settings SET value=?', (manager.db.seal(deb),))
    rpm = upload(manager, 'rpm')
    assert manager.selected(UBUNTU) == deb
    assert manager.selected(ALMA10) == rpm
    assert set(manager.db.splunk_package_format_settings()) == {'deb', 'rpm'}


def legacy_restart(manager, config):
    with manager.db.connect() as connection:
        connection.execute('DELETE FROM splunk_package_format_settings')
    return SplunkPackageManager(Database(config.data_dir, config.secret_key), config)


@pytest.mark.parametrize('order', [('deb', 'rpm'), ('rpm', 'deb')])
def test_old_uploads_are_adopted_without_reselection_and_stay_cleared(manager, config, order):
    choices = {fmt: upload(manager, fmt) for fmt in order}
    reopened = legacy_restart(manager, config)
    assert reopened.selected() == choices[order[-1]]
    for profile, fmt in [(UBUNTU, 'deb'), (ALMA10, 'rpm')]:
        assert reopened.selected(profile) == choices[fmt]
        catalog = reopened.catalog(profile)
        assert catalog['ready'] and catalog['profile_selections'][profile]['ready']
        assert all(item['saved_default'] and not item['can_delete'] for item in catalog['items'])
    reopened.clear_selection('deb')
    reopened = SplunkPackageManager(Database(config.data_dir, config.secret_key), config)
    assert reopened.selected(UBUNTU) is None
    reopened.delete(choices['deb']['id'])
    reopened.clear_selection()
    reopened = SplunkPackageManager(Database(config.data_dir, config.secret_key), config)
    assert reopened.selected(ALMA10) is None
    reopened.delete(choices['rpm']['id'])


def test_legacy_adoption_does_not_guess_between_versions_or_mounted_files(manager, config):
    first = upload(manager, 'deb')
    second = upload(manager, 'deb')
    rpm = upload(manager, 'rpm')
    config.splunk_package.with_name('splunk.deb').write_bytes(deb_bytes())
    reopened = legacy_restart(manager, config)
    assert reopened.selected(UBUNTU) is None
    assert reopened.selected(ALMA10) == rpm
    reopened.delete(first['id'])
    reopened = SplunkPackageManager(Database(config.data_dir, config.secret_key), config)
    assert reopened.selected(UBUNTU) is None  # Deleting a version must not change the choice.
    assert Path(second['path']).exists()


@pytest.mark.parametrize('change', ['missing', 'unverified'])
def test_legacy_adoption_never_marks_bad_upload_ready(manager, config, change):
    deb = upload(manager, 'deb')
    upload(manager, 'rpm')
    if change == 'missing':
        Path(deb['path']).unlink()
    else:
        deb['sha256'] = ''
        with manager.db.connect() as connection:
            connection.execute('UPDATE splunk_package_files SET value=? WHERE id=?', (manager.db.seal(deb), deb['id']))
    reopened = legacy_restart(manager, config)
    assert not reopened.catalog(UBUNTU)['ready']


def test_existing_explicit_clear_is_not_reversed_during_upgrade(manager, config):
    upload(manager, 'deb')
    upload(manager, 'rpm')
    manager.clear_selection('deb')
    reopened = SplunkPackageManager(Database(config.data_dir, config.secret_key), config)
    assert reopened.selected(UBUNTU) is None
    manager.clear_selection()
    reopened = legacy_restart(manager, config)
    assert reopened.selected(ALMA10) is None


def test_broken_native_default_is_reported_without_silent_fallback(manager):
    from test_packages_api import package_body
    body = package_body()
    archive = asyncio.run(manager.upload(Request(body), 'splunk.tgz', hashlib.sha256(body).hexdigest()))['selected']
    deb = upload(manager, 'deb')
    Path(deb['path']).unlink()
    assert manager.selected(UBUNTU) == deb
    assert not manager.catalog(UBUNTU)['ready']
    with pytest.raises(PackageError):
        manager.validate_snapshot(deb, UBUNTU)
    manager.clear_selection('deb')
    assert manager.selected(UBUNTU) == archive and manager.catalog(UBUNTU)['ready']


@pytest.mark.parametrize('fmt,profile', [('rpm', UBUNTU), ('deb', ALMA10)])
def test_profile_mismatch_rejected_before_writing(manager, fmt, profile):
    body = deb_bytes() if fmt == 'deb' else rpm_bytes()
    with pytest.raises(PackageError, match='Select .deb'):
        asyncio.run(manager.upload(Request(body), 'splunk.' + fmt, hashlib.sha256(body).hexdigest(), os_profile=profile))
    assert manager.db.splunk_package_files() == []
    assert manager.db.splunk_package_format_settings() == {}


@pytest.mark.parametrize('fmt,profile', [('deb', UBUNTU), ('rpm', ALMA10)])
def test_profile_scoped_api_and_wrong_profile_cannot_change_defaults(signed_in, fmt, profile):
    body = deb_bytes() if fmt == 'deb' else rpm_bytes()
    params = {'filename': 'splunk.' + fmt, 'sha256': hashlib.sha256(body).hexdigest(), 'profile': profile}
    response = signed_in.post('/api/settings/splunk-package/upload', params=params, content=body)
    assert response.status_code == 200, response.text
    selected = response.json()['selected']
    settings = signed_in.get('/api/settings').json()
    assert settings['splunk_package_profiles'][profile]['selected']['name'] == 'splunk.' + fmt
    scoped = signed_in.get('/api/settings/splunk-package', params={'profile': profile}).json()
    assert scoped['selected'] == selected and scoped['ready']
    wrong = ALMA10 if profile == UBUNTU else UBUNTU
    response = signed_in.put('/api/settings/splunk-package', json={'package_id': selected['id'], 'sha256': params['sha256'], 'profile': wrong})
    assert response.status_code == 400
    assert signed_in.get('/api/settings/splunk-package', params={'profile': wrong}).json()['selected'] is None
    assert signed_in.delete('/api/settings/splunk-package', params={'package_format': 'exe'}).status_code == 422
    assert signed_in.app.state.packages.selected(profile) == selected
