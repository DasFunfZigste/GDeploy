"""Every deployment chooses its own OS and matching saved installation media."""

import pytest

from gdeploy.os_profiles import ALMA10, UBUNTU
from test_alma_wizard_browser import choose_os
from test_wizard_drafts_browser import chromium, ui as wizard_ui, expect  # noqa: F401


ROLES = ("ubuntu", "elasticsearch", "kibana", "splunk", "fleetmanager", "corelight_sensor")


@pytest.fixture
def ui(wizard_ui):  # noqa: F811
    return wizard_ui


@pytest.mark.parametrize("width,height", [(1440, 1100), (390, 844)])
def test_every_role_requires_an_explicit_os_choice_on_desktop_and_mobile(ui, width, height):
    ui.page.set_viewport_size({"width": width, "height": height})
    ui.page.get_by_role("button", name="New deployment", exact=True).click()
    selector = ui.wizard.locator("#wizard-os-profile")
    expect(selector).to_be_visible()
    expect(selector).to_have_value("")
    assert selector.evaluate("element => element.tagName") == "SELECT"
    assert selector.evaluate("element => element.required") is True
    assert selector.locator("option").evaluate_all("items => items.map(item => item.value)") == ["", UBUNTU, ALMA10]
    expect(ui.wizard.locator(".role-grid input:checked")).to_have_count(0)
    ui.page.locator("#deployment-name").fill("explicit-os-lab")
    for profile in (UBUNTU, ALMA10):
        for role in ROLES:
            ui.page.locator("#role-" + role).check()
            expect(ui.next).to_be_disabled()
            selector.select_option(profile)
            expect(selector).to_have_value(profile)
            expect(selector).to_be_enabled()
            expect(ui.next).to_be_enabled()
            for selected in ROLES:
                checkbox = ui.page.locator("#role-" + selected)
                if checkbox.is_checked():
                    checkbox.uncheck()
            selector.select_option("")
    assert ui.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    ui.close.click()
    ui.page.get_by_role("button", name="New deployment", exact=True).click()
    expect(selector).to_have_value("")
    expect(ui.wizard.locator(".role-grid input:checked")).to_have_count(0)


def configure_both(ui, monkeypatch):
    choose_os(ui, monkeypatch, UBUNTU)
    choose_os(ui, monkeypatch, ALMA10)
    profiles = ui.service.media.catalog()["profile_selections"]
    assert profiles[UBUNTU]["ready"] is True
    assert profiles[ALMA10]["ready"] is True
    assert profiles[UBUNTU]["selected"]["name"] != profiles[ALMA10]["selected"]["name"]


def open_os_only(ui, profile, name):
    ui.page.get_by_role("button", name="New deployment", exact=True).click()
    ui.wizard.locator("#wizard-os-profile").select_option(profile)
    ui.page.locator("#deployment-name").fill(name)
    ui.page.locator("#role-ubuntu").check()
    ui.next.click()
    ui.page.locator("#vm-ubuntu-network").select_option("Management")
    ui.page.locator("#vm-ubuntu-name").fill(name + "-custom")
    ui.page.locator("#vm-ubuntu-cpu").fill("3")
    ui.next.click()
    expect(ui.next).to_have_text("Run preflight")


@pytest.mark.parametrize("profile,filename", [
    (UBUNTU, "ubuntu-24.04.5-live-server-amd64.iso"),
    (ALMA10, "AlmaLinux-10.1-x86_64-dvd.iso"),
])
def test_review_preflight_and_queued_snapshot_use_selected_os_media(ui, monkeypatch, profile, filename):
    configure_both(ui, monkeypatch)
    requests = []

    def inspect_preflight(route):
        requests.append(route.request.post_data_json)
        route.continue_()

    ui.page.route("**/api/preflight", inspect_preflight)
    name = "chosen-" + ("alma" if profile == ALMA10 else "ubuntu")
    open_os_only(ui, profile, name)
    expect(ui.wizard).to_contain_text("Operating system: " + ("AlmaLinux 10" if profile == ALMA10 else "Ubuntu Server 24.04"))
    expect(ui.wizard).to_contain_text(name + "-custom")
    ui.next.click()
    expect(ui.next).to_have_text("Deploy 1 VM")
    expect(ui.wizard.locator(".preflight-check").filter(has_text="OS ISO")).to_contain_text(filename)
    assert requests[-1]["os_profile"] == profile
    ui.next.click()
    expect(ui.wizard).not_to_be_visible()
    jobs = [job for job in ui.db.list() if job["name"] == name]
    assert len(jobs) == 1
    job = ui.db.get(jobs[0]["id"], private=True)
    assert job["spec"]["os_profile"] == profile
    assert job["secrets"]["os_media"]["profile"] == profile
    assert job["secrets"]["os_media"]["name"] == filename
    assert job["spec"]["vms"][0]["name"] == name + "-custom"
    assert job["spec"]["vms"][0]["cpu"] == 3


def test_missing_alma_media_does_not_adopt_ready_ubuntu_media(ui):
    ui.page.get_by_role("button", name="New deployment", exact=True).click()
    ui.wizard.locator("#wizard-os-profile").select_option(ALMA10)
    expect(ui.wizard.get_by_role("link", name="Configure installation media", exact=True)).to_be_visible()
    ui.page.locator("#deployment-name").fill("missing-alma-media")
    ui.page.locator("#role-ubuntu").check()
    ui.next.click()
    ui.page.locator("#vm-ubuntu-network").select_option("Management")
    ui.next.click()
    ui.next.click()
    expect(ui.wizard.locator(".preflight-check.fail").filter(has_text="OS ISO")).to_contain_text("AlmaLinux 10")
    expect(ui.next).to_have_text("Run preflight")
    assert not any(job["name"] == "missing-alma-media" for job in ui.db.list())


@pytest.mark.parametrize("profile", [UBUNTU, ALMA10])
def test_empty_media_setup_does_not_block_wizard_and_requested_os_detour_keeps_vm_edits(ui, monkeypatch, profile):
    source = ui.service.config.ubuntu_iso
    source_bytes = source.read_bytes()
    source.unlink()
    ui.page.reload()
    ui.page.get_by_role("button", name="New deployment", exact=True).click()
    selector = ui.wizard.locator("#wizard-os-profile")
    expect(selector).to_have_value("")
    selector.select_option(profile)
    name = "missing-media-" + ("alma" if profile == ALMA10 else "ubuntu")
    ui.page.locator("#deployment-name").fill(name)
    ui.page.locator("#role-ubuntu").check()
    ui.next.click()
    ui.page.locator("#vm-ubuntu-network").select_option("Management")
    ui.page.locator("#vm-ubuntu-name").fill(name + "-custom")
    ui.page.locator("#vm-ubuntu-cpu").fill("5")
    ui.back.click()
    ui.wizard.get_by_role("link", name="Configure installation media", exact=True).click()
    expect(ui.wizard).not_to_be_visible()
    expect(ui.page.locator("#os-media-profile")).to_have_value(profile)
    source.write_bytes(source_bytes)
    choose_os(ui, monkeypatch, profile, return_to_overview=False)
    ui.page.get_by_role("button", name="Return to deployment", exact=True).click()
    expect(ui.wizard).to_be_visible()
    expect(selector).to_have_value(profile)
    ui.next.click()
    expect(ui.page.locator("#vm-ubuntu-name")).to_have_value(name + "-custom")
    expect(ui.page.locator("#vm-ubuntu-cpu")).to_have_value("5")
    expect(ui.page.locator("#vm-ubuntu-network")).to_have_value("Management")
    ui.next.click()
    ui.next.click()
    expect(ui.next).to_have_text("Deploy 1 VM")
    ui.next.click()
    expect(ui.wizard).not_to_be_visible()
    job = next(job for job in ui.db.list() if job["name"] == name)
    private = ui.db.get(job["id"], private=True)
    assert private["spec"]["os_profile"] == profile
    assert private["secrets"]["os_media"]["profile"] == profile
    assert private["spec"]["vms"][0]["name"] == name + "-custom"
    assert private["spec"]["vms"][0]["cpu"] == 5


def assert_fleet_secret_inputs(ui):
    for key, value in ui.values.items():
        if key != "fleetmanager-online-version":
            expect(ui.wizard.locator("#" + key)).to_have_value(value)
    assert ui.file_handle.evaluate("element => element.files[0].name") == ui.license_path.name
    assert ui.file_handle.evaluate("element => element.files[0].text()") == ui.license_path.read_text()
    ui.assert_private()


def change_fleet_os(ui, profile):
    ui.blueprint()
    ui.wizard.locator("#wizard-os-profile").select_option(profile)
    ui.next.click()
    expect(ui.page.locator("#vm-fleetmanager-name")).to_have_value("fleet-original-name")
    expect(ui.page.locator("#vm-fleetmanager-cpu")).to_have_value("6")
    ui.next.click()
    ui.configuration()
    expect(ui.wizard.locator("#fleetmanager-online-version option")).to_have_count(3)


def test_os_changes_preserve_fleet_secrets_resources_and_independent_unsaved_versions(ui, monkeypatch):
    configure_both(ui, monkeypatch)
    ui.open("fleetmanager", name="fleet-os-toggle", splunk=False, address="192.0.2.40/24", profile=UBUNTU)
    ui.fill()
    ui.back.click()
    ui.page.locator("#vm-fleetmanager-name").fill("fleet-original-name")
    ui.page.locator("#vm-fleetmanager-cpu").fill("6")
    ui.next.click()
    ui.configuration()
    change_fleet_os(ui, ALMA10)
    assert_fleet_secret_inputs(ui)
    ui.wizard.locator("#fleetmanager-online-version").select_option("0:29.2.2-1.x86_64")
    change_fleet_os(ui, UBUNTU)
    assert_fleet_secret_inputs(ui)
    expect(ui.wizard.locator("#fleetmanager-online-version")).to_have_value("29.2.1-1")
    change_fleet_os(ui, ALMA10)
    assert_fleet_secret_inputs(ui)
    expect(ui.wizard.locator("#fleetmanager-online-version")).to_have_value("0:29.2.2-1.x86_64")
    ui.save_review()
    expect(ui.wizard.locator("#fleetmanager-review")).to_contain_text("0:29.2.2-1.x86_64")
    ui.next.click()
    expect(ui.next).to_have_text("Deploy 1 VM")
    ui.next.click()
    expect(ui.wizard).not_to_be_visible()
    ui.assert_scrubbed()
    job = next(job for job in ui.db.list() if job["name"] == "fleet-os-toggle")
    private = ui.db.get(job["id"], private=True)
    assert private["spec"]["os_profile"] == ALMA10
    assert private["secrets"]["fleetmanager"]["online_version_rpm"] == "0:29.2.2-1.x86_64"


def test_sensor_os_choice_survives_setup_detour_with_management_and_pairing_edits(ui, monkeypatch):
    configure_both(ui, monkeypatch)
    ui.open("corelight_sensor", name="sensor-explicit-os", splunk=False, address="192.0.2.50/24", profile=UBUNTU)
    ui.fill()
    ui.blueprint()
    ui.wizard.locator("#wizard-os-profile").select_option(ALMA10)
    ui.next.click()
    expect(ui.page.locator("#vm-corelight_sensor-address")).to_have_value("192.0.2.50/24")
    expect(ui.page.locator("#vm-corelight_sensor-monitor_network")).to_have_value("Mirror")
    ui.next.click()
    ui.configuration()
    ui.assert_values()
    expect(ui.wizard.locator("#corelight-sensor-panel .surface-header")).to_contain_text("AlmaLinux 10")
    expect(ui.wizard.get_by_text("Experimental Corelight compatibility", exact=True)).to_be_visible()
    ui.suspend()
    choose_os(ui, monkeypatch, UBUNTU, return_to_overview=False)
    ui.resume()
    ui.assert_values()
    expect(ui.wizard.locator("#corelight-sensor-panel .surface-header")).to_contain_text("AlmaLinux 10")
    expect(ui.wizard.get_by_text("Experimental Corelight compatibility", exact=True)).to_be_visible()
    ui.save_review()
    ui.next.click()
    expect(ui.next).to_have_text("Deploy 1 VM")
    ui.next.click()
    expect(ui.wizard).not_to_be_visible()
    job = next(job for job in ui.db.list() if job["name"] == "sensor-explicit-os")
    private = ui.db.get(job["id"], private=True)
    assert private["spec"]["os_profile"] == ALMA10
    assert private["secrets"]["os_media"]["profile"] == ALMA10
    assert private["secrets"]["corelight_sensor"]["pairing_token"] == ui.values["sensor-pairing-token"]
