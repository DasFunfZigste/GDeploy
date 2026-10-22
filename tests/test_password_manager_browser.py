"""Real GUI credential lifecycle and account/role flows against SQLite/FastAPI."""
from uuid import uuid4

import pytest

from gdeploy.accounts import Accounts, UserInput
from test_password_manager import ENTRY, TEMP, CHOSEN
from test_wizard_drafts_browser import chromium, ui as wizard_ui, expect, PASSWORD  # noqa: F401


@pytest.fixture
def ui(wizard_ui):  # noqa: F811
    return wizard_ui


def open_manager(ui):
    ui.page.get_by_role('link', name='Password Manager', exact=True).click()
    expect(ui.page.get_by_role('heading', name='Password Manager', exact=True)).to_be_visible()
    expect(ui.page.locator('#credential-list')).to_be_visible()


def fill_entry(ui, title='My lab desktop'):
    dialog = ui.page.locator('#password-manager-dialog')
    dialog.locator('#credential-title').fill(title)
    dialog.locator('#credential-application').fill('Lab Linux')
    dialog.locator('#credential-username').fill('homelab')
    dialog.locator('#credential-password').fill('browser-private-password')
    dialog.locator('#credential-host').fill('192.0.2.21')
    dialog.locator('#credential-port').fill('22')
    dialog.locator('#credential-notes').fill('A private note')
    return dialog


def seed(ui):
    return ui.page.request.post(ui.base + '/api/password-manager', data=ENTRY,
        headers={'X-CSRF-Token': ui.page.request.get(ui.base + '/api/session').json()['csrf_token']}).json()


def test_manual_entry_reveal_copy_edit_search_and_delete(ui, tmp_path):
    open_manager(ui)
    ui.page.get_by_role('button', name='Add credential', exact=True).click()
    dialog = fill_entry(ui)
    dialog.get_by_role('button', name='Save entry', exact=True).click()
    expect(dialog).not_to_be_visible()
    card = ui.page.locator('.vault-card').filter(has_text='My lab desktop')
    expect(card).to_be_visible()
    expect(ui.page.get_by_text('browser-private-password', exact=True)).to_have_count(0)
    ui.page.evaluate("() => { document.execCommand = () => { window.copied = document.activeElement.value; return true; }; Object.defineProperty(navigator, 'clipboard', {value: undefined, configurable: true}); }")
    card.get_by_role('button', name='Reveal credentials', exact=True).click()
    expect(card.get_by_text('browser-private-password', exact=True)).to_be_visible()
    card.get_by_role('button', name='Copy password', exact=True).click()
    assert ui.page.evaluate('window.copied') == 'browser-private-password'
    card.get_by_role('button', name='Hide credentials', exact=True).click()
    expect(card.get_by_text('browser-private-password', exact=True)).to_have_count(0)
    card.get_by_role('button', name='Edit', exact=True).click()
    expect(dialog.locator('#credential-password')).to_have_value('browser-private-password')
    dialog.locator('#credential-password').fill('edited-browser-password')
    dialog.locator('#credential-connection-type').select_option('RDP')
    dialog.locator('#credential-port').fill('3389')
    dialog.get_by_role('button', name='Save entry', exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(card).to_contain_text('192.0.2.21:3389')
    ui.page.locator('#credential-search').fill('does-not-match')
    expect(ui.page.locator('.vault-card')).to_have_count(0)
    ui.page.locator('#credential-search').fill('homelab')
    expect(ui.page.locator('.vault-card')).to_have_count(1)
    screenshot = tmp_path / 'password-manager-desktop.png'
    ui.page.screenshot(path=str(screenshot), full_page=True)
    print('DESKTOP_SCREENSHOT', screenshot)
    card.get_by_role('button', name='Delete', exact=True).click()
    dialog.get_by_role('button', name='Delete credential', exact=True).click()
    expect(ui.page.locator('.vault-card')).to_have_count(0)


def test_provisioned_credentials_are_listed_masked_and_survive_hidden_history(ui):
    identifier = str(uuid4())
    spec = {'name': 'Provisioned lab', 'vms': [{'name': 'lab-ubuntu', 'role': 'ubuntu', 'address': None}]}
    ui.db.create(identifier, spec, {'vm_credentials': {'lab-ubuntu': {'username': 'gdeploy', 'password': 'provisioned-os-secret', 'private_key': 'PRIVATE-AUTOMATION-KEY'}}})
    job = ui.db.get(identifier)
    job['vms'][0].update(ip='192.0.2.24', status='completed')
    ui.db.update(identifier, vms=job['vms'], status='completed')
    ui.db.set_visibility(identifier, True)
    open_manager(ui)
    card = ui.page.locator('.vault-card')
    expect(card).to_contain_text('lab-ubuntu · OS')
    expect(card).to_contain_text('192.0.2.24:22')
    expect(card.get_by_text('provisioned-os-secret', exact=True)).to_have_count(0)
    card.get_by_role('button', name='Reveal credentials', exact=True).click()
    expect(card.get_by_text('PRIVATE-AUTOMATION-KEY', exact=True)).to_be_visible()
    card.get_by_role('link', name='View deployment', exact=True).click()
    expect(ui.page.locator('#page .vault-secrets pre')).to_have_count(0)


@pytest.mark.parametrize('role,edit', [('credential_viewer', False), ('credential_editor', True)])
def test_additional_user_first_login_and_restricted_navigation(ui, role, edit):
    seed(ui)
    Accounts(ui.db).save(UserInput(username='browser-user', role=role, password=TEMP), 'admin')
    ui.page.get_by_role('button', name='Sign out', exact=True).click()
    ui.page.locator('#login-username').fill('browser-user')
    ui.page.locator('#login-password').fill(TEMP)
    ui.page.get_by_role('button', name='Sign in', exact=False).click()
    expect(ui.page.get_by_role('heading', name='Choose your own password', exact=True)).to_be_visible()
    expect(ui.page.locator('#setup-username')).to_have_value('browser-user')
    expect(ui.page.locator('#setup-username')).to_have_attribute('readonly', '')
    ui.page.locator('#setup-password').fill(CHOSEN)
    ui.page.locator('#setup-confirm').fill(CHOSEN)
    ui.page.get_by_role('button', name='Save account password', exact=False).click()
    expect(ui.page.locator('#login-screen')).to_be_visible()
    ui.page.locator('#login-password').fill(CHOSEN)
    ui.page.get_by_role('button', name='Sign in', exact=False).click()
    expect(ui.page.locator('#credential-list')).to_be_visible()
    expect(ui.page.get_by_role('link', name='Setup', exact=True)).not_to_be_visible()
    expect(ui.page.get_by_role('link', name='Deployments', exact=True)).not_to_be_visible()
    expect(ui.page.get_by_role('link', name='Users & roles', exact=True)).to_have_count(0)
    expect(ui.page.get_by_role('button', name='Add credential', exact=True)).to_have_count(1 if edit else 0)
    card = ui.page.locator('.vault-card')
    expect(card.get_by_role('button', name='Edit', exact=True)).to_have_count(1 if edit else 0)
    card.get_by_role('button', name='Reveal credentials', exact=True).click()
    expect(card.get_by_text(ENTRY['password'], exact=True)).to_be_visible()
    ui.page.evaluate("location.hash = 'settings'")
    expect(ui.page).to_have_url(ui.base + '/#password-manager')
    expect(ui.page.get_by_text(ENTRY['password'], exact=True)).to_have_count(0)
    assert ui.page.request.get(ui.base + '/api/settings').status == 403
    expect(ui.page.locator('.vault-card').get_by_role('button', name='Organize', exact=True)).to_have_count(1 if edit else 0)
    ui.page.get_by_role('link', name='Sections', exact=True).click()
    expect(ui.page).to_have_url(ui.base + '/#password-manager/sections')
    expect(ui.page.get_by_role('button', name='Add section', exact=True)).to_have_count(1 if edit else 0)
    expect(ui.page.get_by_role('link', name='Users & roles', exact=True)).to_have_count(0)


def test_admin_user_creation_roles_disable_and_delete(ui):
    open_manager(ui)
    ui.page.get_by_role('link', name='Users & roles', exact=True).click()
    ui.page.get_by_role('button', name='Add user', exact=True).click()
    dialog = ui.page.locator('#password-manager-dialog')
    dialog.locator('#user-username').fill('gui-created-user')
    dialog.locator('#user-password').fill(TEMP)
    dialog.locator('#user-role').select_option('credential_editor')
    dialog.get_by_role('button', name='Save user', exact=True).click()
    expect(dialog).not_to_be_visible()
    row = ui.page.locator('.vault-user-row').filter(has_text='gui-created-user')
    expect(row).to_contain_text('Credential Editor')
    row.get_by_role('button', name='Edit user', exact=True).click()
    dialog.locator('#user-enabled').uncheck()
    dialog.locator('#user-role').select_option('credential_viewer')
    dialog.get_by_role('button', name='Save user', exact=True).click()
    expect(row).to_contain_text('Disabled')
    expect(row).to_contain_text('Credential Viewer')
    row.get_by_role('button', name='Delete user', exact=True).click()
    dialog.get_by_role('button', name='Delete user', exact=True).click()
    expect(row).to_have_count(0)


def test_secret_timeout_mobile_layout_and_signout_clear(ui, tmp_path):
    seed(ui)
    ui.page.set_viewport_size({'width': 390, 'height': 844})
    open_manager(ui)
    ui.page.clock.install()
    card = ui.page.locator('.vault-card')
    card.get_by_role('button', name='Reveal credentials', exact=True).click()
    expect(card.get_by_text(ENTRY['password'], exact=True)).to_be_visible()
    ui.page.clock.fast_forward(61000)
    expect(card.get_by_text(ENTRY['password'], exact=True)).to_have_count(0)
    assert ui.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
    ui.page.evaluate('window.scrollTo(0, 0)')
    screenshot = tmp_path / 'password-manager-mobile.png'
    ui.page.screenshot(path=str(screenshot), full_page=True)
    print('MOBILE_SCREENSHOT', screenshot)
    card.get_by_role('button', name='Edit', exact=True).click()
    expect(ui.page.locator('#credential-password')).to_have_value(ENTRY['password'])
    ui.page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    expect(ui.page.locator('#password-manager-dialog')).not_to_be_visible()
    expect(ui.page.locator('#credential-password')).to_have_count(0)
    ui.page.get_by_role('button', name='Sign out', exact=True).click()
    expect(ui.page.locator('#login-screen')).to_be_visible()


def test_own_gui_password_can_be_changed(ui):
    ui.page.get_by_role('button', name='Change your password', exact=True).click()
    dialog = ui.page.locator('#password-manager-dialog')
    dialog.locator('#account-current-password').fill(PASSWORD)
    dialog.locator('#account-new-password').fill('new-browser-gui-passphrase')
    dialog.locator('#account-confirm-password').fill('new-browser-gui-passphrase')
    dialog.get_by_role('button', name='Change password', exact=True).click()
    expect(ui.page.locator('#login-screen')).to_be_visible()
    ui.page.locator('#login-password').fill('new-browser-gui-passphrase')
    ui.page.get_by_role('button', name='Sign in', exact=False).click()
    expect(ui.page.get_by_role('button', name='New deployment', exact=True)).to_be_visible()


@pytest.mark.parametrize('leave', ['navigate', 'pagehide', 'logout'])
def test_delayed_reveal_cannot_restore_secrets_after_leaving(ui, leave):
    seed(ui)
    open_manager(ui)
    requests = []
    ui.page.route('**/api/password-manager/*/reveal', lambda route: requests.append(route))
    ui.page.locator('.vault-card').get_by_role('button', name='Reveal credentials', exact=True).click()
    expect(ui.page.locator('.vault-card').get_by_role('button', name='Reveal credentials', exact=True)).to_be_disabled()
    assert len(requests) == 1
    if leave == 'navigate':
        ui.page.get_by_role('link', name='Setup', exact=True).click()
        expect(ui.page.locator('#credential-list')).to_have_count(0)
    elif leave == 'logout':
        ui.page.get_by_role('button', name='Sign out', exact=True).click()
        expect(ui.page.locator('#login-screen')).to_be_visible()
    else:
        ui.page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    requests[0].fulfill(json={**ENTRY, 'id': 'delayed-response', 'revision': 1})
    ui.page.wait_for_timeout(100)
    expect(ui.page.get_by_text(ENTRY['password'], exact=True)).to_have_count(0)
    expect(ui.page.locator('.vault-secrets pre')).to_have_count(0)


def test_sections_tags_organization_filters_rename_and_removal(ui, tmp_path):
    seed(ui)
    open_manager(ui)
    ui.page.get_by_role('link', name='Sections', exact=True).click()
    ui.page.get_by_role('button', name='Add section', exact=True).click()
    dialog = ui.page.locator('#password-manager-dialog')
    dialog.locator('#section-name').fill('Network')
    dialog.get_by_role('button', name='Save section', exact=True).click()
    row = ui.page.locator('.vault-section-row').filter(has_text='Network')
    expect(row).to_contain_text('0 credentials')
    ui.page.get_by_role('link', name='Credentials', exact=True).click()
    requests = []
    ui.page.on('request', lambda request: requests.append(request.url) if request.url.endswith('/reveal') else None)
    ui.page.locator('.vault-card').get_by_role('button', name='Organize', exact=True).click()
    dialog.locator('#credential-section').select_option(label='Network')
    dialog.locator('#credential-tags').fill(' Linux, production, LINUX ')
    dialog.get_by_role('button', name='Save organization', exact=True).click()
    expect(dialog).not_to_be_visible()
    card = ui.page.locator('.vault-card')
    expect(card.get_by_role('button', name='Filter tag: linux', exact=True)).to_be_visible()
    assert requests == [], 'Organizing must not fetch credential secrets'
    card.get_by_role('button', name='Filter tag: production', exact=True).click()
    expect(ui.page.locator('#credential-tag-filter')).to_have_value('production')
    expect(card).to_have_count(1)
    ui.page.get_by_role('button', name='Add credential', exact=True).click()
    fill_entry(ui, 'Media server')
    dialog.locator('#credential-section').select_option('__new__')
    dialog.locator('#credential-new-section').fill('Media')
    dialog.locator('#credential-tags').fill('linux, streaming')
    dialog.get_by_role('button', name='Save entry', exact=True).click()
    expect(dialog).not_to_be_visible()
    # Existing filters survive saving; the new entry appears when filters clear.
    expect(ui.page.locator('.vault-card')).to_have_count(1)
    ui.page.get_by_role('button', name='Clear filters', exact=True).click()
    expect(ui.page.locator('.vault-card')).to_have_count(2)
    ui.page.locator('#credential-tag-filter').select_option('linux')
    expect(ui.page.locator('.vault-card')).to_have_count(2)
    expect(ui.page.locator('.vault-section-group')).to_have_count(2)
    ui.page.locator('#credential-section-filter').select_option(label='Media (1)')
    expect(ui.page.locator('.vault-card')).to_have_count(1)
    expect(ui.page.locator('.vault-card')).to_contain_text('Media server')
    ui.page.get_by_role('button', name='Clear filters', exact=True).click()
    ui.page.locator('#credential-search').fill('streaming')
    expect(ui.page.locator('.vault-card')).to_have_count(1)
    ui.page.get_by_role('button', name='Clear filters', exact=True).click()
    screenshot = tmp_path / 'password-manager-organized-desktop.png'
    ui.page.screenshot(path=str(screenshot), full_page=True)
    print('ORGANIZED_DESKTOP_SCREENSHOT', screenshot)
    ui.page.get_by_role('link', name='Sections', exact=True).click()
    row = ui.page.locator('.vault-section-row').filter(has_text='Network')
    row.get_by_role('button', name='Rename', exact=True).click()
    dialog.locator('#section-name').fill('Infrastructure')
    dialog.get_by_role('button', name='Save section', exact=True).click()
    expect(ui.page.locator('.vault-section-row').filter(has_text='Infrastructure')).to_contain_text('1 credential')
    ui.page.locator('.vault-section-row').filter(has_text='Infrastructure').get_by_role('button', name='View credentials', exact=True).click()
    expect(ui.page.locator('.vault-card')).to_have_count(1)
    expect(ui.page.locator('.vault-card')).to_contain_text('Lab desktop')
    ui.page.get_by_role('link', name='Sections', exact=True).click()
    ui.page.locator('.vault-section-row').filter(has_text='Infrastructure').get_by_role('button', name='Remove section', exact=True).click()
    expect(dialog).to_contain_text('kept under Unsectioned')
    dialog.get_by_role('button', name='Remove section', exact=True).click()
    expect(ui.page.locator('.vault-section-row').filter(has_text='Infrastructure')).to_have_count(0)
    ui.page.locator('.vault-section-row').filter(has_text='Unsectioned').get_by_role('button', name='View credentials', exact=True).click()
    card = ui.page.locator('.vault-card')
    expect(card).to_have_count(1)
    expect(card.get_by_role('button', name='Filter tag: production', exact=True)).to_be_visible()
    card.get_by_role('button', name='Reveal credentials', exact=True).click()
    expect(card.get_by_text(ENTRY['password'], exact=True)).to_be_visible()
    ui.page.reload()
    expect(ui.page.locator('.vault-card')).to_have_count(2)
    expect(ui.page.get_by_text(ENTRY['password'], exact=True)).to_have_count(0)


def test_inline_section_errors_preserve_new_entry_and_mobile_layout(ui, tmp_path):
    open_manager(ui)
    ui.page.set_viewport_size({'width': 390, 'height': 844})
    ui.page.get_by_role('button', name='Add credential', exact=True).click()
    dialog = fill_entry(ui, 'Inline section credential')
    dialog.locator('#credential-section').select_option('__new__')
    dialog.locator('#credential-new-section').fill('Unsectioned')
    dialog.locator('#credential-tags').fill('LAB, web servers')
    dialog.get_by_role('button', name='Save entry', exact=True).click()
    expect(dialog.get_by_role('alert')).to_be_visible()
    expect(dialog.locator('#credential-title')).to_have_value('Inline section credential')
    expect(dialog.locator('#credential-password')).to_have_value('browser-private-password')
    dialog.locator('#credential-new-section').fill('Home lab')
    dialog.get_by_role('button', name='Save entry', exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(ui.page.locator('.vault-card')).to_contain_text('Home lab')
    expect(ui.page.locator('.vault-card').get_by_role('button', name='Filter tag: web servers', exact=True)).to_be_visible()
    assert ui.page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    ui.page.evaluate('window.scrollTo(0, 0)')
    screenshot = tmp_path / 'password-manager-organized-mobile.png'
    ui.page.screenshot(path=str(screenshot), full_page=True)
    print('ORGANIZED_MOBILE_SCREENSHOT', screenshot)
