from __future__ import annotations

import hashlib
import ipaddress
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pyVmomi import vim

from gdeploy import tls, vmware
from gdeploy.vmware import ESXiClient, VMwareError


@pytest.fixture
def certificates(tmp_path):
    def make(name="esxi.internal.test", *, start=None, end=None):
        now = datetime.now(timezone.utc)
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start or now - timedelta(days=1))
            .not_valid_after(end or now + timedelta(days=30))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(name), x509.IPAddress(ipaddress.ip_address("192.0.2.1"))]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
        cert_path = tmp_path / f"{certificate.serial_number}.crt"
        key_path = tmp_path / f"{certificate.serial_number}.key"
        cert_path.write_text(pem)
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        return SimpleNamespace(pem=pem, certificate=certificate, context=context)

    return make


@contextmanager
def tls_server(*certificates):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.requests.append(("GET", self.path, dict(self.headers), b""))
            response = getattr(self.server, "download_body", None) if self.path.startswith("/folder/") else None
            response = response if response is not None else (
                b'<namespaces version="1.0"><namespace><name>urn:vim25</name>'
                b'<version>8.0.3.0</version></namespace></namespaces>'
            )
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def do_PUT(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.server.requests.append(("PUT", self.path, dict(self.headers), data))
            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.server.requests.append(("POST", self.path, dict(self.headers), data))
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def get_request(self):
            raw, address = super().get_request()
            raw.settimeout(3)
            context = certificates[min(self.connections, len(certificates) - 1)].context
            self.connections += 1
            try:
                return context.wrap_socket(raw, server_side=True), address
            except BaseException:
                raw.close()
                raise

    server = Server(("127.0.0.1", 0), Handler)
    server.requests = []
    server.connections = 0
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=4)


def session_for(pem, server):
    context = tls.certificate_context(pem, hostname="127.0.0.1", port=server.server_port)
    session = requests.Session()
    session.trust_env = False
    adapter = tls.PinnedCertificateAdapter(context)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def test_details_include_fingerprint_names_and_dates(certificates):
    certificate = certificates()
    details = tls.certificate_details(certificate.pem)
    digest = hashlib.sha256(certificate.certificate.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
    assert details["fingerprint_sha256"] == ":".join(digest[i : i + 2] for i in range(0, 64, 2))
    assert details["subject"] == details["issuer"] == "CN=esxi.internal.test"
    assert details["dns_names"] == ["esxi.internal.test"]
    assert details["ip_addresses"] == ["192.0.2.1"]
    assert details["valid_from"].endswith("Z") and details["valid_until"].endswith("Z")
    assert details["can_trust"] is True and details["validation_error"] is None


@pytest.mark.parametrize("state", ["expired", "future"])
def test_invalid_dates_can_be_reviewed_but_never_trusted(certificates, state):
    now = datetime.now(timezone.utc)
    certificate = certificates(
        start=now + timedelta(days=1) if state == "future" else now - timedelta(days=2),
        end=now + timedelta(days=2) if state == "future" else now - timedelta(days=1),
    )
    details = tls.certificate_details(certificate.pem)
    assert details["can_trust"] is False
    assert ("not yet valid" if state == "future" else "expired") in details["validation_error"]
    with pytest.raises(ssl.SSLCertVerificationError):
        tls.certificate_context(certificate.pem, hostname="esxi")


@pytest.mark.parametrize("pem", [None, "secret-password", "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----"])
def test_bad_pem_errors_are_safe(pem):
    with pytest.raises(tls.CertificateError) as error:
        tls.certificate_details(pem)
    assert "secret-password" not in str(error.value)


def test_rejects_certificate_bundles_or_trailing_content(certificates):
    certificate = certificates()
    for pem in (certificate.pem * 2, certificate.pem + "extra-data"):
        with pytest.raises(tls.CertificateError, match="exactly one"):
            tls.certificate_details(pem)


def test_invalid_extensions_become_safe_certificate_errors(monkeypatch):
    class InvalidCertificate:
        @property
        def extensions(self):
            raise x509.DuplicateExtension("untrusted extension text", x509.oid.ExtensionOID.SUBJECT_ALTERNATIVE_NAME)

    monkeypatch.setattr(tls, "_certificate", lambda pem: InvalidCertificate())
    with pytest.raises(tls.CertificateError, match="invalid extensions"):
        tls.certificate_details("placeholder")


def test_preview_fetches_untrusted_leaf_without_sending_http(certificates):
    certificate = certificates()
    with tls_server(certificate) as server:
        assert tls.fetch_certificate("127.0.0.1", server.server_port) == certificate.pem
        assert server.connections == 1
        assert server.requests == []


def test_preview_retains_expired_leaf_for_display(certificates):
    certificate = certificates(end=datetime.now(timezone.utc) - timedelta(hours=1))
    with tls_server(certificate) as server:
        pem = tls.fetch_certificate("127.0.0.1", server.server_port)
        assert tls.certificate_details(pem)["can_trust"] is False
        assert server.requests == []


def test_preview_handshake_has_finite_timeout(monkeypatch):
    monkeypatch.setattr(tls, "INSPECTION_TIMEOUT", 0.1)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        start = time.monotonic()
        with pytest.raises(tls.CertificateError, match="timed out"):
            tls.fetch_certificate("127.0.0.1", listener.getsockname()[1])
        assert time.monotonic() - start < 2


def test_trusted_self_signed_name_mismatch_can_upload_on_approved_endpoint(certificates):
    certificate = certificates("esxi.internal.test")
    with tls_server(certificate) as server, session_for(certificate.pem, server) as session:
        response = session.put(
            f"https://127.0.0.1:{server.server_port}/folder/test.iso",
            data=b"iso-data",
            headers={"Cookie": "vmware_soap_session=test-secret"},
            timeout=(1, 2),
        )
        assert response.status_code == 201
        assert server.requests[0][2]["Cookie"] == "vmware_soap_session=test-secret"
        assert server.requests[0][3] == b"iso-data"


def test_changed_leaf_never_receives_upload_cookie_or_body(certificates):
    approved, replacement = certificates(), certificates()
    with tls_server(replacement) as server, session_for(approved.pem, server) as session:
        # Even a replacement in the context's CA store must fail exact pinning.
        session.get_adapter("https://").context.load_verify_locations(cadata=replacement.pem)
        with pytest.raises(requests.exceptions.SSLError):
            session.put(
                f"https://127.0.0.1:{server.server_port}/folder/test.iso",
                data=b"secret-iso-data",
                headers={"Cookie": "vmware_soap_session=test-secret"},
                timeout=(1, 2),
            )
        assert server.requests == []


def test_each_new_connection_rechecks_pin_after_success(certificates):
    approved, replacement = certificates(), certificates()
    with tls_server(approved, replacement) as server, session_for(approved.pem, server) as session:
        url = f"https://127.0.0.1:{server.server_port}/folder/test.iso"
        assert session.put(url, data=b"first", timeout=(1, 2)).status_code == 201
        with pytest.raises(requests.exceptions.SSLError):
            session.put(url, data=b"second-secret", timeout=(1, 2))
        assert len(server.requests) == 1


@pytest.mark.parametrize("changed", [False, True])
def test_client_upload_uses_same_pinned_context_as_sdk(certificates, monkeypatch, tmp_path, changed):
    approved = certificates()
    served = certificates() if changed else approved
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "irrelevant-missing-bundle.pem"))
    with tls_server(served) as server:
        client = ESXiClient(
            f"127.0.0.1:{server.server_port}", "root", "password", verify_tls=False, trusted_certificate=approved.pem
        )
        host = SimpleNamespace(
            runtime=SimpleNamespace(connectionState="connected", inMaintenanceMode=False),
            datastore=[SimpleNamespace(name="datastore1", summary=SimpleNamespace(accessible=True))],
        )
        dc = SimpleNamespace(name="ha-datacenter")
        si = MagicMock()
        si.RetrieveContent.return_value.about.apiType = "HostAgent"
        si._stub.cookie = 'vmware_soap_session="test-session-secret"'
        connect = MagicMock(return_value=si)
        monkeypatch.setattr(vmware, "SmartConnect", connect)
        monkeypatch.setattr(vmware, "Disconnect", lambda instance: None)
        monkeypatch.setattr(client, "_objects", lambda kind: [host] if kind is vim.HostSystem else [dc])
        source = tmp_path / "ubuntu.iso"
        source.write_bytes(b"installation-media-secret")
        with client:
            assert client.verify_tls is True
            assert client._http.get_adapter("https://").context is connect.call_args.kwargs["sslContext"]
            assert client._http.trust_env is False
            if changed:
                with pytest.raises(VMwareError, match="TLS certificate"):
                    client.upload_iso("datastore1", "gdeploy/067a159a-4055-4bfe-b5ed-3b34644b6ebc/ubuntu.iso", source)
                assert server.requests == []
            else:
                client.upload_iso("datastore1", "gdeploy/067a159a-4055-4bfe-b5ed-3b34644b6ebc/ubuntu.iso", source)
                assert server.requests[0][2]["Cookie"] == 'vmware_soap_session="test-session-secret"'
                assert server.requests[0][3] == b"installation-media-secret"


@pytest.mark.parametrize("changed", [False, True])
def test_client_download_streams_only_from_approved_certificate(certificates, monkeypatch, tmp_path, changed):
    approved = certificates()
    served = certificates() if changed else approved
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "irrelevant-missing-bundle.pem"))
    with tls_server(served) as server:
        server.download_body = b"\0" * (16 * 2048) + b"\x01CD001\x01" + b"installation-media" * 70000
        client = ESXiClient(
            f"127.0.0.1:{server.server_port}", "root", "password", verify_tls=False, trusted_certificate=approved.pem
        )
        host = SimpleNamespace(
            runtime=SimpleNamespace(connectionState="connected", inMaintenanceMode=False),
            datastore=[SimpleNamespace(name="datastore1", summary=SimpleNamespace(accessible=True))],
        )
        dc = SimpleNamespace(name="ha-datacenter")
        si = MagicMock()
        si.RetrieveContent.return_value.about.apiType = "HostAgent"
        si._stub.cookie = 'vmware_soap_session="test-session-secret"'
        connect = MagicMock(return_value=si)
        monkeypatch.setattr(vmware, "SmartConnect", connect)
        monkeypatch.setattr(vmware, "Disconnect", lambda instance: None)
        monkeypatch.setattr(client, "_objects", lambda kind: [host] if kind is vim.HostSystem else [dc])
        destination = tmp_path / "received.partial"
        with client:
            assert client.verify_tls is True
            assert client._http.get_adapter("https://").context is connect.call_args.kwargs["sslContext"]
            assert client._http.trust_env is False
            if changed:
                with pytest.raises(VMwareError, match="TLS certificate"):
                    client.download_iso("datastore1", "ISO Library/ubuntu.iso", destination, max_bytes=2 * 1024**2)
                assert server.requests == []
                assert not destination.exists()
            else:
                count = client.download_iso(
                    "datastore1", "ISO Library/ubuntu.iso", destination,
                    max_bytes=2 * 1024**2, expected_size=len(server.download_body),
                )
                assert count == len(server.download_body)
                assert destination.read_bytes() == server.download_body
                assert len(server.requests) == 1
                method, path, headers, body = server.requests[0]
                assert method == "GET"
                assert path.startswith("/folder/ISO%20Library/ubuntu.iso?")
                assert headers["Cookie"] == 'vmware_soap_session="test-session-secret"'
                assert headers["Accept-Encoding"] == "identity"
                assert "Authorization" not in headers
                assert body == b""


@pytest.mark.parametrize("bundle", ["matching", "wrong", "missing"])
def test_unpinned_upload_honors_explicit_ca_bundle_without_environment_proxies(
    certificates, monkeypatch, tmp_path, bundle
):
    certificate = certificates("localhost")
    ca_path = tmp_path / "custom-ca.pem"
    ca_path.write_text(certificate.pem if bundle == "matching" else certificates("other-ca").pem)
    if bundle == "missing":
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    else:
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_path))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    with tls_server(certificate) as server:
        client = ESXiClient(f"localhost:{server.server_port}", "root", "password")
        host = SimpleNamespace(
            runtime=SimpleNamespace(connectionState="connected", inMaintenanceMode=False),
            datastore=[SimpleNamespace(name="datastore1", summary=SimpleNamespace(accessible=True))],
        )
        dc = SimpleNamespace(name="ha-datacenter")
        si = MagicMock()
        si.RetrieveContent.return_value.about.apiType = "HostAgent"
        si._stub.cookie = 'vmware_soap_session="test-session-secret"'
        monkeypatch.setattr(vmware, "SmartConnect", lambda **kwargs: si)
        monkeypatch.setattr(vmware, "Disconnect", lambda instance: None)
        monkeypatch.setattr(client, "_objects", lambda kind: [host] if kind is vim.HostSystem else [dc])
        source = tmp_path / "ubuntu.iso"
        source.write_bytes(b"installation-media-secret")
        with client:
            assert client._http.trust_env is False
            path = "gdeploy/067a159a-4055-4bfe-b5ed-3b34644b6ebc/ubuntu.iso"
            if bundle == "matching":
                client.upload_iso("datastore1", path, source)
                assert server.requests[0][2]["Cookie"] == 'vmware_soap_session="test-session-secret"'
                assert server.requests[0][3] == b"installation-media-secret"
            else:
                with pytest.raises(VMwareError, match="TLS certificate"):
                    client.upload_iso("datastore1", path, source)
                assert server.requests == []


@pytest.mark.parametrize("verify_tls", [True, False])
def test_sdk_discovery_refuses_changed_certificate_before_http_or_credentials(certificates, verify_tls):
    approved, replacement = certificates(), certificates()
    with tls_server(replacement) as server:
        client = ESXiClient(
            f"127.0.0.1:{server.server_port}",
            "root-secret",
            "password-secret",
            verify_tls=verify_tls,
            trusted_certificate=approved.pem,
        )
        with pytest.raises(VMwareError, match="TLS certificate"):
            client.connect()
        assert server.connections == 1
        assert server.requests == []


def test_sdk_rechecks_pin_between_version_discovery_and_soap(certificates):
    approved, replacement = certificates(), certificates()
    with tls_server(approved, replacement) as server:
        client = ESXiClient(
            f"127.0.0.1:{server.server_port}", "root-secret", "password-secret", trusted_certificate=approved.pem
        )
        with pytest.raises(VMwareError, match="certificate changed"):
            client.connect()
        assert server.connections == 2
        assert len(server.requests) == 1
        method, path, headers, body = server.requests[0]
        assert (method, path, body) == ("GET", "/sdk/vimServiceVersions.xml", b"")
        assert "Authorization" not in headers and "Cookie" not in headers


def test_default_sdk_ca_verification_remains_enabled(certificates):
    with tls_server(certificates()) as server:
        client = ESXiClient(f"127.0.0.1:{server.server_port}", "root", "password-secret")
        with pytest.raises(VMwareError, match="TLS certificate"):
            client.connect()
        assert server.requests == []


@pytest.mark.parametrize("change", ["host", "port", "http", "proxy", "verify"])
def test_trust_cannot_be_reused_for_different_endpoint_or_disabled(certificates, change):
    certificate = certificates()
    with tls_server(certificate) as server, session_for(certificate.pem, server) as session:
        url = f"https://127.0.0.1:{server.server_port}/folder/test.iso"
        kwargs = {}
        if change == "host":
            url = url.replace("127.0.0.1", "localhost")
        elif change == "port":
            url = "https://127.0.0.1:1/folder/test.iso"
        elif change == "http":
            url = url.replace("https://", "http://")
        elif change == "proxy":
            kwargs["proxies"] = {"https": "http://127.0.0.1:1"}
        else:
            kwargs["verify"] = False
        with pytest.raises(requests.exceptions.SSLError):
            session.put(url, data=b"secret", timeout=(1, 2), **kwargs)
        assert server.connections == 0 and server.requests == []


def test_existing_session_fails_after_approved_certificate_expires(certificates, monkeypatch):
    certificate = certificates()
    with tls_server(certificate) as server, session_for(certificate.pem, server) as session:
        url = f"https://127.0.0.1:{server.server_port}/folder/test.iso"
        assert session.put(url, data=b"first", timeout=(1, 2)).status_code == 201

        class Future(datetime):
            @classmethod
            def now(cls, tz=None):
                return certificate.certificate.not_valid_after_utc + timedelta(seconds=1)

        monkeypatch.setattr(tls, "datetime", Future)
        with pytest.raises(requests.exceptions.SSLError):
            session.put(url, data=b"after-expiry-secret", timeout=(1, 2))
        assert len(server.requests) == 1 and server.connections == 1


def test_context_fails_closed_for_unchecked_memory_bio_connections(certificates):
    context = tls.certificate_context(certificates().pem, hostname="esxi")
    with pytest.raises(ssl.SSLCertVerificationError, match="direct TLS"):
        context.wrap_bio(ssl.MemoryBIO(), ssl.MemoryBIO(), server_hostname="esxi")
