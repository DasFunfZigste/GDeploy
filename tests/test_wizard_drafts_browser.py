"""Optional browser regression for confidential deployment drafts.

Run with ``pip install playwright && playwright install chromium``, then
``pytest -q tests/test_wizard_drafts_browser.py``. A custom browser cache can be
selected with PLAYWRIGHT_BROWSERS_PATH. Missing Playwright/browser skips these
optional tests. APIs, encrypted SQLite settings and preflight are real; ESXi is
read-only synthetic inventory and the provisioning worker never starts.
"""

import hashlib
import shutil
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import uvicorn
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from gdeploy.config import Config, hash_password
from gdeploy.db import Database
from gdeploy.main import create_app
from gdeploy.service import DeploymentService


playwright = pytest.importorskip("playwright.sync_api", reason="Optional wizard browser tests require Playwright")
expect = playwright.expect
PASSWORD = "wizard-browser-fixture-passphrase"
RESERVATION_ID = "c6c7c8aa-83c1-447d-bfe7-696621f633cc"


class InventoryClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def inventory(self):
        return {
            "host": {"name": "esxi.test", "cpu_threads": 32, "cpu_cores": 16, "cpu_mhz": 2600,
                     "memory_gb": 128, "free_cpu_reservation_mhz": 40000, "free_memory_reservation_gb": 120},
            "datastores": [{"name": "fast-ssd", "free_gb": 5000, "capacity_gb": 8000}],
            "networks": [{"name": "Management"}, {"name": "Mirror"}], "vms": [],
        }


class FixtureService(DeploymentService):
    def client(self, settings):
        return InventoryClient()


def make_license(path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fleet-browser-fixture")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30)).sign(key, hashes.SHA256()))
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()) + cert.public_bytes(serialization.Encoding.PEM))


@pytest.fixture(scope="module")
def chromium():
    with playwright.sync_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("Optional wizard browser tests require 'playwright install chromium' or PLAYWRIGHT_BROWSERS_PATH")
        browser = runtime.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def ui(tmp_path, monkeypatch, chromium):
    body = bytearray(40 * 2048)
    body[16 * 2048:16 * 2048 + 7] = b"\x01CD001\x01"
    source = tmp_path / "ubuntu-24.04.5-live-server-amd64.iso"
    source.write_bytes(body)
    license_path = tmp_path / "wizard-license.pem"
    make_license(license_path)
    config = Config(tmp_path / "data", Fernet.generate_key().decode(), hash_password(PASSWORD),
                    cookie_secure=False, ubuntu_iso=source, ubuntu_sha256=hashlib.sha256(body).hexdigest())
    db = Database(config.data_dir, config.secret_key)
    db.set_settings({"host": "esxi.test", "username": "root", "password": "fixture-esxi-secret", "verify_tls": True})
    reserved = {"name": "existing-reservation", "vms": [{"role": "ubuntu", "name": "reserved-ubuntu", "cpu": 2,
        "ram_gb": 4, "disk_gb": 40, "datastore": "fast-ssd", "network": "Management", "ip_mode": "static",
        "address": "192.0.2.10/24", "gateway": "192.0.2.1", "dns": ["192.0.2.1"]}]}
    db.create(RESERVATION_ID, reserved, {})
    record = db.get(RESERVATION_ID)
    record["vms"][0]["vm_id"] = "retained-fixture-vm"
    db.update(RESERVATION_ID, status="completed", stage="completed", vms=record["vms"])
    original_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda command, *args, **kwargs: "/fixture/xorriso" if command == "xorriso" else original_which(command, *args, **kwargs))
    app = create_app(config, start_worker=False, service_factory=FixtureService)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started, "Browser fixture server did not start"
    context = chromium.new_context(viewport={"width": 1440, "height": 1100})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/settings/fleetmanager/versions", lambda route: route.fulfill(json={"versions": ["29.2.2-1", "29.2.1-1"]}))
    fixture = WizardUI(page, base, db, license_path)
    try:
        fixture.sign_in()
        yield fixture
        assert not errors, errors
    finally:
        context.close()
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive(), "Browser fixture server did not stop"


class WizardUI:
    def __init__(self, page, base, db, license_path):
        self.page, self.base, self.db, self.license_path = page, base, db, license_path
        self.wizard = page.locator("#wizard-dialog")
        self.next = self.wizard.locator("#wizard-next")
        self.back = self.wizard.get_by_role("button", name="Back", exact=True)
        self.close = self.wizard.get_by_role("button", name="Close deployment wizard", exact=True)

    def sign_in(self):
        self.page.goto(self.base)
        self.page.locator("#login-username").fill("admin")
        self.page.locator("#login-password").fill(PASSWORD)
        self.page.get_by_role("button", name="Sign in", exact=False).click()
        expect(self.page.get_by_role("button", name="New deployment", exact=True)).to_be_visible()

    def open(self, role, name="draft-test", *, splunk=True, address="192.0.2.10/24"):
        self.role = role
        self.page.get_by_role("button", name="New deployment", exact=True).click()
        expect(self.wizard.locator(".role-grid input:checked")).to_have_count(0)
        self.page.locator("#deployment-name").fill(name)
        self.page.locator("#role-" + role).check()
        if splunk:
            self.page.locator("#role-splunk").check()
        self.next.click()
        self.page.locator(f"#vm-{role}-network").select_option("Management")
        if splunk:
            self.page.locator("#vm-splunk-network").select_option("Management")
        if role == "corelight_sensor":
            self.page.locator("#vm-corelight_sensor-monitor_network").select_option("Mirror")
        self.page.locator(f"#vm-{role}-network-static").check()
        self.page.locator(f"#vm-{role}-address").fill(address)
        self.page.locator(f"#vm-{role}-gateway").fill("192.0.2.1")
        self.page.locator(f"#vm-{role}-dnsText").fill("192.0.2.1")
        self.next.click()
        self.configuration()

    def configuration(self):
        name = "FleetManager configuration" if self.role == "fleetmanager" else "Sensor configuration"
        expect(self.wizard.get_by_role("heading", name=name, exact=True)).to_be_visible()
        expect(self.next).to_be_enabled()
        expect(self.next).to_have_text("Save & continue")

    def fill(self):
        role = self.role
        values = ({"fleetmanager-community": "draft-community-secret", "fleetmanager-token": "draft-repository-token"}
                  if role == "fleetmanager" else {
                      "sensor-repository-token": "draft-sensor-repository-token", "sensor-community-string": "draft-sensor-community",
                      "sensor-license-key": "draft-required-sensor-license", "sensor-fleet-url": "https://fleet.example.test:1443/pair",
                      "sensor-server-sslname": "fleet.example.test", "sensor-api-network": "192.0.2.0/24",
                      "sensor-pairing-token": "draft-unique-sensor-pairing-token",
                  })
        for key, value in values.items():
            self.wizard.locator("#" + key).fill(value)
        if role == "fleetmanager":
            self.wizard.locator("#fleetmanager-license").set_input_files(self.license_path)
            self.wizard.locator("#fleetmanager-load-versions").click()
            expect(self.wizard.locator("#fleetmanager-online-version option")).to_have_count(3)
            self.wizard.locator("#fleetmanager-online-version").select_option("29.2.1-1")
            values["fleetmanager-online-version"] = "29.2.1-1"
        self.values = values
        self.handles = {key: self.wizard.locator("#" + key).element_handle() for key in values}
        if role == "fleetmanager":
            self.file_handle = self.wizard.locator("#fleetmanager-license").element_handle()
        else:
            self.file_handle = None
        self.assert_private()

    def assert_values(self):
        for key, value in self.values.items():
            expect(self.wizard.locator("#" + key)).to_have_value(value)
        if self.file_handle:
            assert self.file_handle.evaluate("e => e.files.length") == 1
            assert self.file_handle.evaluate("e => e.files[0].name") == self.license_path.name
            assert self.file_handle.evaluate("e => e.files[0].text()") == self.license_path.read_text()

    def assert_private(self):
        assert self.page.evaluate("JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})") == '{"local":{},"session":{}}'
        self.page.evaluate("() => { if (document.querySelector('#wizard-dialog').open) document.querySelector('#wizard-dialog').scrollTop = 0; }")

    def assert_scrubbed(self):
        for key, handle in self.handles.items():
            if "version" in key or key in {"sensor-fleet-url", "sensor-server-sslname", "sensor-api-network"}:
                continue
            assert handle.evaluate("e => e.value") == "", key
        if self.file_handle:
            assert self.file_handle.evaluate("e => e.files.length") == 0
        self.assert_private()

    def blueprint(self):
        self.back.click()
        expect(self.page.locator(f"#vm-{self.role}-network")).to_be_visible()
        self.back.click()
        expect(self.page.locator("#deployment-name")).to_be_visible()

    def suspend(self):
        self.blueprint()
        self.page.locator("#role-splunk").check()
        self.wizard.get_by_role("link", name="Configure Splunk package", exact=True).click()
        expect(self.wizard).not_to_be_visible()
        expect(self.page.get_by_role("button", name="Return to deployment", exact=True)).to_be_visible()
        duplicates = self.page.evaluate("() => { const a = [...document.querySelectorAll('[id]')].map(n => n.id); return a.filter((id, i) => a.indexOf(id) !== i); }")
        assert duplicates == [], duplicates

    def resume(self):
        self.page.get_by_role("button", name="Return to deployment", exact=True).click()
        expect(self.wizard).to_be_visible()
        expect(self.page.locator("#deployment-name")).to_be_visible()
        self.page.locator("#role-splunk").uncheck()
        self.next.click()
        self.next.click()
        self.configuration()

    def save_review(self):
        self.next.click()
        expect(self.next).to_have_text("Run preflight")
        for key, value in self.values.items():
            if "version" in key or key in {"sensor-fleet-url", "sensor-server-sslname", "sensor-api-network"}:
                continue
            assert value not in self.wizard.inner_text()
        self.assert_private()


@pytest.mark.parametrize("role", ["fleetmanager", "corelight_sensor"])
def test_credentials_file_version_survive_back_detour_and_repeated_network_corrections(ui, role):
    ui.open(role)
    ui.fill()
    ui.back.click()
    cpu = ui.page.locator(f"#vm-{role}-cpu")
    original = cpu.input_value()
    cpu.fill("0")
    ui.next.click()
    expect(ui.wizard.locator(".vm-validation-error")).to_be_visible()
    cpu.fill(original)
    ui.next.click()
    ui.configuration()
    ui.assert_values()
    ui.blueprint()
    ui.page.locator("#role-" + role).uncheck()
    ui.page.locator("#role-" + role).check()
    ui.next.click()
    ui.next.click()
    ui.configuration()
    ui.assert_values()
    ui.suspend()
    ui.resume()
    ui.assert_values()
    ui.save_review()
    ui.back.click()
    ui.configuration()
    ui.assert_values()
    ui.save_review()
    # Settings can change independently of this live draft. The preflight
    # correction shortcut must refresh saved-state flags without losing inputs.
    if role == "fleetmanager":
        ui.db.clear_fleetmanager_settings()
    else:
        ui.db.clear_corelight_sensor_settings()
    ui.next.click()
    edit = "Edit FleetManager configuration" if role == "fleetmanager" else "Edit sensor configuration"
    ui.wizard.get_by_role("button", name=edit, exact=True).click()
    ui.configuration()
    ui.assert_values()
    ui.save_review()
    for retry in range(2):
        ui.next.click()
        expect(ui.wizard.locator(".preflight-check.fail")).to_contain_text("Static address reservations")
        expect(ui.wizard.locator(".preflight-check.fail")).to_contain_text("192.0.2.10")
        ui.back.click()
        ui.configuration()
        ui.assert_values()
        ui.back.click()
        ui.page.locator(f"#vm-{role}-address").fill("192.0.2.10/24" if retry == 0 else "192.0.2.20/24")
        ui.next.click()
        ui.configuration()
        ui.assert_values()
        ui.save_review()
    ui.next.click()
    expect(ui.next).to_have_text("Deploy 1 VM")
    ui.next.click()
    expect(ui.wizard).not_to_be_visible()
    expect(ui.page.locator("#deployment-detail-status")).to_have_text("Queued")
    ui.assert_scrubbed()
    queued = ui.db.list()[0]
    secrets = ui.db.get(queued["id"], private=True)["secrets"]
    if role == "fleetmanager":
        assert secrets["fleetmanager"]["online_version"] == "29.2.1-1"
        assert secrets["fleetmanager"]["license_pem"] == ui.license_path.read_text()
    else:
        assert secrets["corelight_sensor"]["pairing_token"] == ui.values["sensor-pairing-token"]
        assert "sensor_pairing_token" not in queued["spec"]


@pytest.mark.parametrize("role", ["fleetmanager", "corelight_sensor"])
@pytest.mark.parametrize("action", ["close", "escape", "logout", "session_expiry", "clear_setup"])
def test_explicit_discard_logout_and_setup_clear_scrub_draft_values(ui, role, action):
    ui.open(role)
    ui.fill()
    if action == "close":
        ui.close.click()
    elif action == "escape":
        ui.page.keyboard.press("Escape")
    elif action == "logout":
        ui.suspend()
        ui.page.locator("#logout").click()
        expect(ui.page.locator("#login-password")).to_be_visible()
    elif action == "session_expiry":
        endpoint = "fleetmanager" if role == "fleetmanager" else "corelight-sensor"
        pattern = "**/api/settings/" + endpoint
        ui.page.route(pattern, lambda route: route.fulfill(status=401, json={"detail": "Fixture session expired"}))
        ui.next.click()
        expect(ui.page.locator("#login-password")).to_be_visible()
        ui.page.unroute(pattern)
        ui.page.context.clear_cookies()
    else:
        # Save first so Clear removes actual encrypted defaults, then open Setup
        # while the same wizard still contains its confidential entered values.
        ui.blueprint()
        ui.page.locator("#role-splunk").uncheck()
        ui.next.click()
        ui.next.click()
        ui.configuration()
        ui.save_review()
        ui.back.click()
        ui.configuration()
        ui.suspend()
        tab = "fleetmanager" if role == "fleetmanager" else "corelight-sensor"
        ui.page.locator("#software-tab-" + tab).click()
        clear = "Clear FleetManager setup" if role == "fleetmanager" else "Clear sensor setup"
        ui.page.get_by_role("button", name=clear, exact=True).click()
        confirm = "Clear setup" if role == "fleetmanager" else "Clear sensor setup"
        ui.page.locator("#confirm-dialog").get_by_role("button", name=confirm, exact=True).click()
        expect(ui.page.locator("#confirm-dialog")).not_to_be_visible()
    ui.assert_scrubbed()
    if action == "clear_setup":
        ui.resume()
        for key in ui.values:
            if "version" in key or key in {"sensor-fleet-url", "sensor-server-sslname", "sensor-api-network"}:
                continue
            expect(ui.wizard.locator("#" + key)).to_have_value("")
    else:
        if action in {"logout", "session_expiry"}:
            ui.sign_in()
        ui.page.evaluate("location.hash = 'deployments'")
        expect(ui.page.get_by_role("button", name="New deployment", exact=True)).to_be_visible()
        ui.open(role, "fresh-draft", splunk=False, address="192.0.2.20/24")
        for key in ui.values:
            if "version" in key or key in {"sensor-fleet-url", "sensor-server-sslname", "sensor-api-network"}:
                continue
            expect(ui.wizard.locator("#" + key)).to_have_value("")
        if role == "fleetmanager":
            assert ui.wizard.locator("#fleetmanager-license").evaluate("e => e.files.length") == 0


@pytest.mark.parametrize("role", ["fleetmanager", "corelight_sensor"])
def test_failed_configuration_save_keeps_masked_values_and_file_for_retry(ui, role):
    ui.open(role, splunk=False, address="192.0.2.20/24")
    ui.fill()
    endpoint = "fleetmanager" if role == "fleetmanager" else "corelight-sensor"
    pattern = "**/api/settings/" + endpoint
    ui.page.route(pattern, lambda route: route.fulfill(status=503, json={"detail": "Fixture save unavailable"}))
    ui.next.click()
    expect(ui.wizard.get_by_role("alert")).to_contain_text("Fixture save unavailable")
    ui.configuration()
    ui.assert_values()
    ui.page.unroute(pattern)
    ui.save_review()
    ui.back.click()
    ui.configuration()
    ui.assert_values()
