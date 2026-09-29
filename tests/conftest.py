import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from gdeploy.config import Config, hash_password
from gdeploy.main import create_app


@pytest.fixture
def config(tmp_path):
    return Config(
        data_dir=tmp_path / "data",
        secret_key=Fernet.generate_key().decode(),
        admin_password_hash=hash_password("test-admin-passphrase"),
        cookie_secure=False,
        ubuntu_iso=tmp_path / "ubuntu.iso",
        splunk_package=tmp_path / "splunk.tgz",
    )


@pytest.fixture
def client(config):
    app = create_app(config, start_worker=False)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client):
    response = client.post("/api/login", json={"username": "admin", "password": "test-admin-passphrase"})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client


@pytest.fixture
def spec():
    return {
        "name": "Lab",
        "splunk_license_accepted": False,
        "vms": [
            {
                "name": "lab-elasticsearch",
                "role": "elasticsearch",
                "cpu": 2,
                "ram_gb": 8,
                "disk_gb": 60,
                "datastore": "datastore1",
                "network": "VM Network",
                "ip_mode": "dhcp",
                "address": None,
                "gateway": None,
                "dns": [],
            }
        ],
    }
