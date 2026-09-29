"""Explicit certificate approval, scoped to one normalized ESXi endpoint."""

from __future__ import annotations

import hmac
import ipaddress
import threading

from . import tls
from .vmware import ESXiClient, VMwareError


class CertificateTrustError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def certificate_endpoint(value):
    host, port, _ = ESXiClient._parse_host(value)
    try:
        host = ipaddress.ip_address(host).compressed
    except ValueError:
        host = host.lower().rstrip(".")
    authority = f"[{host}]" if ":" in host else host
    return host, port, f"https://{authority}:{port}"


class CertificateTrust:
    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()

    def _target(self, host):
        try:
            return certificate_endpoint(host)
        except VMwareError as error:
            raise CertificateTrustError(str(error)) from None

    def _fetch(self, host, port):
        try:
            return tls.fetch_certificate(host, port)
        except tls.CertificateError as error:
            raise CertificateTrustError(str(error)) from None

    def _details(self, pem, host, endpoint, saved):
        try:
            details = tls.certificate_details(pem)
            fingerprint = tls.certificate_details(saved["pem"])["fingerprint_sha256"] if saved else None
        except tls.CertificateError as error:
            raise CertificateTrustError(str(error)) from None
        return {
            **details,
            "host": host,
            "endpoint": endpoint,
            "trusted": bool(fingerprint and hmac.compare_digest(fingerprint, details["fingerprint_sha256"])),
            "trusted_fingerprint_sha256": fingerprint,
            "trusted_at": saved["trusted_at"] if saved else None,
        }

    def saved(self, host):
        try:
            hostname, _, endpoint = certificate_endpoint(host)
        except VMwareError:
            # Older versions accepted malformed DNS labels. Keep those settings
            # visible and editable; retrieval and connections still reject them.
            return None
        saved = self.db.esxi_certificate(endpoint)
        return self._details(saved["pem"], hostname, endpoint, saved) if saved else None

    def inspect(self, host):
        hostname, port, endpoint = self._target(host)
        pem = self._fetch(hostname, port)
        return self._details(pem, hostname, endpoint, self.db.esxi_certificate(endpoint))

    def trust(self, host, fingerprint):
        hostname, port, endpoint = self._target(host)
        # Serialize approval/removal. Fetch again so an old preview can never
        # silently approve the certificate a host presents after replacement.
        with self.lock:
            pem = self._fetch(hostname, port)
            details = self._details(pem, hostname, endpoint, self.db.esxi_certificate(endpoint))
            if not hmac.compare_digest(details["fingerprint_sha256"].upper(), fingerprint.upper()):
                raise CertificateTrustError(
                    "The ESXi certificate changed after retrieval. Retrieve it again and review the new fingerprint.", 409
                )
            if not details["can_trust"]:
                raise CertificateTrustError(details["validation_error"] or "This certificate cannot be trusted.")
            saved = self.db.trust_esxi_certificate(endpoint, pem, details["fingerprint_sha256"])
            return self._details(pem, hostname, endpoint, saved)

    def remove(self, host):
        _, _, endpoint = self._target(host)
        with self.lock:
            self.db.remove_esxi_certificate(endpoint)
