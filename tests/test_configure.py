import base64
import hashlib
import re
import stat

import pytest

from gdeploy.config import verify_password
from scripts import configure


def read_docker_env(path):
    # Model Docker --env-file: values are literal, including any quotes or dollar signs.
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )


def test_bootstrap_creates_matching_compose_and_docker_credentials(tmp_path, capsys):
    setup = tmp_path / "new" / "setup"
    configure.main(["--directory", str(setup)])

    compose_values = configure.read_compose_env(setup / ".env")
    docker_values = read_docker_env(setup / "docker.env")
    assert docker_values == compose_values
    credentials = (setup / "bootstrap-credentials.txt").read_text()
    password = re.search(r"^Password: (.+)$", credentials, re.MULTILINE).group(1)
    assert verify_password(password, docker_values["GDEPLOY_ADMIN_PASSWORD_HASH"])
    assert len(base64.urlsafe_b64decode(docker_values["GDEPLOY_SECRET_KEY"])) == 32
    assert "GDEPLOY_ADMIN_PASSWORD_HASH='scrypt$" in (setup / ".env").read_text()
    assert "GDEPLOY_ADMIN_PASSWORD_HASH=scrypt$" in (setup / "docker.env").read_text()
    for name in (".env", "docker.env", "bootstrap-credentials.txt"):
        assert stat.S_IMODE((setup / name).stat().st_mode) == 0o600
    assert stat.S_IMODE((setup / "media").stat().st_mode) == 0o755

    output = capsys.readouterr().out
    assert str(setup / "bootstrap-credentials.txt") in output
    assert password not in output
    assert docker_values["GDEPLOY_SECRET_KEY"] not in output
    assert docker_values["GDEPLOY_ADMIN_PASSWORD_HASH"] not in output


def test_bootstrap_defaults_to_script_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(configure, "__file__", str(tmp_path / "scripts" / "configure.py"))
    configure.main([])
    assert (tmp_path / ".env").is_file()
    assert (tmp_path / "docker.env").is_file()


def test_bootstrap_includes_existing_media_checksums(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (media / "ubuntu.iso").write_bytes(b"ubuntu installer")
    (media / "splunk.tgz").write_bytes(b"splunk installer")
    configure.main(["--directory", str(tmp_path)])

    values = read_docker_env(tmp_path / "docker.env")
    assert values["GDEPLOY_UBUNTU_SHA256"] == hashlib.sha256(b"ubuntu installer").hexdigest()
    assert values["GDEPLOY_SPLUNK_SHA256"] == hashlib.sha256(b"splunk installer").hexdigest()


@pytest.mark.parametrize("existing", [".env", "docker.env", "bootstrap-credentials.txt"])
def test_bootstrap_refuses_any_existing_configuration(tmp_path, existing):
    destination = tmp_path / existing
    destination.write_text("preserve me")
    with pytest.raises(SystemExit, match="Configuration already exists"):
        configure.main(["--directory", str(tmp_path)])
    assert destination.read_text() == "preserve me"
    assert {path.name for path in tmp_path.iterdir()} == {existing}


def test_bootstrap_repeat_keeps_all_credentials(tmp_path):
    configure.main(["--directory", str(tmp_path)])
    original = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    with pytest.raises(SystemExit, match="Configuration already exists"):
        configure.main(["--directory", str(tmp_path)])
    assert original == {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}


def test_bootstrap_refuses_dangling_configuration_symlink(tmp_path):
    (tmp_path / "docker.env").symlink_to(tmp_path / "missing.env")
    with pytest.raises(SystemExit, match="Configuration already exists"):
        configure.main(["--directory", str(tmp_path)])
    assert not (tmp_path / ".env").exists()


def test_export_preserves_custom_settings_and_credentials(tmp_path, capsys):
    configure.main(["--directory", str(tmp_path)])
    credentials = (tmp_path / "bootstrap-credentials.txt").read_bytes()
    original_values = configure.read_compose_env(tmp_path / ".env")
    with (tmp_path / ".env").open("a") as stream:
        stream.write(
            "\n# Custom settings\n"
            'GDEPLOY_SESSION_HOURS="12" # quoted comment\n'
            "SSL_CERT_FILE='/certificates/company CA.pem'\n"
            'REQUESTS_CA_BUNDLE="/certificates/company CA.pem"\n'
            "export FORWARDED_ALLOW_IPS=127.0.0.1,172.18.0.2 # reverse proxy\n"
            "UNRELATED_COMPOSE_VARIABLE=${SHELL_VARIABLE}\n"
        )
    original_env = (tmp_path / ".env").read_bytes()
    (tmp_path / "docker.env").write_text("outdated settings")
    (tmp_path / "docker.env").chmod(0o644)
    capsys.readouterr()

    configure.main(["--directory", str(tmp_path), "--export-docker-env"])

    actual = read_docker_env(tmp_path / "docker.env")
    assert actual == {
        **original_values,
        "GDEPLOY_SESSION_HOURS": "12",
        "SSL_CERT_FILE": "/certificates/company CA.pem",
        "REQUESTS_CA_BUNDLE": "/certificates/company CA.pem",
        "FORWARDED_ALLOW_IPS": "127.0.0.1,172.18.0.2",
    }
    assert (tmp_path / ".env").read_bytes() == original_env
    assert (tmp_path / "bootstrap-credentials.txt").read_bytes() == credentials
    assert stat.S_IMODE((tmp_path / "docker.env").stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".docker.env.*.tmp"))
    assert original_values["GDEPLOY_SECRET_KEY"] not in capsys.readouterr().out


def test_export_existing_installation_does_not_generate_credentials(tmp_path):
    (tmp_path / ".env").write_text("GDEPLOY_SECRET_KEY='old-key'\nGDEPLOY_ADMIN_PASSWORD_HASH='scrypt$old$salt'\n")
    configure.main(["--directory", str(tmp_path), "--export-docker-env"])
    assert read_docker_env(tmp_path / "docker.env") == {
        "GDEPLOY_SECRET_KEY": "old-key",
        "GDEPLOY_ADMIN_PASSWORD_HASH": "scrypt$old$salt",
    }
    assert not (tmp_path / "bootstrap-credentials.txt").exists()
    assert not (tmp_path / "media").exists()


@pytest.mark.parametrize(
    "setting,expected",
    [
        ("GDEPLOY_OS_TIMEOUT=${TIMEOUT:-3600}", "Unsupported interpolation"),
        ('GDEPLOY_OS_TIMEOUT="$TIMEOUT"', "Unsupported interpolation"),
        ("GDEPLOY_ADMIN_USERNAME=$(touch bad-file)", "Unsupported interpolation"),
        ("GDEPLOY_OS_TIMEOUT='unterminated", "Unclosed or multiline"),
        ("GDEPLOY_OS_TIMEOUT='3600'garbage", "Unsupported text after quoted value"),
        ('GDEPLOY_ADMIN_USERNAME="line\\nbreak"', "cannot be represented"),
        ('GDEPLOY_ADMIN_USERNAME="unsupported\\q"', "Unsupported escape"),
        ("GDEPLOY_OS_TIMEOUT=10\nGDEPLOY_OS_TIMEOUT=20", "Duplicate setting"),
        ("malformed setting", "Expected KEY=value"),
    ],
)
def test_export_rejects_ambiguous_values_without_replacing_file(tmp_path, setting, expected):
    (tmp_path / ".env").write_text(
        "GDEPLOY_SECRET_KEY='original-key'\nGDEPLOY_ADMIN_PASSWORD_HASH='scrypt$old$salt'\n" + setting + "\n"
    )
    (tmp_path / "docker.env").write_text("unchanged")
    with pytest.raises(SystemExit, match=expected):
        configure.main(["--directory", str(tmp_path), "--export-docker-env"])
    assert (tmp_path / "docker.env").read_text() == "unchanged"
    assert not (tmp_path / "bad-file").exists()
    assert not list(tmp_path.glob(".docker.env.*.tmp"))


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("'scrypt$salt$digest'", "scrypt$salt$digest"),
        ("'let\\'s keep $literal' # comment", "let's keep $literal"),
        ('"path\\\\to\\\\file"', "path\\to\\file"),
        ('"say \\"hello\\""', 'say "hello"'),
        ("https://example.invalid/#fragment # comment", "https://example.invalid/#fragment"),
        ("value # comment", "value"),
        (" # comment", ""),
        ("#literal", "#literal"),
        ("", ""),
    ],
)
def test_dotenv_literal_parsing(raw, expected):
    assert configure._dotenv_value(raw, 1) == expected


def test_export_requires_existing_configuration(tmp_path):
    with pytest.raises(SystemExit, match="Could not export docker.env"):
        configure.main(["--directory", str(tmp_path), "--export-docker-env"])
    assert not (tmp_path / "docker.env").exists()


@pytest.mark.parametrize("missing", ["GDEPLOY_SECRET_KEY", "GDEPLOY_ADMIN_PASSWORD_HASH"])
def test_export_requires_original_secrets(tmp_path, missing):
    values = {"GDEPLOY_SECRET_KEY": "original-key", "GDEPLOY_ADMIN_PASSWORD_HASH": "scrypt$old$salt"}
    values[missing] = ""
    (tmp_path / ".env").write_text("".join(f"{key}='{value}'\n" for key, value in values.items()))
    with pytest.raises(SystemExit, match=f"{missing} is missing or empty"):
        configure.main(["--directory", str(tmp_path), "--export-docker-env"])
    assert not (tmp_path / "docker.env").exists()
