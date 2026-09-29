from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from gdeploy import tls
from gdeploy.certificate_trust import certificate_endpoint
from gdeploy.config import hash_password
from gdeploy.db import Database
from gdeploy.main import create_app


HOST = "esxi.lab.test"
ENDPOINT = "https://esxi.lab.test:443"


@pytest.fixture
def certificate():
    def make(*, expired=False, future=False):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now + timedelta(days=1) if future else now - timedelta(days=3))
            .not_valid_after(now - timedelta(days=1) if expired else now + timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
            .sign(key, hashes.SHA256())
        )
        return cert.public_bytes(serialization.Encoding.PEM).decode()
    return make


def approve(client, pem):
    return client.post("/api/settings/certificate/trust", json={
        "host": HOST, "fingerprint_sha256": tls.certificate_details(pem)["fingerprint_sha256"],
    })


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/settings/certificate/inspect"),
    ("POST", "/api/settings/certificate/trust"),
    ("DELETE", "/api/settings/certificate"),
])
def test_certificate_endpoints_require_completed_account_and_csrf(client, signed_in, monkeypatch, method, path):
    def unexpected_network(*args, **kwargs):
        pytest.fail("Unauthorized request attempted certificate retrieval")
    monkeypatch.setattr(tls, "fetch_certificate", unexpected_network)
    payload = {"host": HOST}
    if path.endswith("/trust"):
        payload["fingerprint_sha256"] = ":".join(["AB"] * 32)
    token = signed_in.headers.pop("X-CSRF-Token")
    assert signed_in.request(method, path, json=payload).status_code == 403
    signed_in.headers["X-CSRF-Token"] = token
    with signed_in.app.state.db.connect() as connection:
        connection.execute("UPDATE administrator SET must_change_credentials=1")
    assert signed_in.request(method, path, json=payload).json()["detail"]["code"] == "credentials_change_required"
    client.cookies.clear()
    assert client.request(method, path, json=payload).status_code == 401


def test_preview_needs_only_host_and_does_not_trust_or_save_credentials(signed_in, certificate, monkeypatch):
    pem = certificate()
    fetched = []
    monkeypatch.setattr(tls, "fetch_certificate", lambda host, port: fetched.append((host, port)) or pem)
    response = signed_in.post("/api/settings/certificate/inspect", json={"host": HOST.upper()})
    assert response.status_code == 200
    details = response.json()
    assert details["host"] == HOST and details["endpoint"] == ENDPOINT
    assert details["can_trust"] and not details["trusted"]
    assert details["trusted_fingerprint_sha256"] is None
    assert details["fingerprint_sha256"] == tls.certificate_details(pem)["fingerprint_sha256"]
    assert "BEGIN CERTIFICATE" not in response.text
    assert fetched == [(HOST, 443)]
    assert signed_in.app.state.db.settings() is None
    assert signed_in.app.state.db.esxi_certificate(ENDPOINT) is None
    rejected = signed_in.post("/api/settings/certificate/inspect", json={"host": HOST, "password": "do-not-send"})
    assert rejected.status_code == 422 and "do-not-send" not in rejected.text


def test_trust_survives_restart_and_is_scoped_to_endpoint(signed_in, config, certificate, monkeypatch):
    pem = certificate()
    monkeypatch.setattr(tls, "fetch_certificate", lambda *args: pem)
    response = approve(signed_in, pem)
    assert response.status_code == 200 and response.json()["trusted"]
    assert response.json()["trusted_at"]
    db = signed_in.app.state.db
    assert db.esxi_certificate(ENDPOINT)["pem"] == pem
    assert Database(config.data_dir, config.secret_key).esxi_certificate(ENDPOINT)["pem"] == pem
    assert signed_in.put("/api/settings", json={"host": HOST, "username": "root", "password": "esxi-secret"}).status_code == 200
    with TestClient(create_app(config, start_worker=False)) as restarted:
        restarted.post("/api/login", json={"username": "admin", "password": "test-admin-passphrase"})
        details = restarted.get("/api/settings").json()["certificate_trust"]
        assert details["trusted"] and details["endpoint"] == ENDPOINT
        assert "pem" not in details
    service = signed_in.app.state.service
    service.client_factory = lambda **kwargs: kwargs
    configured = db.settings()
    assert service.client(configured)["trusted_certificate"] == pem
    assert service.client({**configured, "host": HOST.upper()})["trusted_certificate"] == pem
    assert "trusted_certificate" not in service.client({**configured, "host": "other.lab.test"})
    assert "trusted_certificate" not in service.client({**configured, "host": HOST + ":8443"})
    assert "trusted_certificate" not in configured  # Historical deployment snapshots are not rewritten.
    with db.connect() as connection:
        assert "BEGIN CERTIFICATE" not in connection.execute("SELECT value FROM esxi_certificates").fetchone()[0]
        audit = " ".join(row[0] for row in connection.execute("SELECT action FROM audit").fetchall())
    assert response.json()["fingerprint_sha256"] in audit and "esxi-secret" not in audit


def test_changed_certificate_requires_new_review_and_keeps_previous_trust(signed_in, certificate, monkeypatch):
    original, replacement = certificate(), certificate()
    monkeypatch.setattr(tls, "fetch_certificate", lambda *args: original)
    assert approve(signed_in, original).status_code == 200
    monkeypatch.setattr(tls, "fetch_certificate", lambda *args: replacement)
    response = approve(signed_in, original)
    assert response.status_code == 409 and "changed" in response.text
    assert signed_in.app.state.db.esxi_certificate(ENDPOINT)["pem"] == original
    preview = signed_in.post("/api/settings/certificate/inspect", json={"host": HOST}).json()
    assert not preview["trusted"]
    assert preview["trusted_fingerprint_sha256"] == tls.certificate_details(original)["fingerprint_sha256"]
    assert approve(signed_in, replacement).json()["trusted"]
    assert signed_in.app.state.db.esxi_certificate(ENDPOINT)["pem"] == replacement


@pytest.mark.parametrize("condition", ["expired", "future"])
def test_invalid_certificate_dates_can_be_previewed_but_not_trusted(signed_in, certificate, monkeypatch, condition):
    pem = certificate(**{condition: True})
    monkeypatch.setattr(tls, "fetch_certificate", lambda *args: pem)
    preview = signed_in.post("/api/settings/certificate/inspect", json={"host": HOST})
    assert preview.status_code == 200 and not preview.json()["can_trust"]
    assert preview.json()["validation_error"]
    assert approve(signed_in, pem).status_code == 400
    assert signed_in.app.state.db.esxi_certificate(ENDPOINT) is None


def test_remove_trust_restores_default_verification_without_changing_settings(signed_in, certificate, monkeypatch):
    pem = certificate()
    monkeypatch.setattr(tls, "fetch_certificate", lambda *args: pem)
    assert approve(signed_in, pem).status_code == 200
    settings = {"host": HOST, "username": "root", "password": "existing-esxi-secret", "verify_tls": True}
    assert signed_in.put("/api/settings", json=settings).status_code == 200
    assert signed_in.request("DELETE", "/api/settings/certificate", json={"host": HOST.upper()}).json() == {"ok": True}
    assert signed_in.get("/api/settings").json()["certificate_trust"] is None
    assert signed_in.app.state.db.settings() == settings
    service = signed_in.app.state.service
    service.client_factory = lambda **kwargs: kwargs
    assert service.client(settings) == settings


@pytest.mark.parametrize("host", ["http://esxi", "esxi/path", "user:secret@esxi", "esxi:8443", "", "bad\nhost"])
def test_certificate_api_rejects_urls_paths_ports_and_credentials(signed_in, monkeypatch, host):
    monkeypatch.setattr(tls, "fetch_certificate", lambda *args: pytest.fail("Invalid host reached network"))
    assert signed_in.post("/api/settings/certificate/inspect", json={"host": host}).status_code == 422


def test_fetch_failure_is_actionable_without_leaking_details(signed_in, monkeypatch):
    def unavailable(*args):
        raise tls.CertificateError("Could not retrieve the ESXi certificate. Check its address and HTTPS port 443.")
    monkeypatch.setattr(tls, "fetch_certificate", unavailable)
    response = signed_in.post("/api/settings/certificate/inspect", json={"host": HOST})
    assert response.status_code == 400 and "HTTPS port 443" in response.text
    assert signed_in.app.state.db.esxi_certificate(ENDPOINT) is None


def test_old_database_and_account_settings_are_preserved(config):
    db = Database(config.data_dir, config.secret_key)
    db.ensure_administrator("operator", hash_password("existing-administrator-password"))
    settings = {"host": HOST, "username": "root", "password": "saved-esxi-secret"}
    db.set_settings(settings)
    with db.connect() as connection:
        connection.execute("DROP TABLE esxi_certificates")
    reopened = Database(config.data_dir, config.secret_key)
    assert reopened.settings() == settings
    assert reopened.administrator()["username"] == "operator"
    assert reopened.esxi_certificate(ENDPOINT) is None


def test_certificate_endpoint_normalizes_dns_and_ipv6_without_merging_ports():
    assert certificate_endpoint("ESXI.LAB.TEST.")[2] == ENDPOINT
    assert certificate_endpoint("2001:0db8::1")[2] == "https://[2001:db8::1]:443"
    assert certificate_endpoint("[2001:db8::1]:8443")[2] == "https://[2001:db8::1]:8443"


def test_certificate_flow_never_allows_an_api_request_or_old_snapshot_to_disable_tls(signed_in):
    settings = {"host": HOST, "username": "root", "password": "esxi-secret", "verify_tls": False}
    assert signed_in.put("/api/settings", json=settings).status_code == 422
    db = signed_in.app.state.db
    db.set_settings(settings)  # Simulate settings or a deployment saved by an older API client.
    service = signed_in.app.state.service
    service.client_factory = lambda **kwargs: kwargs
    assert service.client(settings)["verify_tls"] is True
    assert signed_in.get("/api/settings").json()["verify_tls"] is True
    assert db.settings()["password"] == "esxi-secret"


def test_legacy_malformed_host_remains_editable(signed_in):
    db = signed_in.app.state.db
    db.set_settings({"host": "esxi..lab", "username": "root", "password": "old-esxi-secret"})
    response = signed_in.get("/api/settings")
    assert response.status_code == 200
    assert response.json()["host"] == "esxi..lab" and response.json()["certificate_trust"] is None
    assert "old-esxi-secret" not in response.text
    assert signed_in.post("/api/settings/certificate/inspect", json={"host": "esxi..lab"}).status_code == 422
    assert signed_in.get("/api/inventory").status_code == 400
    assert signed_in.put("/api/settings", json={
        "host": HOST, "username": "root", "password": "corrected-host-password",
    }).status_code == 200
    assert signed_in.get("/api/settings").json()["host"] == HOST
