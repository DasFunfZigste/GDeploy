"""Inspect ESXi certificates and explicitly trust one leaf for one endpoint.

Certificate inspection sends only a TLS handshake, never an HTTP request or
credentials. Explicit trust replaces CA/name verification with an exact leaf
pin, checked on the same socket before either SDK or HTTP code can send data.
"""

from __future__ import annotations

import hmac
import ipaddress
import re
import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from requests.adapters import HTTPAdapter
from requests.exceptions import SSLError


class CertificateError(ValueError):
    """A certificate inspection/parse error safe to display to the operator."""


INSPECTION_TIMEOUT = 10
CONNECTION_TIMEOUT = 60
_PEM = re.compile(
    r"\s*-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*\Z"
)


def _hostname(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or any(c.isspace() for c in value):
        raise CertificateError("Use a valid ESXi hostname or IP address.")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        hostname = value.lower().rstrip(".")
        if len(hostname) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
            for part in hostname.split(".")
        ):
            raise CertificateError("Use a valid ESXi hostname or IP address.") from None
        return hostname


def _port(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise CertificateError("The ESXi HTTPS port must be between 1 and 65535.")
    return value


def _certificate(pem: str) -> x509.Certificate:
    if not isinstance(pem, str) or len(pem) > 65536 or not _PEM.fullmatch(pem):
        raise CertificateError("Expected exactly one PEM-encoded ESXi certificate.")
    try:
        return x509.load_pem_x509_certificate(pem.encode("ascii"))
    except (ValueError, TypeError, UnicodeError):
        raise CertificateError("The ESXi certificate could not be parsed.") from None


def _validity_error(certificate: x509.Certificate) -> str | None:
    now = datetime.now(timezone.utc)
    if now < certificate.not_valid_before_utc:
        return "The certificate is not yet valid. Check the host clock and certificate start date."
    if now >= certificate.not_valid_after_utc:
        return "The certificate has expired. Renew it on ESXi before trusting it."
    return None


def certificate_details(pem: str) -> dict:
    """Return display metadata, retaining expired certificates for review."""
    certificate = _certificate(pem)
    try:
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        dns_names = names.get_values_for_type(x509.DNSName)
        ip_addresses = [str(value) for value in names.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        dns_names, ip_addresses = [], []
    except (ValueError, x509.DuplicateExtension, x509.UnsupportedGeneralNameType):
        raise CertificateError("The ESXi certificate has invalid extensions.") from None
    fingerprint = certificate.fingerprint(hashes.SHA256()).hex().upper()
    validation_error = _validity_error(certificate)
    return {
        "subject": certificate.subject.rfc4514_string(),
        "issuer": certificate.issuer.rfc4514_string(),
        "fingerprint_sha256": ":".join(fingerprint[i : i + 2] for i in range(0, len(fingerprint), 2)),
        "valid_from": certificate.not_valid_before_utc.isoformat().replace("+00:00", "Z"),
        "valid_until": certificate.not_valid_after_utc.isoformat().replace("+00:00", "Z"),
        "dns_names": dns_names,
        "ip_addresses": ip_addresses,
        "can_trust": validation_error is None,
        "validation_error": validation_error,
    }


def fetch_certificate(hostname: str, port: int = 443) -> str:
    """Fetch the presented leaf with bounded connect/handshake timeouts.

    This untrusted preview intentionally skips certificate validation. Its
    result must be reviewed before being used as an endpoint pin.
    """
    hostname, port = _hostname(hostname), _port(port)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    deadline = time.monotonic() + INSPECTION_TIMEOUT
    try:
        with socket.create_connection((hostname, port), timeout=INSPECTION_TIMEOUT) as raw:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            raw.settimeout(remaining)
            with context.wrap_socket(raw, server_hostname=hostname) as connection:
                certificate = connection.getpeercert(binary_form=True)
                if not certificate:
                    raise CertificateError("ESXi did not present a TLS certificate.")
                pem = ssl.DER_cert_to_PEM_cert(certificate)
                _certificate(pem)
                return pem
    except (TimeoutError, socket.timeout):
        raise CertificateError("Certificate inspection timed out. Check the ESXi address and network access.") from None
    except ssl.SSLError:
        raise CertificateError("The TLS handshake failed while inspecting the ESXi certificate.") from None
    except OSError:
        raise CertificateError("Could not connect to the ESXi HTTPS endpoint to inspect its certificate.") from None


class PinnedCertificateContext(ssl.SSLContext):
    """A client context that releases sockets only after exact leaf pinning."""

    def __new__(cls, pem: str, *, hostname: str, port: int = 443):
        return super().__new__(cls, ssl.PROTOCOL_TLS_CLIENT)

    def __init__(self, pem: str, *, hostname: str, port: int = 443):
        self.hostname = _hostname(hostname)
        self.port = _port(port)
        self.certificate = _certificate(pem)
        self._der = self.certificate.public_bytes(serialization.Encoding.DER)
        self.fingerprint = self.certificate.fingerprint(hashes.SHA256()).hex()
        # The approved leaf may be self-signed or use a DNS name while the
        # operator connects by IP. Exact per-endpoint pinning supplies identity.
        self.check_hostname = False
        self.verify_mode = ssl.CERT_NONE
        self.minimum_version = ssl.TLSVersion.TLSv1_2
        self.ensure_valid()

    def ensure_valid(self) -> None:
        error = _validity_error(self.certificate)
        if error:
            raise ssl.SSLCertVerificationError(error)

    def wrap_socket(
        self,
        sock,
        server_side=False,
        do_handshake_on_connect=True,
        suppress_ragged_eofs=True,
        server_hostname=None,
        session=None,
    ):
        connection = None
        try:
            self.ensure_valid()
            if server_side or _hostname(server_hostname) != self.hostname or sock.getpeername()[1] != self.port:
                raise ssl.SSLCertVerificationError("The certificate trust belongs to a different ESXi endpoint.")
            if sock.gettimeout() is None or sock.gettimeout() > CONNECTION_TIMEOUT:
                sock.settimeout(CONNECTION_TIMEOUT)
            # A delayed handshake must never expose an unchecked socket. Force
            # completion here, before the SDK or requests can send credentials.
            connection = super().wrap_socket(
                sock,
                server_side=False,
                do_handshake_on_connect=True,
                suppress_ragged_eofs=suppress_ragged_eofs,
                server_hostname=server_hostname,
                session=session,
            )
            if not hmac.compare_digest(connection.getpeercert(binary_form=True), self._der):
                raise ssl.SSLCertVerificationError(
                    "The ESXi certificate changed. Inspect and explicitly trust the replacement before connecting."
                )
            self.ensure_valid()
            return connection
        except BaseException:
            (connection if connection is not None else sock).close()
            raise

    def wrap_bio(self, *args, **kwargs):
        # Memory-BIO connections would bypass wrap_socket's check. Neither
        # supported ESXi transport needs this mode; fail closed if that changes.
        raise ssl.SSLCertVerificationError("The pinned ESXi certificate requires a direct TLS socket.")


def certificate_context(pem: str, *, hostname: str, port: int = 443) -> PinnedCertificateContext:
    return PinnedCertificateContext(pem, hostname=hostname, port=port)


class PinnedCertificateAdapter(HTTPAdapter):
    """Use endpoint trust for datastore transfers, including reused pools."""

    def __init__(self, context: PinnedCertificateContext):
        self.context = context
        super().__init__()

    def _check_url(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.username is not None
                or parsed.password is not None
                or _hostname(parsed.hostname) != self.context.hostname
                or (parsed.port or 443) != self.context.port
            ):
                raise ValueError
            self.context.ensure_valid()
        except (ValueError, ssl.SSLError):
            raise SSLError("The request does not have valid certificate trust for this ESXi endpoint.") from None

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        self._check_url(request.url)
        if verify is not True or cert or any((proxies or {}).values()):
            raise SSLError("Pinned ESXi requests require a direct connection with verification enabled.")
        return super().send(request, stream=stream, timeout=timeout, verify=True, cert=None, proxies={})

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        self._check_url(request.url)
        return (
            {"scheme": "https", "host": self.context.hostname, "port": self.context.port},
            {
                "ssl_context": self.context,
                "cert_reqs": ssl.CERT_NONE,
                "assert_hostname": False,
                # urllib3 independently enforces the same pin on its live
                # socket and marks this explicitly trusted connection verified.
                "assert_fingerprint": self.context.fingerprint,
            },
        )

    def cert_verify(self, conn, url, verify, cert):
        self._check_url(url)
        # Requests must not replace the custom context with its default CA
        # bundle. Pinning and dates are enforced by the context before any I/O.
        conn.cert_reqs = ssl.CERT_NONE
        conn.ca_certs = None
        conn.ca_cert_dir = None
