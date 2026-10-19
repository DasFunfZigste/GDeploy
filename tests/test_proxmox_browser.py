"""Real browser/API target selection; synthetic hypervisors, no VM provisioning."""
import pytest

from test_targets import Inventory, PAYLOAD
from test_wizard_drafts_browser import chromium, ui as wizard_ui, expect, InventoryClient  # noqa: F401


@pytest.fixture
def ui(wizard_ui):  # noqa: F811
    original_client = wizard_ui.service.client
    wizard_ui.service.client = lambda settings: Inventory() if settings.get('hypervisor') == 'proxmox' else original_client(settings)
    return wizard_ui


def add_proxmox(ui):
    ui.db.set_target_host('pve-lab', PAYLOAD)
    ui.page.goto(ui.base + '/#settings/connection')
    expect(ui.page.locator('#target-hosts-panel')).to_be_visible()
    expect(ui.page.locator('#target-hosts-panel')).to_contain_text('Lab Proxmox')
    ui.page.goto(ui.base + '/#deployments')


@pytest.mark.parametrize('width,height', [(1440, 1100), (390, 844)])
def test_proxmox_target_automatically_sets_resources_and_network_defaults(ui, width, height):
    add_proxmox(ui)
    ui.page.set_viewport_size({'width': width, 'height': height})
    ui.page.get_by_role('button', name='New deployment', exact=True).click()
    target = ui.wizard.locator('#wizard-target-host')
    expect(target).to_be_visible()
    expect(target.locator('option')).to_have_count(3)
    target.select_option('pve-lab')
    expect(target).to_be_enabled()
    ui.wizard.locator('#deployment-name').fill('pve-lab-test')
    ui.wizard.locator('#wizard-os-profile').select_option('ubuntu-autoinstall')
    ui.wizard.locator('#role-ubuntu').check()
    ui.next.click()
    expect(ui.wizard.locator('#vm-ubuntu-network')).to_have_value('vmbr0')
    expect(ui.wizard.locator('#vm-ubuntu-datastore')).to_have_value('local-lvm')
    expect(ui.wizard).to_contain_text('Network / bridge')
    expect(ui.wizard).to_contain_text('QEMU Guest Agent')
    ui.next.click()
    expect(ui.wizard).to_contain_text('Target: Lab Proxmox · Proxmox')
    ui.next.click()
    expect(ui.next).to_have_text('Deploy 1 VM')
    ui.next.click()
    expect(ui.wizard).not_to_be_visible()
    job = next(job for job in ui.db.list() if job['name'] == 'pve-lab-test')
    assert job['spec']['target_host_id'] == 'pve-lab'
    assert job['spec']['target']['hypervisor'] == 'proxmox'
    expect(ui.page.locator('body')).to_contain_text('Lab Proxmox (Proxmox)')
    assert ui.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')


def test_target_switch_preserves_sensitive_configuration_and_custom_resources(ui):
    add_proxmox(ui)
    ui.open('fleetmanager', splunk=False)
    ui.fill()
    ui.blueprint()
    ui.wizard.locator('#wizard-target-host').select_option('pve-lab')
    expect(ui.wizard.locator('#wizard-target-host')).to_be_enabled()
    ui.next.click()
    expect(ui.wizard.locator('#vm-fleetmanager-network')).to_have_value('vmbr0')
    expect(ui.wizard.locator('#vm-fleetmanager-name')).to_have_value('draft-test-fleetmanager')
    expect(ui.wizard.locator('#vm-fleetmanager-address')).to_have_value('192.0.2.10/24')
    ui.next.click()
    ui.configuration()
    ui.assert_values()
    ui.assert_private()


def test_setup_can_add_proxmox_without_configuring_a_legacy_esxi_host(ui):
    ui.page.goto(ui.base + '/#settings/connection')
    panel = ui.page.locator('#target-hosts-panel')
    expect(panel).to_be_visible()
    panel.locator('#target-name').fill('Second PVE')
    panel.locator('#target-hypervisor').select_option('proxmox')
    panel.locator('#target-address').fill('pve2.test')
    panel.locator('#target-username').fill('user@pve!token')
    panel.locator('#target-secret').fill('new-token-secret')
    panel.locator('#target-node').fill('pve')
    panel.locator('#target-iso-storage').fill('local')
    panel.locator('#target-default-network').fill('vmbr0')
    panel.locator('#target-save').click()
    expect(panel).to_contain_text('Second PVE')
    assert any(host['host'] == 'pve2.test' for host in ui.db.target_hosts())
    expect(panel.locator('#target-secret')).to_have_value('')
    preview = ui.page.locator('#sidebar-preview')
    preview.locator('summary').click()
    expect(preview).not_to_contain_text('Proxmox')
    expect(preview).to_contain_text('VM Manager')
