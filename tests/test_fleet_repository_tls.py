"""Exercise signing-key redirects over real HTTPS without external services."""

import base64
import ipaddress
import ssl
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from gdeploy import fleet_guest as fleet


TOKEN = "synthetic-test-repository-token"
KEY = b"synthetic vendor signing key"
KEY_PATH = "/corelight/fleet-stable/gpgkey"
SIGNED_PATH = "/keys/fleet.asc?signature=synthetic%2Bsignature&expires=123&filename=key%2Ffleet.asc"


@pytest.fixture
def identities(tmp_path, monkeypatch):
    """Trust only our test CA while retaining certificate and hostname checks."""
    now = datetime.now(timezone.utc)

    def authority(name):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        certificate = (
            x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
            .sign(key, hashes.SHA256())
        )
        return key, certificate

    trusted, untrusted = authority("Trusted test CA"), authority("Untrusted test CA")
    default_context = ssl.create_default_context
    ca_pem = trusted[1].public_bytes(serialization.Encoding.PEM).decode()

    def client_context():
        context = default_context(cadata=ca_pem)
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        return context

    monkeypatch.setattr(fleet.ssl, "create_default_context", client_context)
    # Local regression traffic must not depend on the developer's proxy settings.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    def issue(*, trusted_by_client=True):
        issuer_key, issuer = trusted if trusted_by_client else untrusted
        key = ec.generate_private_key(ec.SECP256R1())
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(issuer.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]), False)
            .sign(issuer_key, hashes.SHA256())
        )
        certificate_file = tmp_path / f"{certificate.serial_number}.crt"
        key_file = tmp_path / f"{certificate.serial_number}.key"
        certificate_file.write_bytes(
            certificate.public_bytes(serialization.Encoding.PEM) + issuer.public_bytes(serialization.Encoding.PEM)
        )
        key_file.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate_file, key_file)
        return context

    return issue


@contextmanager
def repository_server(context=None):
    """Keep only presence/match flags for credentials in request observations."""
    expected_auth = "Basic " + base64.b64encode((TOKEN + ":").encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.requests.append(SimpleNamespace(
                method=self.command, path=self.path,
                authorization_present="Authorization" in self.headers,
                correct_authorization=self.headers.get("Authorization") == expected_auth,
                cookie_present="Cookie" in self.headers,
                proxy_authorization_present="Proxy-Authorization" in self.headers,
                body_present=int(self.headers.get("Content-Length", "0")) > 0,
            ))
            status, headers, body = self.server.routes.get(self.path, (404, {}, b"Unexpected test path"))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def get_request(self):
            raw, address = super().get_request()
            self.connections += 1
            raw.settimeout(3)
            try:
                return (context.wrap_socket(raw, server_side=True) if context else raw), address
            except BaseException:
                raw.close()
                raise

    server = Server(("127.0.0.1", 0), Handler)
    server.routes, server.requests, server.connections = {}, [], 0
    server.url = f"{'https' if context else 'http'}://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=4)


def use_repository(monkeypatch, server):
    monkeypatch.setattr(fleet, "REPOSITORY", server.url + "/corelight/fleet-stable/")


def assert_safe_error(error, *server_urls):
    message = str(error)
    for forbidden in (TOKEN, base64.b64encode((TOKEN + ":").encode()).decode(), "synthetic-server-body", SIGNED_PATH, *server_urls):
        assert forbidden not in message
    return message


def test_same_origin_redirect_preserves_repository_authorization_but_no_cookie(identities, monkeypatch):
    with repository_server(identities()) as origin:
        use_repository(monkeypatch, origin)
        origin.routes[KEY_PATH] = (302, {"Location": SIGNED_PATH, "Set-Cookie": "repository_session=synthetic"}, b"")
        origin.routes[SIGNED_PATH] = (200, {}, KEY)
        assert fleet.download_signing_key(TOKEN) == KEY
        assert [request.path for request in origin.requests] == [KEY_PATH, SIGNED_PATH]
        assert all(request.correct_authorization for request in origin.requests)
        assert not any(request.cookie_present or request.body_present for request in origin.requests)


@pytest.mark.parametrize("return_to_origin", [False, True])
def test_cross_origin_https_redirect_preserves_signed_query_and_permanently_drops_credentials(identities, monkeypatch, return_to_origin):
    with repository_server(identities()) as origin, repository_server(identities()) as cdn:
        use_repository(monkeypatch, origin)
        origin.routes[KEY_PATH] = (302, {"Location": cdn.url + SIGNED_PATH, "Set-Cookie": "repository_session=synthetic"}, b"")
        if return_to_origin:
            cdn.routes[SIGNED_PATH] = (307, {"Location": origin.url + "/final-key?download=1", "Set-Cookie": "cdn_session=synthetic"}, b"")
            origin.routes["/final-key?download=1"] = (200, {}, KEY)
        else:
            cdn.routes[SIGNED_PATH] = (200, {}, KEY)
        assert fleet.download_signing_key(TOKEN) == KEY
        assert origin.requests[0].correct_authorization
        assert len(cdn.requests) == 1 and cdn.requests[0].path == SIGNED_PATH
        redirected = cdn.requests + origin.requests[1:]
        assert all(request.method == "GET" for request in redirected)
        assert not any(request.authorization_present or request.cookie_present or request.proxy_authorization_present or request.body_present for request in redirected)
        assert len(origin.requests) == (2 if return_to_origin else 1)


def test_redirect_destination_must_pass_real_tls_verification_before_any_http_request(identities, monkeypatch):
    with repository_server(identities()) as origin, repository_server(identities(trusted_by_client=False)) as cdn:
        use_repository(monkeypatch, origin)
        origin.routes[KEY_PATH] = (302, {"Location": cdn.url + SIGNED_PATH}, b"")
        cdn.routes[SIGNED_PATH] = (200, {}, KEY)
        with pytest.raises(fleet.FleetInstallError) as caught:
            fleet.download_signing_key(TOKEN)
        assert "TLS" in assert_safe_error(caught.value, origin.url, cdn.url)
        assert origin.requests[0].correct_authorization
        assert cdn.connections == 1 and cdn.requests == []


def test_https_redirect_cannot_downgrade_to_http_even_without_credentials(identities, monkeypatch):
    with repository_server(identities()) as origin, repository_server() as insecure:
        use_repository(monkeypatch, origin)
        origin.routes[KEY_PATH] = (302, {"Location": insecure.url + SIGNED_PATH}, b"")
        insecure.routes[SIGNED_PATH] = (200, {}, KEY)
        with pytest.raises(fleet.FleetInstallError) as caught:
            fleet.download_signing_key(TOKEN)
        assert "HTTPS" in assert_safe_error(caught.value, origin.url, insecure.url)
        assert insecure.connections == 0 and insecure.requests == []


def test_initial_repository_401_is_actionable_and_omits_response_details(identities, monkeypatch):
    with repository_server(identities()) as origin:
        use_repository(monkeypatch, origin)
        origin.routes[KEY_PATH] = (401, {}, b"synthetic-server-body " + TOKEN.encode())
        with pytest.raises(fleet.FleetInstallError) as caught:
            fleet.download_signing_key(TOKEN)
        message = assert_safe_error(caught.value, origin.url)
        assert "401" in message and "token" in message.lower()
        assert "redirected" not in message.lower()
        assert len(origin.requests) == 1 and origin.requests[0].correct_authorization


def test_redirected_403_reports_download_failure_without_exposing_signed_url_or_repository_token(identities, monkeypatch):
    with repository_server(identities()) as origin, repository_server(identities()) as cdn:
        use_repository(monkeypatch, origin)
        origin.routes[KEY_PATH] = (302, {"Location": cdn.url + SIGNED_PATH}, b"")
        cdn.routes[SIGNED_PATH] = (403, {}, b"synthetic-server-body " + SIGNED_PATH.encode())
        with pytest.raises(fleet.FleetInstallError) as caught:
            fleet.download_signing_key(TOKEN)
        message = assert_safe_error(caught.value, origin.url, cdn.url)
        assert "403" in message and "redirected" in message.lower()
        assert len(cdn.requests) == 1 and not cdn.requests[0].authorization_present


def test_real_redirect_loop_is_bounded_without_urllib_automatic_redirects(identities, monkeypatch):
    with repository_server(identities()) as origin:
        use_repository(monkeypatch, origin)
        origin.routes[KEY_PATH] = (302, {"Location": KEY_PATH}, b"")
        with pytest.raises(fleet.FleetInstallError) as caught:
            fleet.download_signing_key(TOKEN)
        assert "redirect" in assert_safe_error(caught.value, origin.url).lower()
        assert 1 <= len(origin.requests) <= 6
        assert all(request.correct_authorization for request in origin.requests)


def test_metadata_fetch_uses_same_verified_redirect_and_auth_rules(identities, monkeypatch):
    relative = "any/dists/any/main/binary-amd64/Packages.gz"
    path = "/corelight/fleet-stable/" + relative
    body = b"synthetic compressed package index"
    with repository_server(identities()) as origin, repository_server(identities()) as cdn:
        use_repository(monkeypatch, origin)
        origin.routes[path] = (302, {"Location": cdn.url + SIGNED_PATH, "Set-Cookie": "private-cookie"}, b"")
        cdn.routes[SIGNED_PATH] = (200, {}, body)
        assert fleet.download_repository_file(
            TOKEN, relative, max_bytes=8 * 1024 * 1024, timeout=30, resource="package metadata",
        ) == body
        assert origin.requests[0].correct_authorization
        assert cdn.requests[0].path == SIGNED_PATH
        assert not cdn.requests[0].authorization_present and not cdn.requests[0].cookie_present


def test_metadata_404_preserves_status_for_compression_fallback_without_response_content(identities, monkeypatch):
    with repository_server(identities()) as origin:
        use_repository(monkeypatch, origin)
        origin.routes["/corelight/fleet-stable/Packages.gz"] = (404, {}, b"synthetic-server-body " + TOKEN.encode())
        with pytest.raises(fleet.RepositoryDownloadError) as caught:
            fleet.download_repository_file(TOKEN, "Packages.gz", resource="package metadata")
        assert caught.value.status_code == 404
        assert "package metadata" in assert_safe_error(caught.value, origin.url)
        assert origin.requests[0].correct_authorization
