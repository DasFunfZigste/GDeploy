"""Readable generated passwords reach guests without rotating saved credentials."""

import copy
import hashlib
import io
import json
import math
import string
import tarfile
import uuid
from dataclasses import replace

import pytest
from passlib.hash import sha512_crypt

from gdeploy import fleet_guest, guest, passwords
from gdeploy.db import Database
from gdeploy.service import DeploymentService


FORBIDDEN = set("BbGgIiLlOoQqSsZz0125689" + string.punctuation + string.whitespace)
KEY_NAMES = ("kibana_encryption_key", "kibana_security_key", "kibana_reporting_key")


def assert_readable(value):
    assert len(value) == 40
    assert set(value) <= set(string.ascii_letters + string.digits)
    assert not FORBIDDEN.intersection(value)
    assert any(character.isupper() for character in value)
    assert any(character.islower() for character in value)
    assert any(character.isdigit() for character in value)


def login_values(secret_data):
    return [credential["password"] for credential in secret_data["vm_credentials"].values()] + [
        secret_data["software"][name] for name in ("elastic_password", "splunk_password")
    ]


def test_generated_passwords_have_no_lookalikes_and_at_least_192_bits_of_entropy():
    alphabet = passwords.PASSWORD_ALPHABET
    length = passwords.PASSWORD_LENGTH
    assert len(set(alphabet)) == len(alphabet)
    assert not FORBIDDEN.intersection(alphabet)
    groups = [sum(check(character) for character in alphabet) for check in (str.isupper, str.islower, str.isdigit)]
    # Inclusion-exclusion counts every accepted candidate, including the required
    # uppercase, lowercase and numeric categories, rather than ignoring that rule.
    possibilities = len(alphabet)**length - sum((len(alphabet) - size)**length for size in groups) + sum(
        size**length for size in groups
    )
    assert math.log2(possibilities) >= 192
    generated = {passwords.generate_login_password() for _ in range(256)}
    assert len(generated) == 256
    for value in generated:
        assert_readable(value)


def test_missing_character_categories_reject_entire_cryptographic_candidates(monkeypatch):
    valid = "Ac3" + "T" * 37
    candidates = iter("A" * 40 + "c" * 40 + "3" * 40 + "Ac" * 20 + valid)

    def choose(alphabet):
        assert alphabet == passwords.PASSWORD_ALPHABET
        return next(candidates)

    monkeypatch.setattr(passwords.secrets, "choice", choose)
    assert passwords.generate_login_password() == valid
    with pytest.raises(StopIteration):
        next(candidates)


@pytest.fixture
def queued_service(config, spec, monkeypatch):
    import gdeploy.service as module

    # A minimal valid package lets the real guest installer validate its input.
    with tarfile.open(config.splunk_package, "w:gz") as archive:
        elf = b"\x7fELF\x02\x01" + b"\x00" * 12 + (62).to_bytes(2, "little")
        for name, content in (("splunk/bin/splunk", b"test launcher"), ("splunk/bin/splunkd", elf)):
            item = tarfile.TarInfo(name)
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
    config = replace(config, splunk_sha256=hashlib.sha256(config.splunk_package.read_bytes()).hexdigest())
    db = Database(config.data_dir, config.secret_key)
    service = DeploymentService(db, config)
    monkeypatch.setattr(service, "preflight", lambda *args, **kwargs: {"ok": True, "checks": []})
    key_number = 0

    def keypair():
        nonlocal key_number
        key_number += 1
        return f"private-SSH-ILO0+/{key_number}", f"public-SSH-ILO0+/{key_number}"

    monkeypatch.setattr(module, "generate_ssh_key", keypair)
    settings = {"host": "esxi.test", "username": "root", "password": "IL10Oo!customer-chosen"}
    db.set_settings(settings)
    prepared = dict(spec, splunk_license_accepted=True, vms=[
        dict(spec["vms"][0], name="lab-" + role, role=role) for role in ("elasticsearch", "kibana", "splunk")
    ])
    return service, db, prepared, settings


def test_queue_generates_distinct_readable_logins_but_preserves_key_generation(queued_service, monkeypatch):
    import gdeploy.service as module

    service, db, spec, settings = queued_service
    hex_requests = []

    def token_hex(size):
        hex_requests.append(size)
        return "0125689abcdef" * 3 + "0125689ab"

    monkeypatch.setattr(module.secrets, "token_hex", token_hex)
    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda *args: pytest.fail("Login passwords must use the readable generator"))
    public = [service.enqueue(spec), service.enqueue(spec)]
    snapshots = [db.get(item["id"], private=True)["secrets"] for item in public]
    values = [value for snapshot in snapshots for value in login_values(snapshot)]
    assert len(set(values)) == 10
    for value in values:
        assert_readable(value)
    assert hex_requests == [24] * 6
    for snapshot in snapshots:
        assert snapshot["esxi"] == settings
        for name in KEY_NAMES:
            assert snapshot["software"][name] == "0125689abcdef" * 3 + "0125689ab"
            assert len(snapshot["software"][name]) == 48
        for credential in snapshot["vm_credentials"].values():
            assert credential["private_key"].startswith("private-SSH-ILO0+/")
            assert credential["public_key"].startswith("public-SSH-ILO0+/")
    serialized = json.dumps(public) + json.dumps([db.events(item["id"]) for item in public])
    for value in values:
        assert value not in serialized
        with db.connect() as connection:
            assert value not in connection.execute("SELECT secrets FROM deployments WHERE id=?", (public[0]["id"],)).fetchone()[0]


@pytest.mark.parametrize("legacy", [False, True])
def test_saved_passwords_reach_os_elastic_kibana_and_splunk_without_regeneration(queued_service, monkeypatch, legacy):
    import gdeploy.service as module

    service, db, spec, _ = queued_service
    item = service.enqueue(spec)
    saved = db.get(item["id"], private=True)["secrets"]
    if legacy:
        for index, credential in enumerate(saved["vm_credentials"].values()):
            credential["password"] = f"old-ILO0+_!guest-{index}"
        saved["software"].update(elastic_password="old-ILO0+_!elastic", splunk_password="old-ILO0+_!splunk")
        db.update(item["id"], secrets=saved)
    original = copy.deepcopy(saved)
    reopened = Database(service.config.data_dir, service.config.secret_key)
    reopened.recover()
    assert reopened.get(item["id"], private=True)["secrets"] == original
    monkeypatch.setattr(module, "generate_login_password", lambda: pytest.fail("Queued passwords must never regenerate"))
    monkeypatch.setattr(module, "generate_ssh_key", lambda: pytest.fail("Queued SSH keys must never regenerate"))
    iso_passwords, ssh_passwords, payloads = {}, [], []

    class ESXi:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def upload_iso(self, *args):
            pass

        def create_vm(self, vm, *args):
            return "vm-" + vm["name"]

        def power_on(self, *args):
            pass

        def guest_ip(self, *args):
            return "192.0.2.20"

        def detach_iso(self, *args):
            pass

        def delete_iso(self, *args):
            pass

    elastic = {"ip": "192.0.2.20", "version": "9.1.4", "service_token": "vendor-ILO0-token",
               "ca_pem": "-----BEGIN CERTIFICATE-----\ntest"}

    class Guest(guest.GuestSession):
        def __enter__(self):
            ssh_passwords.append(self.password)
            self.host_key = "ssh-host-ILO0+/"
            return self

        def wait_ready(self, **kwargs):
            pass

        def _run_script(self, script, payload, *args, **kwargs):
            payloads.append(copy.deepcopy(payload))
            return copy.deepcopy(elastic) if script == guest._ELASTICSEARCH_SCRIPT else {}

    def build(source, destination, vm, username, password, public_key, **kwargs):
        iso_passwords[vm["name"]] = password
        destination.write_bytes(b"prepared ISO")

    monkeypatch.setattr(module, "GuestSession", Guest)
    monkeypatch.setattr(module, "build_seed_iso", build)
    monkeypatch.setattr(service, "client", lambda settings: ESXi())
    assert db.claim() == item["id"]
    service.run(item["id"])
    completed = db.get(item["id"], private=True)
    assert completed["status"] == "completed", completed["error"]
    assert completed["secrets"]["software"] == original["software"]
    assert completed["secrets"]["elastic"] == elastic
    for name, credential in original["vm_credentials"].items():
        assert iso_passwords[name] == credential["password"]
        assert ssh_passwords.count(credential["password"]) == 2
        assert completed["secrets"]["vm_credentials"][name]["password"] == credential["password"]
        assert completed["secrets"]["vm_credentials"][name]["private_key"] == credential["private_key"]
    assert len(payloads) == 3
    assert all(payload["secrets"] == original["software"] for payload in payloads)
    config = payloads[1]["config"]
    assert config["xpack.encryptedSavedObjects.encryptionKey"] == original["software"]["kibana_encryption_key"]
    assert config["xpack.security.encryptionKey"] == original["software"]["kibana_security_key"]
    assert config["xpack.reporting.encryptionKey"] == original["software"]["kibana_reporting_key"]
    revealed = service.credentials(item["id"])["vms"]
    for vm in revealed:
        expected = "splunk_password" if vm["role"] == "splunk" else "elastic_password"
        assert vm["services"][0]["password"] == original["software"][expected]
    public = json.dumps(db.get(item["id"])) + json.dumps(db.events(item["id"]))
    assert all(value not in public for value in login_values(original))


def test_generated_os_password_is_accepted_by_real_autoinstall_hashing(spec):
    value = passwords.generate_login_password()
    settings = guest._autoinstall_data(spec["vms"][0], "gdeploy", value, "ssh-rsa TEST")
    encoded = settings["autoinstall"]["identity"]["password"]
    assert sha512_crypt.verify(value, encoded)
    assert value not in json.dumps(settings)


def test_reopening_completed_history_and_queueing_new_job_keeps_old_passwords(queued_service):
    service, db, spec, _ = queued_service
    original = {
        "vm_credentials": {spec["vms"][0]["name"]: {"username": "gdeploy", "password": "ILO0!unchanged"}},
        "software": {"elastic_password": "ILO0!old-elastic", "splunk_password": "ILO0!old-splunk"},
    }
    identifier = str(uuid.uuid4())
    db.create(identifier, dict(spec, vms=spec["vms"][:1]), original)
    db.update(identifier, status="completed")
    reopened = Database(service.config.data_dir, service.config.secret_key)
    reopened.recover()
    service.enqueue(spec)
    assert reopened.get(identifier, private=True)["secrets"] == original
    assert reopened.get(identifier)["status"] == "completed"


def test_fleetmanager_vendor_password_is_preserved_exactly(monkeypatch):
    temporary = "I1lL0OoB8!{}vendor-password"
    calls = []

    def run(command, *args, **kwargs):
        calls.append(command)
        assert kwargs["private"] is True
        return f'User "admin" successfully created.\nPassword: {temporary}\nFleetAdmin: true\n'

    monkeypatch.setattr(fleet_guest, "run", run)
    monkeypatch.setattr(fleet_guest, "SECRETS", set())
    assert fleet_guest.create_admin() == temporary
    assert calls[0][-3:] == ["create-user", "-a", "admin"]
    assert temporary in fleet_guest.SECRETS
