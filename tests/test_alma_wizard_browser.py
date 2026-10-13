"""Real browser flows for Alma media, experimental warnings and RPM choices."""
import pytest

from gdeploy.os_profiles import ALMA10, UBUNTU
from test_wizard_drafts_browser import chromium, ui as wizard_ui, expect  # noqa: F401


@pytest.fixture
def ui(wizard_ui):  # noqa: F811
    return wizard_ui


def choose_os(ui, monkeypatch, profile, *, return_to_overview=True):
    monkeypatch.setattr('gdeploy.alma_guest.validate_install_iso', lambda *a, **kw: {'version': '10.1', 'arch': 'x86_64'})
    ui.page.goto(ui.base + '/#settings/media')
    picker = ui.page.locator('#os-media-profile')
    expect(picker).to_be_enabled()
    picker.select_option(profile)
    ui.page.get_by_role('button', name='Use selected ISO', exact=True).click()
    expect(ui.page.locator('#os-media-panel .alert-success')).to_be_visible()
    assert ui.db.media_settings()['profile'] == profile
    if return_to_overview:
        ui.page.goto(ui.base + '/#deployments')
        expect(ui.page.get_by_role('button', name='New deployment', exact=True)).to_be_visible()


def test_alma_fleet_wizard_uses_rpm_versions_warning_and_frozen_profile(ui, monkeypatch):
    choose_os(ui, monkeypatch, ALMA10)
    lookup_requests = []
    def versions(route):
        lookup_requests.append(route.request.post_data_json)
        route.fulfill(json={'versions': ['0:29.2.2-1.x86_64', '0:29.2.1-1.x86_64']})
    ui.page.route('**/api/settings/fleetmanager/versions', versions)
    ui.open('fleetmanager', name='alma-fleet-lab', splunk=False, address='192.0.2.40/24')
    expect(ui.wizard.get_by_text('Experimental Corelight compatibility', exact=True)).to_be_visible()
    ui.page.locator('#fleetmanager-community').fill('private-alma-community')
    ui.page.locator('#fleetmanager-token').fill('private-alma-token')
    ui.page.locator('#fleetmanager-license').set_input_files(ui.license_path)
    ui.page.locator('#fleetmanager-load-versions').click()
    expect(ui.page.locator('#fleetmanager-online-version option')).to_have_count(3)
    ui.page.locator('#fleetmanager-online-version').select_option('0:29.2.1-1.x86_64')
    assert lookup_requests[-1]['os_profile'] == ALMA10
    ui.next.click()
    expect(ui.next).to_have_text('Run preflight')
    expect(ui.wizard.get_by_text('Experimental Corelight compatibility', exact=True)).to_be_visible()
    expect(ui.wizard.locator('#fleetmanager-review')).to_contain_text('0:29.2.1-1.x86_64')
    ui.next.click()
    expect(ui.next).to_have_text('Deploy 1 VM')
    expect(ui.wizard.locator('.preflight-check.warning')).to_contain_text('Corelight AlmaLinux 10 compatibility')
    ui.next.click()
    expect(ui.wizard).not_to_be_visible()
    jobs = [job for job in ui.db.list() if job['name'] == 'alma-fleet-lab']
    assert len(jobs) == 1
    job = ui.db.get(jobs[0]['id'], private=True)
    assert job['secrets']['os_media']['profile'] == ALMA10
    assert job['spec']['os_profile'] == ALMA10
    assert job['secrets']['fleetmanager']['online_version_rpm'] == '0:29.2.1-1.x86_64'
    for secret in ('private-alma-community', 'private-alma-token'):
        assert secret not in ui.page.content()


def test_setup_os_change_preserves_secrets_and_uses_separate_fleet_version_choices(ui, monkeypatch):
    ui.service.fleetmanager.save({'mode': 'online', 'community_string': 'saved-community', 'repository_token': 'saved-token', 'license_pem': ui.license_path.read_text(), 'license_name': ui.license_path.name, 'online_version': '29.2.2-1', 'online_version_rpm': '0:29.2.1-1.x86_64'})
    calls = []
    def versions(route):
        body = route.request.post_data_json
        calls.append(body)
        route.fulfill(json={'versions': ['0:29.2.1-1.x86_64'] if body.get('os_profile') == ALMA10 else ['29.2.2-1']})
    ui.page.route('**/api/settings/fleetmanager/versions', versions)
    choose_os(ui, monkeypatch, ALMA10)
    ui.page.goto(ui.base + '/#settings/packages/fleetmanager')
    version = ui.page.locator('#fleetmanager-online-version')
    expect(version).to_have_value('0:29.2.1-1.x86_64')
    ui.page.get_by_role('button', name='Save FleetManager setup', exact=True).click()
    expect(ui.page.locator('#fleetmanager-panel .alert-success')).to_be_visible()
    assert ui.db.fleetmanager_settings()['online_version'] == '29.2.2-1'
    choose_os(ui, monkeypatch, UBUNTU)
    ui.page.goto(ui.base + '/#settings/packages/fleetmanager')
    expect(version).to_have_value('29.2.2-1')
    assert ui.db.fleetmanager_settings()['online_version_rpm'] == '0:29.2.1-1.x86_64'
    for secret in ('saved-token', 'saved-community'):
        assert secret not in ui.page.content()
    assert {call['os_profile'] for call in calls} == {ALMA10, UBUNTU}


def test_alma_os_only_preview_and_sensor_warning(ui, monkeypatch):
    choose_os(ui, monkeypatch, ALMA10)
    preview = ui.page.locator('#preview-navigation')
    if not preview.count():
        preview = ui.page.locator('.sidebar-preview')
    assert 'Alma Linux' not in ui.page.locator('aside').inner_text()
    ui.page.get_by_role('button', name='New deployment', exact=True).click()
    expect(ui.page.locator('#wizard-os-profile')).to_contain_text('AlmaLinux 10')
    ui.page.locator('#deployment-name').fill('alma-sensor-warning')
    ui.page.locator('#role-corelight_sensor').check()
    expect(ui.wizard.get_by_text('Experimental Corelight compatibility', exact=True)).to_be_visible()
    assert ui.page.evaluate('JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})') == '{"local":{},"session":{}}'
    ui.page.locator('#role-corelight_sensor').uncheck()
    ui.page.locator('#role-ubuntu').check()
    expect(ui.wizard.get_by_text('Experimental Corelight compatibility', exact=True)).not_to_be_visible()
    ui.next.click()
    ui.page.locator('#vm-ubuntu-network').select_option('Management')
    ui.next.click()
    ui.page.locator('#wizard-auto-deploy').check()
    ui.next.click()
    expect(ui.wizard).not_to_be_visible()
    jobs = [job for job in ui.db.list() if job['name'] == 'alma-sensor-warning']
    assert len(jobs) == 1
    assert ui.db.get(jobs[0]['id'], private=True)['secrets']['os_media']['profile'] == ALMA10


def test_resumed_sensor_draft_updates_os_warning_without_clearing_pairing_values(ui, monkeypatch):
    ui.open('corelight_sensor', name='sensor-switch-os')
    ui.fill()
    ui.suspend()
    choose_os(ui, monkeypatch, ALMA10, return_to_overview=False)
    ui.resume()
    ui.assert_values()
    expect(ui.wizard.locator('#corelight-sensor-panel .surface-header')).to_contain_text('AlmaLinux 10')
    expect(ui.wizard.get_by_text('Experimental Corelight compatibility', exact=True)).to_be_visible()
