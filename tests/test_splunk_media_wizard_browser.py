"""Configure Splunk and OS media inside the wizard; no licensed vendor payloads."""
import hashlib

import pytest

from gdeploy.os_profiles import ALMA10, UBUNTU
from test_splunk_native import deb_bytes, rpm_bytes
from test_wizard_drafts_browser import chromium, ui as wizard_ui, expect  # noqa: F401


@pytest.fixture
def ui(wizard_ui):  # noqa: F811
    return wizard_ui


def start(ui, profile=UBUNTU, target=None):
    ui.page.get_by_role('button', name='New deployment', exact=True).click()
    ui.wizard.locator('#deployment-name').fill('splunk-media-test')
    if target:
        ui.wizard.locator('#wizard-target-host').select_option(target)
    ui.wizard.locator('#wizard-os-profile').select_option(profile)
    ui.wizard.locator('#role-splunk').check()
    expect(ui.wizard.get_by_role('link', name='Configure installation media', exact=True)).to_have_count(0)
    expect(ui.wizard.get_by_role('link', name='Configure Splunk package', exact=True)).to_have_count(0)
    ui.next.click()
    ui.wizard.locator('#vm-splunk-network').select_option('Management')
    ui.next.click()
    expect(ui.wizard.get_by_role('heading', name='Installation media', exact=True)).to_be_visible()
    expect(ui.wizard.locator('#wizard-os-media-profile')).to_be_disabled()
    expect(ui.wizard.locator('#wizard-splunk-package-file')).to_have_attribute('accept', '.rpm,.tgz' if profile == ALMA10 else '.deb,.tgz')
    expect(ui.wizard.locator('#wizard-os-media-panel')).to_have_attribute('aria-busy', 'false')
    expect(ui.wizard.locator('#wizard-splunk-package-panel')).to_have_attribute('aria-busy', 'false')


def seed(ui, monkeypatch):
    monkeypatch.setattr('gdeploy.alma_guest.validate_install_iso', lambda *a, **kw: {'version': '10.1', 'arch': 'x86_64'})
    for fmt, body, profile in [('deb', deb_bytes(), UBUNTU), ('rpm', rpm_bytes(), ALMA10)]:
        path = ui.service.config.ubuntu_iso.with_name('splunk.' + fmt)
        path.write_bytes(body)
        ui.service.packages.select(ui.service.packages._server_id(path), hashlib.sha256(body).hexdigest(), os_profile=profile)
    source = ui.service.config.ubuntu_iso.with_name('AlmaLinux-10.1-x86_64-dvd.iso')
    source.write_bytes(ui.service.config.ubuntu_iso.read_bytes() + b'Alma fixture')
    item = next(item for item in ui.service.media.catalog()['items'] if item['name'] == source.name)
    ui.service.media.select(item['id'], hashlib.sha256(source.read_bytes()).hexdigest(), profile=ALMA10)


def no_duplicate_ids(ui):
    assert ui.page.evaluate('() => { const ids = [...document.querySelectorAll("[id]")].map(n => n.id); return ids.filter((id, i) => ids.indexOf(id) !== i); }') == []


def test_missing_media_uploads_and_auto_deploy_stay_inside_wizard(ui):
    package_body = deb_bytes()
    body = ui.service.config.ubuntu_iso.read_bytes()
    ui.service.config.ubuntu_iso.unlink()
    preflights = []
    ui.page.on('request', lambda request: preflights.append(request.url) if request.url.endswith('/api/preflight') else None)
    start(ui)
    expect(ui.next).to_be_disabled()
    no_duplicate_ids(ui)
    iso = ui.wizard.locator('#wizard-os-media-panel')
    iso.get_by_role('radio', name='Upload an ISO', exact=True).check()
    iso.locator('input[type=file]').set_input_files({'name': 'ubuntu-24.04.5-live-server-amd64.iso', 'mimeType': 'application/octet-stream', 'buffer': body})
    iso.locator('input[name=sha256]').fill(hashlib.sha256(body).hexdigest())
    iso.get_by_role('button', name='Upload & use ISO', exact=True).click()
    expect(iso.locator('.alert-success')).to_be_visible()
    expect(ui.next).to_be_disabled()
    pkg = ui.wizard.locator('#wizard-splunk-package-panel')
    pkg.get_by_role('radio', name='Upload a package', exact=True).check()
    pkg.locator('input[type=file]').set_input_files({'name': 'splunk.deb', 'mimeType': 'application/vnd.debian.binary-package', 'buffer': package_body})
    pkg.locator('input[name=checksum]').fill('0' * 128)
    pkg.get_by_role('button', name='Upload & use package', exact=True).click()
    expect(pkg.locator('.alert-error')).to_contain_text('publisher checksum')
    assert pkg.locator('input[type=file]').evaluate('node => node.files.length') == 1
    expect(ui.next).to_be_disabled()
    pkg.locator('input[name=checksum]').fill(hashlib.sha512(package_body).hexdigest())
    pkg.get_by_role('button', name='Upload & use package', exact=True).click()
    expect(pkg.locator('.alert-success')).to_be_visible()
    expect(ui.next).to_be_enabled()
    assert preflights == []
    assert '/#settings' not in ui.page.url
    ui.next.click()
    expect(ui.wizard.locator('#wizard-media-review')).to_contain_text('splunk.deb')
    ui.wizard.get_by_role('checkbox', name='I have reviewed', exact=False).check()
    ui.wizard.locator('#wizard-auto-deploy').check()
    ui.next.click()
    expect(ui.wizard).not_to_be_visible()
    expect(ui.page.locator('#deployment-detail-status')).to_have_text('Queued')
    job = ui.db.get(ui.db.list()[0]['id'], private=True)
    assert job['secrets']['splunk_package']['format'] == 'deb'
    assert job['secrets']['os_media']['source'] == 'upload'
    assert len(preflights) == 1


@pytest.mark.parametrize('profile,extension', [(UBUNTU, 'deb'), (ALMA10, 'rpm')])
def test_saved_native_is_automatically_matched_and_os_changes_refresh(ui, monkeypatch, profile, extension):
    seed(ui, monkeypatch)
    start(ui, profile)
    expect(ui.next).to_be_enabled()
    pkg = ui.wizard.locator('#wizard-splunk-package-panel')
    expect(pkg.locator('.media-summary-heading strong')).to_have_text('splunk.' + extension)
    assert 'splunk.' + ('rpm' if extension == 'deb' else 'deb') not in pkg.locator('select').inner_text()
    no_duplicate_ids(ui)
    ui.back.click()
    ui.back.click()
    other = ALMA10 if profile == UBUNTU else UBUNTU
    ui.wizard.locator('#wizard-os-profile').select_option(other)
    ui.next.click()
    ui.next.click()
    expect(ui.next).to_be_enabled()
    expect(ui.wizard.locator('#wizard-splunk-package-summary')).to_contain_text('splunk.' + ('rpm' if extension == 'deb' else 'deb'))
    expect(ui.wizard.locator('#wizard-os-media-profile')).to_have_value(other)
    no_duplicate_ids(ui)


def test_unverified_candidate_survives_back_validation_and_cannot_continue(ui, monkeypatch):
    body = deb_bytes()
    seed(ui, monkeypatch)
    start(ui)
    expect(ui.next).to_be_enabled()
    pkg = ui.wizard.locator('#wizard-splunk-package-panel')
    pkg.get_by_role('radio', name='Upload a package', exact=True).check()
    pkg.locator('input[type=file]').set_input_files({'name': 'splunk-new.deb', 'mimeType': 'application/octet-stream', 'buffer': body})
    pkg.locator('input[name=checksum]').fill(hashlib.sha512(body).hexdigest())
    expect(ui.next).to_be_disabled()
    ui.back.click()
    ui.wizard.locator('#vm-splunk-cpu').fill('0')
    ui.next.click()
    expect(ui.wizard.locator('.vm-validation-error')).to_be_visible()
    ui.wizard.locator('#vm-splunk-cpu').fill('4')
    ui.next.click()
    pkg = ui.wizard.locator('#wizard-splunk-package-panel')
    assert pkg.locator('input[type=file]').evaluate('node => node.files[0].name') == 'splunk-new.deb'
    expect(pkg.locator('input[name=checksum]')).to_have_value(hashlib.sha512(body).hexdigest())
    expect(ui.next).to_be_disabled()
    pkg.get_by_role('button', name='Upload & use package', exact=True).click()
    expect(ui.next).to_be_enabled()


def test_server_media_is_verified_in_wizard_and_failed_preflight_links_return_here(ui, monkeypatch):
    seed(ui, monkeypatch)
    ui.service.packages.clear_selection()
    start(ui)
    pkg = ui.wizard.locator('#wizard-splunk-package-panel')
    pkg.get_by_role('radio', name='GDeploy server', exact=True).check()
    option = pkg.locator('select option').filter(has_text='splunk.deb')
    pkg.locator('select').select_option(option.get_attribute('value'))
    pkg.locator('input[name=checksum]').fill(hashlib.sha512((ui.service.packages.server_dir / 'splunk.deb').read_bytes()).hexdigest())
    expect(ui.next).to_be_disabled()
    pkg.get_by_role('button', name='Use selected package', exact=True).click()
    expect(ui.next).to_be_enabled()
    ui.next.click()
    ui.wizard.get_by_role('checkbox', name='I have reviewed', exact=False).check()
    ui.service.packages.clear_selection()
    ui.next.click()
    ui.wizard.get_by_role('button', name='Edit installation media', exact=True).click()
    expect(ui.wizard.get_by_role('heading', name='Installation media', exact=True)).to_be_visible()
    expect(ui.next).to_be_disabled()
    assert '/#settings' not in ui.page.url


def test_proxmox_wizard_excludes_esxi_source_and_mobile_scroll_is_contained(ui, monkeypatch):
    seed(ui, monkeypatch)
    ui.db.set_target_host('pve-test', {'id': 'pve-test', 'name': 'Proxmox test', 'hypervisor': 'proxmox',
        'host': 'pve.test', 'node': 'pve', 'iso_storage': 'local', 'username': 'root@pam', 'password': 'fixture-token'})
    ui.page.set_viewport_size({'width': 390, 'height': 844})
    start(ui, target='pve-test')
    expect(ui.wizard.locator('#wizard-os-media-panel').get_by_role('radio', name='ESXi datastore', exact=True)).not_to_be_visible()
    expect(ui.next).to_be_enabled()
    no_duplicate_ids(ui)
    assert ui.wizard.evaluate('node => node.scrollWidth <= node.clientWidth + 1')


def test_named_esxi_import_in_wizard_uses_target_and_browsing_recovers_after_back(ui, monkeypatch):
    from test_wizard_drafts_browser import InventoryClient
    seed(ui, monkeypatch)
    target = {'id': 'second-esxi', 'name': 'Second host', 'hypervisor': 'esxi', 'host': 'second-esxi.test',
              'username': 'deploy', 'password': 'target-fixture-secret'}
    ui.db.set_target_host(target['id'], target)
    source = ui.service.config.ubuntu_iso.read_bytes()
    calls = []

    class Client(InventoryClient):
        def browse_iso_media(self, datastore, folder=''):
            return {'datastore': 'fast-ssd', 'folder': '', 'parent': None, 'folders': [],
                    'files': [{'name': 'target-ubuntu.iso', 'path': 'target-ubuntu.iso', 'size_bytes': len(source)}]}

        def download_iso(self, datastore, path, destination, **kwargs):
            destination.write_bytes(source)
            return len(source)

    def client(settings):
        calls.append(settings['host'])
        return Client()

    monkeypatch.setattr(ui.service, 'client', client)
    start(ui, target=target['id'])
    calls.clear()
    routes = []
    pattern = '**/api/settings/media/esxi?*'
    ui.page.route(pattern, lambda route: routes.append(route))
    iso = ui.wizard.locator('#wizard-os-media-panel')
    iso.get_by_role('radio', name='ESXi datastore', exact=True).check()
    expect(iso.get_by_role('button', name='Loading…', exact=True)).to_be_visible()
    ui.back.click()
    assert len(routes) == 1
    routes[0].fulfill(json={'host': target['host'], 'datastores': [{'name': 'fast-ssd'}], **Client().browse_iso_media('fast-ssd')})
    ui.page.unroute(pattern)
    ui.next.click()
    expect(iso.get_by_role('button', name='Refresh folder', exact=True)).to_be_enabled()
    with ui.page.expect_request(pattern) as request:
        iso.get_by_role('button', name='Refresh folder', exact=True).click()
    assert 'target_host_id=second-esxi' in request.value.url
    option = iso.locator('#wizard-os-media-esxi-file option').filter(has_text='target-ubuntu.iso')
    expect(option).to_have_count(1)
    iso.locator('#wizard-os-media-esxi-file').select_option('target-ubuntu.iso')
    expect(ui.next).to_be_disabled()
    iso.locator('input[name=sha256]').fill(hashlib.sha256(source).hexdigest())
    iso.get_by_role('button', name='Copy & use ISO', exact=True).click()
    expect(iso.locator('.alert-success')).to_be_visible()
    expect(ui.next).to_be_enabled()
    assert set(calls) == {target['host']}
    assert ui.service.media.selected(UBUNTU)['origin']['host'] == target['host']


def test_splunk_media_precedes_fleet_configuration_and_keeps_its_secret_draft(ui, monkeypatch):
    seed(ui, monkeypatch)
    ui.page.get_by_role('button', name='New deployment', exact=True).click()
    ui.wizard.locator('#deployment-name').fill('splunk-and-fleet')
    ui.wizard.locator('#wizard-os-profile').select_option(UBUNTU)
    for role in ['splunk', 'fleetmanager']:
        ui.wizard.locator('#role-' + role).check()
    ui.next.click()
    for role in ['splunk', 'fleetmanager']:
        ui.wizard.locator('#vm-' + role + '-network').select_option('Management')
    ui.next.click()
    expect(ui.wizard.get_by_role('heading', name='Installation media', exact=True)).to_be_visible()
    expect(ui.next).to_be_enabled()
    ui.next.click()
    ui.role, ui.profile = 'fleetmanager', UBUNTU
    ui.configuration()
    ui.fill()
    ui.back.click()
    expect(ui.wizard.get_by_role('heading', name='Installation media', exact=True)).to_be_visible()
    expect(ui.next).to_be_enabled()
    ui.next.click()
    ui.configuration()
    ui.assert_values()
    ui.save_review()
    expect(ui.wizard.locator('#wizard-media-review')).to_contain_text('splunk.deb')
