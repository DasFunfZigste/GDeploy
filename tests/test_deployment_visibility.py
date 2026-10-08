import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet

from gdeploy.db import Database, DeploymentVisibilityError


def make_deployment(db, spec, status="failed"):
    identifier = str(uuid.uuid4())
    secret_data = {
        "esxi": {"host": "esxi.example.test", "password": "private-esxi-password"},
        "vm_credentials": {
            vm["name"]: {
                "username": "gdeploy", "password": "private-guest-password",
                "private_key": "PRIVATE-SSH-KEY", "host_key": "ssh-rsa persisted-key", "services": [],
            }
            for vm in spec["vms"]
        },
    }
    db.create(identifier, spec, secret_data)
    db.update(
        identifier, status=status, stage=status, error="Recorded installation failure" if status == "failed" else None,
        resources=[{"datastore": "datastore1", "path": f"gdeploy/{identifier}/installer.iso"}],
        vms=[dict(vm, vm_id="vm-123", ip="10.0.0.20", status=status) for vm in spec["vms"]],
    )
    return identifier


def test_old_database_migration_preserves_rows_and_supports_new_inserts(config, spec):
    config.data_dir.mkdir()
    identifier = str(uuid.uuid4())
    secret_data = {"password": "retained-encrypted-secret"}
    encrypted = Fernet(config.secret_key.encode()).encrypt(json.dumps(secret_data).encode()).decode()
    values = (
        identifier, spec["name"], "failed", "failed", "2026-09-30T12:00:00Z", "2026-09-30T12:01:00Z",
        json.dumps(spec), json.dumps(spec["vms"]), '[{"path":"gdeploy/owned.iso"}]', encrypted,
        "Recorded installer error", "original-parent",
    )
    with sqlite3.connect(config.data_dir / "gdeploy.sqlite3") as connection:
        connection.execute("""CREATE TABLE deployments (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
            stage TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            spec TEXT NOT NULL, vms TEXT NOT NULL, resources TEXT NOT NULL,
            secrets TEXT NOT NULL, error TEXT, parent_id TEXT
        )""")
        connection.execute("INSERT INTO deployments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", values)
    db = Database(config.data_dir, config.secret_key)
    item = db.get(identifier, private=True)
    assert item["hidden_at"] is None
    assert item["secrets"] == secret_data
    assert item["resources"] == [{"path": "gdeploy/owned.iso"}]
    assert item["parent_id"] == "original-parent"
    with db.connect() as connection:
        row = connection.execute("SELECT * FROM deployments WHERE id=?", (identifier,)).fetchone()
        assert tuple(row) == values + (None,)
    db.set_visibility(identifier, True)
    new_id = make_deployment(db, spec, "completed")
    restarted = Database(config.data_dir, config.secret_key)
    assert [item["id"] for item in restarted.list()] == [new_id]
    assert {item["id"] for item in restarted.list(include_hidden=True)} == {identifier, new_id}
    assert restarted.get(identifier, private=True)["secrets"] == secret_data
    with restarted.connect() as connection:
        assert connection.execute("SELECT secrets FROM deployments WHERE id=?", (identifier,)).fetchone()[0] == encrypted


@pytest.mark.parametrize("status", ["completed", "failed", "stopped", "interrupted", "cleanup_failed", "reverted"])
def test_hide_restore_preserves_owned_data_and_never_contacts_esxi(signed_in, spec, monkeypatch, status):
    db = signed_in.app.state.db
    identifier = make_deployment(db, spec, status)
    path = f"/api/deployments/{identifier}"
    original = db.get(identifier, private=True)
    events = db.events(identifier)
    monkeypatch.setattr(
        signed_in.app.state.service, "client", lambda *a, **kw: pytest.fail("Visibility must not contact ESXi")
    )
    response = signed_in.patch(f"{path}/visibility", json={"hidden": True})
    assert response.status_code == 200
    hidden = response.json()
    assert hidden["hidden_at"]
    assert hidden["status"] == status and hidden["stage"] == status
    assert hidden["events"][:len(events)] == events
    assert hidden["events"][-1]["level"] == "audit"
    assert signed_in.get("/api/deployments").json() == []
    assert signed_in.get("/api/deployments?include_hidden=false").json() == []
    assert signed_in.get("/api/deployments?include_hidden=true").json()[0]["id"] == identifier
    assert signed_in.get("/api/deployments?hidden_only=true").json()[0]["id"] == identifier
    assert signed_in.get(path).json()["hidden_at"] == hidden["hidden_at"]
    persisted = db.get(identifier, private=True)
    for key in original.keys() - {"hidden_at", "updated_at"}:
        assert persisted[key] == original[key]
    if status != "reverted":
        assert db.retained()[0]["id"] == identifier
    assert all(secret not in response.text for secret in ("private-guest-password", "PRIVATE-SSH-KEY", "private-esxi-password"))
    credentials = signed_in.post(f"{path}/credentials")
    assert credentials.status_code == 200
    assert credentials.json()["vms"][0]["password"] == "private-guest-password"
    response = signed_in.patch(f"{path}/visibility", json={"hidden": False})
    assert response.status_code == 200 and response.json()["hidden_at"] is None
    assert signed_in.get("/api/deployments").json()[0]["id"] == identifier
    assert signed_in.get("/api/deployments?hidden_only=true").json() == []
    assert db.get(identifier, private=True)["secrets"] == original["secrets"]


def test_hide_and_restore_are_idempotent(signed_in, spec):
    db = signed_in.app.state.db
    identifier = make_deployment(db, spec)
    path = f"/api/deployments/{identifier}/visibility"
    first = signed_in.patch(path, json={"hidden": True}).json()
    assert signed_in.patch(path, json={"hidden": True}).json() == first
    restored = signed_in.patch(path, json={"hidden": False}).json()
    assert signed_in.patch(path, json={"hidden": False}).json() == restored
    with db.connect() as connection:
        audit = [row[0] for row in connection.execute("SELECT action FROM audit WHERE action LIKE ?", (identifier + ":%",))]
    assert len(audit) == 2
    assert identifier in audit[0] and "hidden" in audit[0]
    assert identifier in audit[1] and "restored" in audit[1]


@pytest.mark.parametrize("status", ["queued", "running", "stopping", "cleaning", "unknown"])
def test_active_or_unknown_state_cannot_be_hidden(signed_in, spec, status):
    db = signed_in.app.state.db
    identifier = make_deployment(db, spec, status)
    original = db.get(identifier, private=True)
    events = db.events(identifier)
    response = signed_in.patch(f"/api/deployments/{identifier}/visibility", json={"hidden": True})
    assert response.status_code == 409
    assert db.get(identifier, private=True) == original
    assert db.events(identifier) == events
    with pytest.raises(DeploymentVisibilityError):
        db.set_visibility(identifier, True)


@pytest.mark.parametrize("status", ["queued", "running", "stopping", "cleaning"])
def test_active_transition_restores_hidden_record(config, spec, status):
    db = Database(config.data_dir, config.secret_key)
    identifier = make_deployment(db, spec)
    db.set_visibility(identifier, True)
    db.update(identifier, status=status, stage=status)
    assert db.get(identifier)["hidden_at"] is None
    assert db.list()[0]["id"] == identifier


def test_worker_and_recovery_consider_hidden_metadata(config, spec):
    db = Database(config.data_dir, config.secret_key)
    identifier = make_deployment(db, spec, "queued")
    # Exercise a legacy/manual inconsistent row: history filtering must not
    # cause worker claiming or startup recovery to abandon its resources.
    with db.connect() as connection:
        connection.execute("UPDATE deployments SET hidden_at='previously hidden' WHERE id=?", (identifier,))
    assert db.claim() == identifier
    assert db.get(identifier)["hidden_at"] is None
    with db.connect() as connection:
        connection.execute("UPDATE deployments SET hidden_at='previously hidden' WHERE id=?", (identifier,))
    db.recover()
    assert db.get(identifier)["status"] == "interrupted"
    assert db.retained()[0]["id"] == identifier
    db.set_visibility(identifier, False)
    assert db.list()[0]["id"] == identifier


def test_hide_rechecks_state_after_another_process_starts_cleanup(config, spec):
    db = Database(config.data_dir, config.secret_key)
    identifier = make_deployment(db, spec)
    other = Database(config.data_dir, config.secret_key)
    started = threading.Event()

    def hide():
        started.set()
        return other.set_visibility(identifier, True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE deployments SET status='cleaning' WHERE id=?", (identifier,))
            request = pool.submit(hide)
            assert started.wait(5)
        with pytest.raises(DeploymentVisibilityError):
            request.result(timeout=5)
    assert db.get(identifier)["hidden_at"] is None
    assert db.get(identifier)["status"] == "cleaning"


def test_visibility_requires_login_and_csrf(client, signed_in, spec):
    db = signed_in.app.state.db
    identifier = make_deployment(db, spec)
    path = f"/api/deployments/{identifier}/visibility"
    token = signed_in.headers.pop("X-CSRF-Token")
    assert signed_in.patch(path, json={"hidden": True}).status_code == 403
    assert signed_in.patch(path, json={"hidden": True}, headers={"X-CSRF-Token": "incorrect"}).status_code == 403
    signed_in.headers["X-CSRF-Token"] = token
    assert signed_in.post("/api/logout").status_code == 200
    assert client.patch(path, json={"hidden": True}).status_code == 401
    assert client.get("/api/deployments?include_hidden=true").status_code == 401
    assert client.get("/api/deployments?hidden_only=true").status_code == 401
    assert db.get(identifier)["hidden_at"] is None


@pytest.mark.parametrize("payload", [{}, {"hidden": "true"}, {"hidden": 1}, {"hidden": None}, {"hidden": True, "delete": True}])
def test_visibility_requires_explicit_boolean_without_extra_actions(signed_in, spec, payload):
    identifier = make_deployment(signed_in.app.state.db, spec)
    response = signed_in.patch(f"/api/deployments/{identifier}/visibility", json=payload)
    assert response.status_code == 422
    assert signed_in.app.state.db.get(identifier)["hidden_at"] is None


def test_visibility_unknown_deployment_is_not_found(signed_in):
    for hidden in (True, False):
        response = signed_in.patch(f"/api/deployments/{uuid.uuid4()}/visibility", json={"hidden": hidden})
        assert response.status_code == 404


@pytest.mark.parametrize("hidden", [True, False])
def test_stopped_history_visibility_does_not_wait_for_unrelated_deployment_work(signed_in, spec, hidden):
    db = signed_in.app.state.db
    service = signed_in.app.state.service
    identifier = make_deployment(db, spec, "stopped")
    if not hidden:
        db.set_visibility(identifier, True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with service.action_lock:
            request = pool.submit(
                signed_in.patch, f"/api/deployments/{identifier}/visibility", json={"hidden": hidden},
            )
            response = request.result(timeout=2)
    assert response.status_code == 200
    assert bool(response.json()["hidden_at"]) is hidden


@pytest.mark.parametrize("initial_status", ["queued", "running"])
def test_stop_then_hide_moves_record_to_previous_history_only_after_worker_acknowledgement(
    signed_in, spec, monkeypatch, initial_status,
):
    db, service = signed_in.app.state.db, signed_in.app.state.service
    identifier = make_deployment(db, spec, initial_status)
    original = db.get(identifier, private=True)
    monkeypatch.setattr(service, "client", lambda *args: pytest.fail("Stop and hide must not change ESXi"))
    path = f"/api/deployments/{identifier}"
    response = signed_in.post(f"{path}/stop")
    assert response.status_code == 200
    if initial_status == "running":
        assert response.json()["status"] == "stopping"
        assert signed_in.patch(f"{path}/visibility", json={"hidden": True}).status_code == 409
        service.run(identifier)
    assert db.get(identifier)["status"] == "stopped"
    response = signed_in.patch(f"{path}/visibility", json={"hidden": True})
    assert response.status_code == 200 and response.json()["hidden_at"]
    assert signed_in.get("/api/deployments").json() == []
    previous = signed_in.get("/api/deployments?hidden_only=true").json()
    assert [item["id"] for item in previous] == [identifier]
    assert previous[0]["status"] == "stopped"
    retained = db.get(identifier, private=True)
    for field in ("vms", "resources", "secrets"):
        assert retained[field] == original[field]
    assert signed_in.get(path).json()["events"][-1]["level"] == "audit"
    assert signed_in.post(f"{path}/credentials").json()["vms"][0]["password"] == "private-guest-password"
    assert signed_in.patch(f"{path}/visibility", json={"hidden": False}).status_code == 200
    assert signed_in.get("/api/deployments?hidden_only=true").json() == []
    assert signed_in.get("/api/deployments").json()[0]["id"] == identifier


def test_history_filters_are_separate_and_legacy_include_hidden_remains_supported(signed_in, spec):
    db = signed_in.app.state.db
    visible = {make_deployment(db, spec, "running"), make_deployment(db, spec, "stopped")}
    hidden = {make_deployment(db, spec, "failed"), make_deployment(db, spec, "stopped")}
    for identifier in hidden:
        db.set_visibility(identifier, True)
    queries = {
        "": visible,
        "?hidden_only=false": visible,
        "?include_hidden=true": visible | hidden,
        "?include_hidden=true&hidden_only=false": visible | hidden,
        "?hidden_only=true": hidden,
        "?hidden_only=true&include_hidden=false": hidden,
        "?hidden_only=true&include_hidden=true": hidden,
    }
    for query, expected in queries.items():
        response = signed_in.get("/api/deployments" + query)
        assert response.status_code == 200
        assert {item["id"] for item in response.json()} == expected
        assert response.headers["Cache-Control"] == "no-store"
        assert all(secret not in response.text for secret in ("private-guest-password", "PRIVATE-SSH-KEY", "private-esxi-password"))
    assert {item["id"] for item in db.list(hidden_only=True)} == hidden
    assert {item["id"] for item in db.list(include_hidden=True, hidden_only=True)} == hidden
    assert signed_in.get("/api/deployments?hidden_only=unrecognized").status_code == 422


def test_restore_can_reveal_an_inconsistent_active_record(signed_in, spec):
    db = signed_in.app.state.db
    identifier = make_deployment(db, spec, "running")
    with db.connect() as connection:
        connection.execute("UPDATE deployments SET hidden_at='previously hidden' WHERE id=?", (identifier,))
    response = signed_in.patch(f"/api/deployments/{identifier}/visibility", json={"hidden": False})
    assert response.status_code == 200
    assert response.json()["status"] == "running" and response.json()["hidden_at"] is None
    assert db.list()[0]["id"] == identifier


def test_hidden_rows_do_not_consume_visible_history_limit(config, spec):
    db = Database(config.data_dir, config.secret_key)
    visible_id = make_deployment(db, spec)
    with db.connect() as connection:
        original = dict(connection.execute("SELECT * FROM deployments WHERE id=?", (visible_id,)).fetchone())
        for number in range(501):
            hidden = original | {"id": str(number), "created_at": "9999", "hidden_at": "9999"}
            connection.execute(
                "INSERT INTO deployments (" + ",".join(hidden) + ") VALUES(" + ",".join("?" for _ in hidden) + ")",
                tuple(hidden.values()),
            )
    assert [row["id"] for row in db.list()] == [visible_id]
    assert len(db.list(include_hidden=True)) == 500
    assert len(db.retained()) == 502


def test_newer_visible_rows_do_not_crowd_old_hidden_records_out_of_previous_history(signed_in, spec):
    db = signed_in.app.state.db
    hidden_id = make_deployment(db, spec, "stopped")
    db.set_visibility(hidden_id, True)
    with db.connect() as connection:
        original = dict(connection.execute("SELECT * FROM deployments WHERE id=?", (hidden_id,)).fetchone())
        for number in range(501):
            visible = original | {"id": str(number), "created_at": "9999", "hidden_at": None}
            connection.execute(
                "INSERT INTO deployments (" + ",".join(visible) + ") VALUES(" + ",".join("?" for _ in visible) + ")",
                tuple(visible.values()),
            )
    assert len(db.list()) == 500
    assert len(db.list(include_hidden=True)) == 500
    assert [item["id"] for item in db.list(hidden_only=True)] == [hidden_id]
    for query in ("?hidden_only=true", "?hidden_only=true&include_hidden=true"):
        response = signed_in.get("/api/deployments" + query)
        assert response.status_code == 200
        assert [item["id"] for item in response.json()] == [hidden_id]
    assert len(db.retained()) == 502
