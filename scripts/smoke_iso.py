"""Exercise real xorriso as the container user, with a disk-backed build >256 MiB.

This synthetic ISO tests remastering and permissions, not an Ubuntu boot/install.
Invoked inside the running release-smoke container; no ESXi access is involved.
"""

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

from gdeploy.guest import build_seed_iso


def xorriso(*arguments):
    subprocess.run(["xorriso", *map(str, arguments)], check=True, capture_output=True, timeout=120)


def main():
    assert os.getuid() == 10001, "Test must use the production non-root container user"
    with tempfile.TemporaryDirectory(prefix="gdeploy-iso-smoke-", dir="/data") as directory:
        root = Path(directory)
        tree = root / "tree"
        grub = tree / "boot" / "grub"
        grub.mkdir(parents=True)
        (tree / "casper").mkdir()
        boot_config = "set timeout=30\nmenuentry 'Installer' {\n linux /casper/vmlinuz ---\n initrd /casper/initrd\n}\n"
        for filename in ("grub.cfg", "loopback.cfg"):
            path = grub / filename
            path.write_text(boot_config)
            path.chmod(0o444)
        (grub / "eltorito.img").write_bytes(b"\0" * 8192)
        (tree / "casper" / "vmlinuz").write_bytes(b"synthetic kernel")
        (tree / "casper" / "initrd").write_bytes(b"synthetic initrd")
        with (tree / "casper" / "filesystem.squashfs").open("wb") as payload:
            payload.truncate(260 * 1024**2)
        manifest = tree / "md5sum.txt"
        manifest.write_text("0" * 32 + "  ./boot/grub/grub.cfg\n")
        manifest.chmod(0o444)
        source = root / "source.iso"
        xorriso(
            "-as", "mkisofs", "-r", "-V", "GDEPLOY_SMOKE", "-b", "boot/grub/eltorito.img",
            "-no-emul-boot", "-boot-load-size", "4", "-o", source, tree,
        )
        original = hashlib.file_digest(source.open("rb"), "sha256").hexdigest()
        extracted = root / "original-grub.cfg"
        xorriso("-osirrox", "on", "-indev", source, "-extract", "/boot/grub/grub.cfg", extracted)
        assert extracted.stat().st_mode & 0o222 == 0, "Fixture must preserve read-only ISO permissions"
        output = root / "artifacts" / "customized.iso"
        events = []
        build_seed_iso(
            source, output, {"name": "smoke-vm", "ip_mode": "dhcp"}, "gdeploy",
            "smoke-password-only", "ssh-rsa AAAA smoke", log=lambda text, level: events.append(text),
        )
        assert output.stat().st_size > 256 * 1024**2, "Build must exceed the /tmp tmpfs limit"
        assert output.stat().st_mode & 0o777 == 0o600
        assert hashlib.file_digest(source.open("rb"), "sha256").hexdigest() == original
        patched = root / "patched-grub.cfg"
        seed = root / "installed-user-data"
        xorriso("-osirrox", "on", "-indev", output, "-extract", "/boot/grub/grub.cfg", patched)
        xorriso("-osirrox", "on", "-indev", output, "-extract", "/nocloud/user-data", seed)
        assert "autoinstall noprompt" in patched.read_text()
        settings = yaml.safe_load(seed.read_text())["autoinstall"]
        assert settings["identity"]["hostname"] == "smoke-vm"
        assert settings["identity"]["password"].startswith("$6$")
        assert "smoke-password-only" not in "\n".join(events)
        assert settings["identity"]["password"] not in "\n".join(events)
    print("Real xorriso: read-only ISO metadata, non-root remastering, >256 MiB data-volume output, and secret-free diagnostics passed.")


if __name__ == "__main__":
    main()
