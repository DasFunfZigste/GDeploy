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

from gdeploy.guest import build_seed_iso, generate_ssh_key
from gdeploy.alma_guest import BOOT_CONFIGS, DVD_FILES, validate_install_iso


def xorriso(*arguments):
    subprocess.run(["xorriso", *map(str, arguments)], check=True, capture_output=True, timeout=120)


def alma_smoke(root, automation_key, extra_keys):
    tree = root / "alma-tree"
    tree.mkdir()
    (tree / ".treeinfo").write_text("""[general]
family = AlmaLinux
arch = x86_64
version = 10.0
[release]
name = AlmaLinux
version = 10.0
[tree]
arch = x86_64
[images-x86_64]
kernel = images/pxeboot/vmlinuz
initrd = images/pxeboot/initrd.img
[stage2]
mainimage = images/install.img
[variant-BaseOS]
repository = BaseOS
[variant-AppStream]
repository = AppStream
""")
    boot = "set timeout=60\nmenuentry 'Installer' {\n linux /images/pxeboot/vmlinuz inst.stage2=hd:LABEL=GDEPLOY_ALMA rd.live.check\n initrd /images/pxeboot/initrd.img\n}\n"
    for path in DVD_FILES:
        local = tree / path.lstrip("/")
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(boot if path in BOOT_CONFIGS else "synthetic DVD repository or installer data")
        local.chmod(0o444)
    (tree / "boot/grub2/eltorito.img").write_bytes(b"\0" * 8192)
    (tree / "images/efiboot.img").write_bytes(b"\0" * (256 * 1024))
    with (tree / "BaseOS/fixture-payload").open("wb") as payload:
        payload.truncate(260 * 1024**2)
    source = root / "alma-source.iso"
    xorriso("-as", "mkisofs", "-r", "-J", "-V", "GDEPLOY_ALMA", "-o", source,
            "-b", "boot/grub2/eltorito.img", "-no-emul-boot", "-boot-load-size", "4",
            "-eltorito-alt-boot", "-e", "images/efiboot.img", "-no-emul-boot", tree)
    with source.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    assert validate_install_iso(source, workspace_root=root / "alma-validation")["profile"] == "alma10-kickstart"
    events = []
    output = root / "artifacts" / "alma-customized.iso"
    build_seed_iso(source, output, {"name": "alma-sensor", "role": "corelight_sensor", "os_profile": "alma10-kickstart",
        "ip_mode": "dhcp", "dhcp_reserved": True, "management_mac": "00:50:56:01:02:03", "monitor_mac": "00:50:56:01:02:04"},
        "gdeploy", "alma-smoke-private-password", automation_key, authorized_ssh_keys=extra_keys,
        log=lambda text, level: events.append(text))
    assert output.stat().st_size > 256 * 1024**2
    assert output.stat().st_mode & 0o777 == 0o600
    with source.open("rb") as handle:
        assert hashlib.file_digest(handle, "sha256").hexdigest() == digest
    for number, path in enumerate(BOOT_CONFIGS):
        patched = root / f"alma-patched-{number}"
        xorriso("-osirrox", "on", "-indev", output, "-extract", path, patched)
        assert "inst.ks=cdrom:/gdeploy.ks" in patched.read_text()
        assert "ifname=gdeploymon:00:50:56:01:02:04" in patched.read_text()
        assert "rd.live.check" not in patched.read_text()
    settings = root / "alma-kickstart"
    xorriso("-osirrox", "on", "-indev", output, "-extract", "/gdeploy.ks", settings)
    kickstart = settings.read_text()
    assert "rootpw --lock" in kickstart and "--grow" in kickstart
    assert "method=disabled" in kickstart and "os-install-complete.json" in kickstart
    assert "alma-smoke-private-password" not in kickstart + "\n".join(events)
    for key in [automation_key, *extra_keys]:
        assert key in kickstart and key.split()[1] not in "\n".join(events)
    report = subprocess.run(["xorriso", "-indev", str(output), "-report_el_torito", "plain"], check=True, capture_output=True, text=True)
    assert "BIOS" in report.stdout + report.stderr and "UEFI" in report.stdout + report.stderr


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
        _, automation_key = generate_ssh_key()
        _, administrator_key = generate_ssh_key()
        _, second_administrator_key = generate_ssh_key()
        extra_keys = [administrator_key, second_administrator_key]
        build_seed_iso(
            source, output, {"name": "smoke-vm", "ip_mode": "dhcp"}, "gdeploy",
            "smoke-password-only", automation_key, authorized_ssh_keys=extra_keys,
            log=lambda text, level: events.append(text),
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
        assert settings["identity"]["username"] == "gdeploy"
        assert settings["identity"]["password"].startswith("$6$")
        installed_keys = settings["ssh"]["authorized-keys"]
        assert [key.split()[:2] for key in installed_keys] == [
            key.split()[:2] for key in [automation_key, *extra_keys]
        ], "Remastered ISO must preserve automation access and install every saved administrator key"
        assert settings["ssh"]["install-server"] and settings["ssh"]["allow-pw"]
        assert "smoke-password-only" not in "\n".join(events)
        assert settings["identity"]["password"] not in "\n".join(events)
        assert all(key.split()[1] not in "\n".join(events) for key in installed_keys)
        alma_smoke(root, automation_key, extra_keys)
    print("Ubuntu and Alma real xorriso: non-root remastering, read-only source metadata, >256 MiB output, administrator SSH keys, and redacted diagnostics passed.")


if __name__ == "__main__":
    main()
