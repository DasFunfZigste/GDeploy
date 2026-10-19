"""Real loopback HTTPS transport: certificate approval, rotation and ISO uploads."""
import hashlib
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gdeploy.proxmox import ProxmoxClient, ProxmoxError
from test_tls import certificates  # noqa: F401


@contextmanager
def server(certificate):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.calls.append((self.command, self.path, dict(self.headers)))
            if self.path.endswith('/version'):
                body = {'version': '9.2.1'}
            elif self.path.endswith('/nodes'):
                body = [{'node': 'pve', 'status': 'online'}]
            else:
                body = []
            self.reply(body)
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            self.server.uploads.append((dict(self.headers), body))
            self.reply('UPID:pve:upload')
        def reply(self, body):
            data = json.dumps({'data': body}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    http.daemon_threads = True
    http.calls, http.uploads = [], []
    http.socket = certificate.context.wrap_socket(http.socket, server_side=True)
    thread = threading.Thread(target=lambda: http.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        yield http
    finally:
        http.shutdown()
        http.server_close()
        thread.join(5)


def test_pinned_proxmox_auth_and_streamed_iso_use_same_verified_endpoint(certificates, tmp_path):  # noqa: F811
    certificate = certificates()
    with server(certificate) as http:
        host = '127.0.0.1:' + str(http.server_port)
        client = ProxmoxClient(host, 'user@pve!token', 'test-token-secret', iso_storage='local', trusted_certificate=certificate.pem)
        with client:
            client._task = lambda upid, operation: None
            source = tmp_path / 'small.iso'
            source.write_bytes(b'iso-payload' * 32000)
            client.upload_iso('local-lvm', 'gdeploy/067a159a-4055-4bfe-b5ed-3b34644b6ebc/lab.iso', source)
        assert all(headers['Authorization'] == 'PVEAPIToken=user@pve!token=test-token-secret' for _, _, headers in http.calls)
        headers, body = http.uploads[0]
        assert headers['Authorization'] == 'PVEAPIToken=user@pve!token=test-token-secret'
        assert int(headers['Content-Length']) == len(body)
        assert source.read_bytes() in body
        assert hashlib.sha256(source.read_bytes()).hexdigest().encode() in body
        assert headers['Content-Type'].startswith('multipart/form-data; boundary=')


def test_proxmox_changed_or_unapproved_certificate_fails_before_credentials(certificates):  # noqa: F811
    previous, current = certificates(), certificates()
    with server(current) as http:
        host = '127.0.0.1:' + str(http.server_port)
        for approval in (None, previous.pem):
            client = ProxmoxClient(host, 'user@pve!token', 'never-transmit-this', trusted_certificate=approval)
            with pytest.raises(ProxmoxError, match='TLS verification failed'):
                client.connect()
        assert http.calls == [] and http.uploads == []
