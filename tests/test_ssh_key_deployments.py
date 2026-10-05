import json
import subprocess
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from passlib.hash import sha512_crypt

from gdeploy import guest
from gdeploy.db import Database
from gdeploy.service import DeploymentService, safe_error


@pytest.fixture(scope="module")
def ssh_keys():
    def public(comment):
        key = ed25519.Ed25519PrivateKey.generate().public_key()
        return key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode() + " " + comment

    return [public(comment) for comment in ("gdeploy", "first-admin", "second-admin", "future-admin")]


def test_autoinstall_merges_keys_and_preserves_generated_automation_access(spec, ssh_keys):
    automation, first, second, _ = ssh_keys
    extras = [first, second, first.rsplit(" ", 1)[0] + " duplicate", automation.rsplit(" ", 1)[0] + " same-key"]
    rendered = yaml.safe_dump(guest._autoinstall_data(spec["vms"][0], "gdeploy", "guest-password", automation, extras))
    install = yaml.safe_load(rendered)["autoinstall"]
    assert install["ssh"] == {
        "install-server": True, "allow-pw": True, "authorized-keys": [automation, first, second],
    }
    assert install["identity"]["username"] == "gdeploy"
    assert sha512_crypt.verify("guest-password", install["identity"]["password"])
    assert "guest-password" not in rendered and "NOPASSWD" not in rendered
    assert "openssh-server" in install["packages"]
    assert install["late-commands"] == ["curtin in-target --target=/target -- systemctl enable open-vm-tools ssh"]


@pytest.mark.parametrize("extras", [None, []])
def test_empty_keys_leave_existing_guest_login_behavior(spec, ssh_keys, extras):
    data = guest._autoinstall_data(spec["vms"][0], "gdeploy", "guest-password", ssh_keys[0], extras)
    assert data["autoinstall"]["ssh"] == {
        "install-server": True, "allow-pw": True, "authorized-keys": [ssh_keys[0]],
    }
    assert data["autoinstall"]["identity"]["username"] == "gdeploy"


def test_admin_key_limit_does_not_count_the_automation_key(spec, ssh_keys):
    extras = [
        ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH,
        ).decode()
        for _ in range(50)
    ]
    data = guest._autoinstall_data(spec["vms"][0], "gdeploy", "guest-password", ssh_keys[0], extras)
    assert data["autoinstall"]["ssh"]["authorized-keys"] == [ssh_keys[0], *extras]


def test_builder_validates_additional_keys_before_running_tools(tmp_path, spec, ssh_keys, monkeypatch):
    source, output = tmp_path / "source.iso", tmp_path / "output.iso"
    source.write_bytes(b"unchanged-source")
    invalid = "-----BEGIN OPENSSH PRIVATE KEY-----\nprivate-test-material\n-----END OPENSSH PRIVATE KEY-----"
    monkeypatch.setattr(guest.subprocess, "run", lambda *a, **kw: pytest.fail("Invalid keys must not reach xorriso"))
    with pytest.raises(guest.GuestError, match="SSH public keys are invalid") as caught:
        guest.build_seed_iso(source, output, spec["vms"][0], "gdeploy", "guest-password", ssh_keys[0], authorized_ssh_keys=[invalid])
    assert "private-test-material" not in str(caught.value)
    assert not output.exists() and source.read_bytes() == b"unchanged-source"


def test_iso_maps_valid_yaml_with_admin_keys_and_keeps_source_unchanged(tmp_path, spec, ssh_keys, monkeypatch):
    source, output = tmp_path / "source.iso", tmp_path / "output.iso"
    source.write_bytes(b"unchanged-source")
    mapped = {}
    commands = []
    comment_key = ssh_keys[2].rsplit(" ", 1)[0] + " admin $(touch /tmp/not-executed); ' #"
    extra_keys = [ssh_keys[1], comment_key]

    def run(command, **kwargs):
        commands.append(command)
        if "-extract" in command:
            index = command.index("-extract")
            name, target = command[index + 1:index + 3]
            Path(target).write_text("" if name == "/md5sum.txt" else "linux /casper/vmlinuz ---\n")
        else:
            for index, word in enumerate(command):
                if word == "-map":
                    mapped[command[index + 2]] = Path(command[index + 1]).read_bytes()
            output.write_bytes(b"remastered")
        return subprocess.CompletedProcess(command, 0, stderr=b"")

    monkeypatch.setattr(guest.subprocess, "run", run)
    guest.build_seed_iso(
        source, output, spec["vms"][0], "gdeploy", "guest-password", ssh_keys[0], authorized_ssh_keys=extra_keys,
    )
    assert mapped["/nocloud/user-data"].startswith(b"#cloud-config\n")
    install = yaml.safe_load(mapped["/nocloud/user-data"])["autoinstall"]
    assert install["ssh"]["authorized-keys"] == [ssh_keys[0], *extra_keys]
    assert source.read_bytes() == b"unchanged-source"
    assert output.stat().st_mode & 0o777 == 0o600
    assert all(key not in str(commands) for key in [ssh_keys[0], *extra_keys])
    assert "not-executed" not in str(commands) and "not-executed" not in str(install["late-commands"])


def test_iso_failure_diagnostics_redact_all_installed_public_keys(tmp_path, spec, ssh_keys, monkeypatch):
    source, output = tmp_path / "source.iso", tmp_path / "output.iso"
    source.touch()
    logs = []

    def fail(command, **kwargs):
        # Tool output may omit comments or print just encoded key material.
        output.write_bytes(b"partial")
        diagnostic = "\n".join(ssh_keys[:3] + [key.split()[1] for key in ssh_keys[:3]])
        raise subprocess.CalledProcessError(5, command, stderr=diagnostic.encode())

    monkeypatch.setattr(guest.subprocess, "run", fail)
    with pytest.raises(guest.GuestError, match="status 5"):
        guest.build_seed_iso(
            source, output, spec["vms"][0], "gdeploy", "guest-password", ssh_keys[0],
            authorized_ssh_keys=ssh_keys[1:3], log=lambda text, level: logs.append(text),
        )
    messages = "\n".join(logs)
    assert "[redacted]" in messages
    assert all(key not in messages and key.split()[1] not in messages for key in ssh_keys[:3])
    assert not output.exists()


class FakeESXi:
    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def find_owned_vms(self, owner):
        return []

    def upload_iso(self, datastore, remote, local):
        assert local.is_file()

    def create_vm(self, vm, iso, owner):
        return "vm-" + vm["role"]

    def power_on(self, vm_id, owner):
        pass

    def detach_iso(self, vm_id, owner):
        pass

    def delete_iso(self, datastore, remote, owner):
        pass


def make_service(config, monkeypatch, ssh_keys):
    import gdeploy.service as module

    db = Database(config.data_dir, config.secret_key)
    service = DeploymentService(db, config, FakeESXi)
    monkeypatch.setattr(service, "preflight", lambda *a, **kw: {"ok": True, "checks": []})
    monkeypatch.setattr(module, "generate_ssh_key", lambda: ("automation-private-key", ssh_keys[0]))
    return db, service


def test_queue_snapshots_saved_keys_and_redeployment_uses_latest(config, spec, ssh_keys, monkeypatch):
    db, service = make_service(config, monkeypatch, ssh_keys)
    db.set_ssh_public_keys(ssh_keys[1:3])
    original = service.enqueue(spec, {"host": "esxi.example.test"})
    db.set_ssh_public_keys([ssh_keys[3]])
    saved = db.get(original["id"], private=True)
    assert saved["secrets"]["authorized_ssh_keys"] == ssh_keys[1:3]
    assert all(key not in json.dumps(original) for key in ssh_keys)
    with db.connect() as connection:
        ciphertext = connection.execute("SELECT secrets FROM deployments WHERE id=?", (original["id"],)).fetchone()[0]
    assert all(key not in ciphertext for key in ssh_keys)
    db.update(original["id"], status="failed")
    replacement = service.redeploy(original["id"], original["name"])
    assert db.get(replacement["id"], private=True)["secrets"]["authorized_ssh_keys"] == [ssh_keys[3]]
    assert db.get(original["id"], private=True)["secrets"]["authorized_ssh_keys"] == ssh_keys[1:3]


@pytest.mark.parametrize("legacy", [False, True])
def test_worker_installs_snapshot_on_every_role_without_changing_automation(config, spec, ssh_keys, monkeypatch, legacy):
    import gdeploy.service as module

    db, service = make_service(config, monkeypatch, ssh_keys)
    db.set_ssh_public_keys(ssh_keys[1:3])
    spec["vms"] = [dict(spec["vms"][0], name="lab-" + role, role=role) for role in ("ubuntu", "splunk", "elasticsearch", "kibana")]
    spec["splunk_license_accepted"] = True
    # This test stubs OS/application installation to isolate SSH key snapshots.
    # Supply a package reference too; its integrity is exercised separately.
    config.splunk_package.write_bytes(b"package fixture")
    package = service.packages._snapshot(config.splunk_package, sha256="0" * 64)
    monkeypatch.setattr(service.packages, "selected", lambda: package)
    monkeypatch.setattr(service.packages, "validate_snapshot", lambda snapshot: config.splunk_package)
    item = service.enqueue(spec, {"host": "esxi.example.test"})
    if legacy:
        secret_data = db.get(item["id"], private=True)["secrets"]
        secret_data.pop("authorized_ssh_keys")
        db.update(item["id"], secrets=secret_data)
    db.set_ssh_public_keys([ssh_keys[3]])
    rendered = {}
    sessions = []

    def build(source, output, vm, username, password, key, *, authorized_ssh_keys, log):
        data = guest._autoinstall_data(vm, username, password, key, authorized_ssh_keys)
        rendered[vm["role"]] = yaml.safe_load(yaml.safe_dump(data))["autoinstall"]["ssh"]["authorized-keys"]
        output.write_bytes(b"installation-media")

    def wait_ready(esxi, vm, credential, identifier):
        vm["ip"] = "10.0.0.20"
        credential["host_key"] = "verified-host-key"

    class Guest:
        def __init__(self, ip, username, private_key, password, known_host_key):
            sessions.append((username, private_key, password, known_host_key))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def install(self, role, secret_data, **kwargs):
            return {"services": []}

    monkeypatch.setattr(module, "build_seed_iso", build)
    monkeypatch.setattr(module, "GuestSession", Guest)
    monkeypatch.setattr(service, "_wait_for_guest", wait_ready)
    db.claim()
    service.run(item["id"])
    assert db.get(item["id"])["status"] == "completed"
    assert set(rendered) == {"ubuntu", "splunk", "elasticsearch", "kibana"}
    assert all(keys == (ssh_keys[:1] if legacy else ssh_keys[:3]) for keys in rendered.values())
    assert len(sessions) == 4
    assert all(username == "gdeploy" and private == "automation-private-key" and password and host == "verified-host-key" for username, private, password, host in sessions)


def test_worker_failure_redacts_saved_and_automation_key_material(config, spec, ssh_keys, monkeypatch):
    import gdeploy.service as module

    db, service = make_service(config, monkeypatch, ssh_keys)
    db.set_ssh_public_keys(ssh_keys[1:3])

    def fail(source, output, vm, username, password, key, *, authorized_ssh_keys, log):
        details = "\n".join([key, *authorized_ssh_keys, *(public.split()[1] for public in authorized_ssh_keys)])
        log("Tool details: " + details, "error")
        raise guest.GuestError("Cannot prepare ISO: " + details + " automation-private-key " + password)

    monkeypatch.setattr(module, "build_seed_iso", fail)
    item = service.enqueue(spec, {"host": "esxi.example.test"})
    db.claim()
    service.run(item["id"])
    deployment = db.get(item["id"])
    messages = json.dumps(deployment) + json.dumps(db.events(item["id"]))
    assert deployment["status"] == "failed" and "[redacted" in messages
    assert all(key not in messages and key.split()[1] not in messages for key in ssh_keys[:3])
    assert "automation-private-key" not in messages
    assert db.get(item["id"], private=True)["secrets"]["authorized_ssh_keys"] == ssh_keys[1:3]


def test_safe_error_redacts_key_blobs_without_requiring_their_comment(ssh_keys):
    error = "Encoded keys: " + " ".join(key.split()[1] for key in ssh_keys[:3])
    result = safe_error(error, {"authorized_ssh_keys": ssh_keys[1:3], "vm_credentials": {"vm": {"public_key": ssh_keys[0]}}})
    assert all(key.split()[1] not in result for key in ssh_keys[:3])
