from __future__ import annotations

import ipaddress
import logging
import re
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path

from .certificate_trust import certificate_endpoint
from .guest import GuestConnectionError, GuestSession, build_seed_iso, generate_ssh_key
from .media import MediaError, MediaManager
from .packages import PackageError, SplunkPackageManager
from .vmware import ESXiClient


class DeploymentError(RuntimeError):
    pass


def safe_error(error, secret_data=None):
    text = str(error) or type(error).__name__

    def redact(value):
        nonlocal text
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"public_key", "authorized_ssh_keys"}:
                    public_keys = child if isinstance(child, list) else [child]
                    for public_key in public_keys:
                        if isinstance(public_key, str) and public_key:
                            text = text.replace(public_key, "[redacted public key]")
                            parts = public_key.split()
                            if len(parts) >= 2:
                                text = text.replace(parts[1], "[redacted public key]")
                elif (
                    any(
                        word in key
                        for word in (
                            "password",
                            "private_key",
                            "token",
                            "encryption_key",
                            "security_key",
                            "reporting_key",
                        )
                    )
                    and isinstance(child, str)
                    and child
                ):
                    text = text.replace(child, "[redacted]")
                else:
                    redact(child)
        elif isinstance(value, list):
            for child in value:
                redact(child)

    redact(secret_data or {})
    text = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[redacted private key]", text, flags=re.S
    )
    return text[:2000]


class DeploymentService:
    def __init__(self, db, config, client_factory=ESXiClient):
        self.db, self.config, self.client_factory = db, config, client_factory
        self.media = MediaManager(db, config)
        self.packages = SplunkPackageManager(db, config)
        self.action_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.worker_error = False

    def start(self):
        self.db.recover()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="gdeploy-worker")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def client(self, settings):
        if not settings:
            raise DeploymentError("Configure the ESXi connection in Setup first.")
        options = dict(settings)
        # Trust must use either the approved endpoint certificate or the CA
        # store, including older deployment snapshots that disabled validation.
        options["verify_tls"] = True
        _, _, endpoint = certificate_endpoint(options["host"])
        trusted = self.db.esxi_certificate(endpoint)
        if trusted:
            options["trusted_certificate"] = trusted["pem"]
        return self.client_factory(**options)

    def inventory(self):
        settings = self.db.settings()
        try:
            with self.client(settings) as esxi:
                return esxi.inventory()
        except Exception as exc:
            raise DeploymentError(safe_error(exc, settings)) from None

    def preflight(self, spec, settings=None, exclude_id=None, os_media=None, splunk_package=None):
        settings = settings or self.db.settings()
        checks = []

        def check(name, ok, message):
            checks.append({"name": name, "ok": bool(ok), "message": message})

        os_media = self.media.selected() if os_media is None else os_media
        iso_size = None
        try:
            if not os_media:
                raise MediaError("Select and verify an OS ISO in Setup first.")
            source = self.media.validate_snapshot(os_media)
            iso_size = source.stat().st_size
            check("OS ISO", True, f"{os_media['name']} is available and its SHA-256 matches.")
        except (MediaError, OSError) as exc:
            check("OS ISO", False, str(exc))
        if any(vm["role"] == "splunk" for vm in spec["vms"]):
            package = self.packages.selected() if splunk_package is None else splunk_package
            try:
                if not package:
                    raise PackageError("Upload or select a Splunk Enterprise Linux x86_64 .tgz and verify its publisher SHA-256.")
                self.packages.validate_snapshot(package)
                check("Splunk Linux x86_64 package", True, f"{package['name']} is available; SHA-256 and archive checks passed.")
            except (PackageError, OSError) as exc:
                check("Splunk Linux x86_64 package", False, f"Open Setup → Software packages. {exc}")
                checks[-1]["action"] = {"label": "Configure Splunk package", "href": "#settings/packages"}
        check(
            "ISO builder",
            shutil.which("xorriso"),
            "xorriso is available."
            if shutil.which("xorriso")
            else "The ISO builder is missing; use the supplied Docker image.",
        )
        if iso_size is not None:
            needed = iso_size * 1.2 + 512 * 1024**2
            free = shutil.disk_usage(self.config.data_dir).free
            check(
                "Local workspace",
                free >= needed,
                f"{free / 1024**3:.1f} GiB free; at least {needed / 1024**3:.1f} GiB needed for one temporary ISO.",
            )
        if not settings:
            check("ESXi connection", False, "Save the ESXi connection in Setup.")
            return {"ok": False, "checks": checks}
        try:
            with self.client(settings) as esxi:
                inventory = esxi.inventory()
            check("ESXi connection", True, "Connected to standalone ESXi; inventory is readable.")
        except Exception as exc:
            check("ESXi connection", False, safe_error(exc, settings))
            return {"ok": False, "checks": checks}
        if "api_write_supported" in inventory["host"]:
            check(
                "Provisioning API",
                inventory["host"]["api_write_supported"],
                "ESXi license supports API writes."
                if inventory["host"]["api_write_supported"]
                else "This ESXi license does not support provisioning API writes.",
            )
        host = inventory["host"]
        requested_names = {vm["name"] for vm in spec["vms"]}
        names = {vm["name"] for vm in inventory["vms"]}
        retained = [d for d in self.db.retained() if d["id"] != exclude_id]
        pending = [d for d in retained if d["status"] in {"queued", "running", "cleaning"}]
        reserved = [vm for d in pending for vm in d["vms"]]
        conflicts = requested_names & (names | {vm["name"] for vm in reserved})
        check(
            "VM names",
            not conflicts,
            "Names are available."
            if not conflicts
            else "Names already in use or queued: " + ", ".join(sorted(conflicts)),
        )
        retained_vms = [
            vm
            for d in retained
            for vm in d["vms"]
            if vm.get("vm_id") or d["status"] in {"queued", "running", "cleaning"}
        ]
        active_addresses = {vm["address"].split("/")[0] for vm in retained_vms if vm.get("address")}
        active_addresses.update(vm["ip"] for vm in retained_vms if vm.get("ip"))
        requested_addresses = {vm["address"].split("/")[0] for vm in spec["vms"] if vm.get("address")}
        address_conflicts = active_addresses & requested_addresses
        check(
            "Static address reservations",
            not address_conflicts,
            "No conflicting retained or queued VM addresses. Confirm addresses are unused on your LAN."
            if not address_conflicts
            else "Addresses already in use or reserved: " + ", ".join(sorted(address_conflicts)),
        )
        check(
            "CPU limits",
            all(vm["cpu"] <= host["cpu_threads"] for vm in spec["vms"]),
            f"Host exposes {host['cpu_threads']} logical CPUs; CPU overcommit remains an administrator decision.",
        )
        # Conservatively count all pending reservations, including powered-on work.
        free_memory = host.get("free_memory_gb", host["memory_gb"])
        ram = sum(vm["ram_gb"] for vm in spec["vms"] + reserved)
        check(
            "Memory capacity",
            ram + 2 <= free_memory,
            f"{ram} GiB requested/reserved plus 2 GiB headroom; {free_memory:.1f} GiB host memory available.",
        )
        networks = {network["name"] for network in inventory["networks"]}
        stores = {store["name"]: store for store in inventory["datastores"]}
        iso_gb = iso_size / 1024**3 if iso_size is not None else 6
        for vm in spec["vms"]:
            check(f"{vm['name']} network", vm["network"] in networks, f"Port group: {vm['network']}")
        for name in {vm["datastore"] for vm in spec["vms"]}:
            space = sum(
                vm["disk_gb"] + iso_gb + vm["ram_gb"] for vm in spec["vms"] + reserved if vm["datastore"] == name
            )
            store = stores.get(name)
            check(
                f"Datastore {name}",
                store is not None and store["free_gb"] >= space,
                f"{space:.1f} GiB required including disks, install media and VM swap; {store['free_gb']:.1f} GiB free."
                if store
                else "Datastore is unavailable.",
            )
        return {"ok": all(item["ok"] for item in checks), "checks": checks}

    def enqueue(self, spec, settings=None, parent_id=None):
        settings = settings or self.db.settings()
        os_media = self.media.selected()
        splunk_package = self.packages.selected() if any(vm["role"] == "splunk" for vm in spec["vms"]) else None
        result = self.preflight(
            spec, settings, exclude_id=parent_id, os_media=os_media or {}, splunk_package=splunk_package or {},
        )
        if not result["ok"]:
            raise DeploymentError(
                "Preflight failed: "
                + "; ".join(c["name"] + ": " + c["message"] for c in result["checks"] if not c["ok"])
            )
        deployment_id = str(uuid.uuid4())
        vm_credentials = {}
        for vm in spec["vms"]:
            private, public = generate_ssh_key()
            vm_credentials[vm["name"]] = {
                "username": "gdeploy",
                "password": secrets.token_urlsafe(24),
                "private_key": private,
                "public_key": public,
                "services": [],
            }
        data = {
            "esxi": settings,
            "os_media": os_media,
            "splunk_package": splunk_package,
            "authorized_ssh_keys": self.db.ssh_public_keys(),
            "vm_credentials": vm_credentials,
            "software": {
                key: secrets.token_hex(24)
                for key in (
                    "elastic_password",
                    "splunk_password",
                    "kibana_encryption_key",
                    "kibana_security_key",
                    "kibana_reporting_key",
                )
            },
        }
        self.db.create(deployment_id, spec, data, parent_id)
        return self.db.get(deployment_id)

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                deployment_id = self.db.claim()
                if deployment_id:
                    self.run(deployment_id)
                else:
                    self.stop_event.wait(2)
                self.worker_error = False
            except Exception as exc:
                # Disk/database failures must not permanently kill the only worker.
                self.worker_error = True
                logging.error("Deployment worker storage/state failure (%s)", type(exc).__name__)
                self.stop_event.wait(10)

    def stage(self, deployment_id, stage, message):
        self.db.update(deployment_id, stage=stage)
        self.db.event(deployment_id, message)

    def _wait_for_guest(self, esxi, vm, credential, deployment_id):
        deadline = time.monotonic() + self.config.os_timeout
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                raise DeploymentError("Service shutdown requested during OS installation.")
            ip = esxi.guest_ip(vm["vm_id"])
            expected = str(ipaddress.IPv4Interface(vm["address"]).ip) if vm.get("address") else None
            if ip and (not expected or ip == expected):
                try:
                    with GuestSession(
                        ip,
                        credential["username"],
                        credential["private_key"],
                        credential["password"],
                        credential.get("host_key"),
                    ) as guest:
                        vm["ip"] = ip
                        credential["host_key"] = guest.host_key
                        # Persist the first authenticated SSH host key before running anything.
                        current = self.db.get(deployment_id, private=True)
                        current["secrets"]["vm_credentials"][vm["name"]] = credential
                        self.db.update(deployment_id, secrets=current["secrets"])
                        guest.wait_ready(
                            timeout=max(1, int(deadline - time.monotonic())),
                            log=lambda text, level: self.db.event(
                                deployment_id, safe_error(f"{vm['name']}: {text}", current["secrets"]), level,
                            ),
                        )
                        return
                except GuestConnectionError as exc:
                    # Installation can expose an IP before SSH is ready. Host-key
                    # mismatches and cloud-init failures propagate immediately.
                    self.db.event(
                        deployment_id, safe_error(
                            f"{vm['name']}: {exc} Retrying within the OS installation timeout.", {"credential": credential},
                        ),
                    )
            self.stop_event.wait(min(10, max(0, deadline - time.monotonic())))
        raise DeploymentError(
            f"Timed out waiting for the operating system on {vm['name']}. Check its ESXi console, DHCP/static network, OS package mirror access, and TCP 22 reachability from GDeploy."
        )

    def run(self, deployment_id):
        deployment = None
        secret_data = {}
        vms = []
        artifact_dir = self.config.data_dir / "artifacts" / deployment_id
        try:
            deployment = self.db.get(deployment_id, private=True)
            if not deployment:
                raise DeploymentError("Deployment record is unavailable.")
            secret_data = deployment["secrets"]
            # Jobs created before media selection existed retain their environment
            # configuration. A later Setup edit must not switch a queued job's ISO.
            os_media = secret_data.get("os_media") or self.media.legacy()
            # Never adopt a later Setup selection for an already queued job.
            # Jobs from before package selection retain their environment source.
            splunk_package = secret_data.get("splunk_package") if "splunk_package" in secret_data else self.packages.legacy()
            vms = deployment["vms"]
            resources = deployment["resources"]
            artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.stage(deployment_id, "preflight", "Checking media, host capacity and deployment inputs")
            result = self.preflight(
                deployment["spec"], secret_data["esxi"], exclude_id=deployment_id, os_media=os_media or {},
                splunk_package=splunk_package or {},
            )
            if not result["ok"]:
                raise DeploymentError(
                    "; ".join(c["name"] + ": " + c["message"] for c in result["checks"] if not c["ok"])
                )
            with self.client(secret_data["esxi"]) as esxi:
                for vm in vms:
                    if self.stop_event.is_set():
                        raise DeploymentError("Service shutdown requested.")
                    credential = secret_data["vm_credentials"][vm["name"]]
                    self.stage(deployment_id, "preparing", f"Preparing unattended OS installation for {vm['name']}")
                    iso = artifact_dir / (vm["name"] + ".iso")
                    if os_media:
                        # A mounted file can be replaced while an earlier VM installs.
                        self.media.validate_snapshot(os_media)
                    build_seed_iso(
                        Path(os_media["path"]) if os_media else self.config.ubuntu_iso,
                        iso,
                        vm,
                        credential["username"],
                        credential["password"],
                        credential["public_key"],
                        # Missing snapshots are legacy jobs: never adopt keys
                        # added to Setup after that deployment was queued.
                        authorized_ssh_keys=secret_data.get("authorized_ssh_keys", []),
                        log=lambda text, level: self.db.event(deployment_id, safe_error(text, secret_data), level),
                    )
                    remote = f"gdeploy/{deployment_id}/{vm['name']}.iso"
                    resources.append({"datastore": vm["datastore"], "path": remote})
                    self.db.update(deployment_id, resources=resources)
                    self.stage(deployment_id, "creating", f"Uploading installation media and creating {vm['name']}")
                    esxi.upload_iso(vm["datastore"], remote, iso)
                    iso.unlink(missing_ok=True)
                    vm["vm_id"] = esxi.create_vm(vm, f"[{vm['datastore']}] {remote}", deployment_id)
                    vm["status"] = "installing_os"
                    self.db.update(deployment_id, vms=vms)
                    esxi.power_on(vm["vm_id"], deployment_id)
                    self.stage(
                        deployment_id,
                        "installing_os",
                        f"Installing the operating system on {vm['name']}; waiting for VMware Tools and SSH",
                    )
                    self._wait_for_guest(esxi, vm, credential, deployment_id)
                    self.db.update(deployment_id, secrets=secret_data)
                    esxi.detach_iso(vm["vm_id"], deployment_id)
                    esxi.delete_iso(vm["datastore"], remote, deployment_id)
                    resources.remove({"datastore": vm["datastore"], "path": remote})
                    vm["status"] = "os_ready"
                    self.db.update(deployment_id, vms=vms, resources=resources)
                    self.db.event(
                        deployment_id, f"Operating system ready on {vm['name']} ({vm['ip']}); installation media removed"
                    )
                elastic = None
                for vm in sorted(
                    vms, key=lambda item: {"elasticsearch": 0, "kibana": 1, "splunk": 2, "ubuntu": 3}[item["role"]]
                ):
                    credential = secret_data["vm_credentials"][vm["name"]]
                    # OS installation may take time: a mounted installer can be
                    # replaced since preflight. Recheck the snapshotted bytes.
                    package_path = self.packages.validate_snapshot(splunk_package or {}) if vm["role"] == "splunk" else None
                    role_label = "operating system" if vm["role"] == "ubuntu" else vm["role"]
                    self.stage(deployment_id, "installing_software", f"Configuring {role_label} on {vm['name']}")
                    vm["status"] = "installing_software"
                    self.db.update(deployment_id, vms=vms)
                    with GuestSession(
                        vm["ip"],
                        credential["username"],
                        credential["private_key"],
                        credential["password"],
                        credential["host_key"],
                    ) as guest:
                        installed = guest.install(
                            vm["role"],
                            secret_data["software"],
                            elastic=elastic,
                            splunk_package=package_path,
                            splunk_sha256=splunk_package["sha256"] if package_path is not None else None,
                            log=lambda text: self.db.event(deployment_id, safe_error(text, secret_data)),
                        )
                    if installed.get("elastic"):
                        elastic = installed["elastic"]
                        secret_data["elastic"] = elastic
                    credential["services"] = installed.get("services", [])
                    vm["services"] = [
                        {"name": service["name"], "url": service["url"]} for service in credential["services"]
                    ]
                    vm["status"] = "completed"
                    self.db.update(deployment_id, vms=vms, secrets=secret_data)
                self.stage(deployment_id, "verifying", "OS readiness and application health checks passed")
            self.db.update(deployment_id, status="completed", stage="completed")
            self.db.event(
                deployment_id, "Deployment completed. Open Credentials for VM and application sign-in details."
            )
        except Exception as exc:
            error = safe_error(exc, secret_data)
            status = "interrupted" if self.stop_event.is_set() else "failed"
            updates = {"status": status, "stage": status, "error": error}
            if deployment is not None:
                updates.update(vms=vms, secrets=secret_data)
            self.db.update(deployment_id, **updates)
            self.db.event(deployment_id, error, "error")
        finally:
            # Only our own local working directory; shared source media is never removed.
            shutil.rmtree(artifact_dir, ignore_errors=True)

    def redeploy(self, deployment_id, confirm_name):
        deployment = self.db.get(deployment_id, private=True)
        if not deployment:
            raise DeploymentError("Deployment not found.")
        if deployment["status"] not in {"failed", "interrupted", "cleanup_failed"}:
            raise DeploymentError("Only failed or interrupted deployments can be deleted and redeployed.")
        if confirm_name != deployment["name"]:
            raise DeploymentError("Type the exact deployment name to confirm deletion.")
        self.db.update(deployment_id, status="cleaning", stage="cleaning", error=None)
        self.db.event(
            deployment_id,
            "Administrator confirmed permanent deletion of this deployment's VMs and disks, followed by redeployment.",
            "warning",
        )
        try:
            with self.client(deployment["secrets"]["esxi"]) as esxi:
                owned = esxi.find_owned_vms(deployment_id)
                ids = {item["vm_id"] for item in owned} | {vm["vm_id"] for vm in deployment["vms"] if vm.get("vm_id")}
                for vm_id in sorted(ids):
                    esxi.destroy_vm(vm_id, deployment_id)
                    self.db.event(deployment_id, "Deleted owned VM " + vm_id)
                for resource in deployment["resources"]:
                    esxi.delete_iso(resource["datastore"], resource["path"], deployment_id)
            # Mark cleanup complete before re-running read-only preflight; no replacement on failure.
            self.db.update(deployment_id, resources=[])
            replacement = self.enqueue(deployment["spec"], deployment["secrets"]["esxi"], parent_id=deployment_id)
            self.db.update(deployment_id, status="reverted", stage="reverted", error=None)
            self.db.event(deployment_id, "Cleanup complete; replacement deployment queued: " + replacement["id"])
            return replacement
        except Exception as exc:
            error = safe_error(exc, deployment["secrets"])
            self.db.update(deployment_id, status="cleanup_failed", stage="cleanup_failed", error=error)
            self.db.event(deployment_id, "Delete/redeploy stopped: " + error, "error")
            raise DeploymentError(error) from None

    def credentials(self, deployment_id):
        deployment = self.db.get(deployment_id, private=True)
        if not deployment:
            raise DeploymentError("Deployment not found.")
        result = []
        for vm in deployment["vms"]:
            credential = deployment["secrets"]["vm_credentials"][vm["name"]]
            result.append(
                {
                    "name": vm["name"],
                    "role": vm["role"],
                    "ip": vm.get("ip"),
                    "username": credential["username"],
                    "password": credential["password"],
                    "ssh_host_key": credential.get("host_key"),
                    "services": credential.get("services", []),
                }
            )
        self.db.event(deployment_id, "Administrator revealed deployment credentials.", "audit")
        return {"vms": result}
