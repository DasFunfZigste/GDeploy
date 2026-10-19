# Proxmox VE 9.2

GDeploy can provision dedicated Ubuntu 24.04 or AlmaLinux 10 VMs on Proxmox VE 9.x, targeting **9.2**. All existing software roles use the same application installers as ESXi: OS only, Splunk, Elasticsearch, Kibana, FleetManager and Corelight Software Sensor. Corelight on AlmaLinux 10 remains experimental; adding a hypervisor does not change vendor OS support.

## Add a host

1. Open **Setup → Virtualization hosts**.
2. Enter a display name, choose **Proxmox VE 9.2**, and enter its hostname/IP. HTTPS defaults to port **8006**; custom ports are accepted.
3. Choose API token authentication (`user@realm!tokenid` plus its token secret), or password authentication (`user@realm` plus password). Use an account dedicated to GDeploy. Password login requires an account usable without an interactive second factor; API tokens are recommended.
4. Enter the node name. You can leave it blank when the endpoint exposes exactly one online node. For a cluster, create an explicit node profile for each deployment target.
5. Enter active **ISO storage**, such as `local`, that accepts ISO content. Disk-only storage such as `local-lvm` cannot hold installation ISOs. Each VM's disk storage is selected separately during deployment.
6. For a self-signed host, select **Retrieve certificate**, compare its SHA-256 fingerprint with a trusted Proxmox source, then **Approve certificate**. Retrieval alone does not establish trust. Approval is bound to the exact hostname/IP and port; renewed, changed, expired or not-yet-valid certificates require attention. Ordinary CA and hostname verification applies without an approval. A private CA bundle can be mounted read-only and configured with `REQUESTS_CA_BUNDLE`.
7. Save the host, then select **Test connection** in its card. This loads the available ISO stores and bridges into the editor's suggestions. Set an optional **Default network**, usually `vmbr0`, then save again. Every VM can override that default.

Existing installations retain their default ESXi host automatically, shown alongside named hosts. The older singleton ESXi form remains available under **Default ESXi connection · legacy settings**. Its datastore ISO browser is unchanged. Secrets are encrypted and omitted from host-list responses; leave the secret blank when editing the same authentication identity to retain it.

## Account permissions

Preflight reads effective permissions before any provisioning. Grant propagated privileges at these paths to the user and, when token privilege separation is enabled, the token as well:

| Path | Required privileges |
| --- | --- |
| `/nodes/NODE` | `Sys.Audit` |
| `/vms` | `VM.Audit`, `VM.Allocate`, `VM.PowerMgmt`, `VM.Config.CPU`, `VM.Config.Memory`, `VM.Config.Disk`, `VM.Config.Network`, `VM.Config.CDROM`, `VM.Config.Options`, `VM.Config.HWType`, and `VM.GuestAgent.Audit` (or `VM.GuestAgent.Unrestricted`) |
| `/storage/DISK_STORE` | `Datastore.Audit`, `Datastore.AllocateSpace` |
| `/storage/ISO_STORE` | `Datastore.Audit`, `Datastore.AllocateTemplate` |
| `/sdn/zones/localnetwork/BRIDGE` | `SDN.Use`, for every management/monitoring bridge used |

Create an appropriate custom role using Proxmox's supported permissions UI. VM audit access must cover all VMs: otherwise inventory can silently omit machines and cleanup cannot verify ISO attachments, including VMs on other cluster nodes. GDeploy refuses cleanup when that access is absent. Scope the remaining privileges to the required node, stores and bridges. GDeploy does not modify host networking, create bridges, or configure SDN/VLAN/TAP/SPAN routing.

## Installation media and deployment

Under **Setup → OS installation media**, upload a publisher-verified ISO or select one mounted on the GDeploy server. Save a separate matching ISO for each OS. Proxmox media import over SSH is not included; its API does not download existing stored ISO bytes for local unattended-install preparation. GDeploy needs a local verified source to remaster it.

Open **New deployment**, choose the **Target virtualization host**, then the OS and software. There is no additional hypervisor selector. Configure each VM's name, CPU, RAM, disk storage, bridge and IPv4 settings. Complete any product configuration before preflight. The review shows the selected target.

GDeploy remasters a private ISO for each VM, streams it to the configured Proxmox ISO store, creates a QEMU VM and boots the installer. It uses `cpu=host`, q35/SeaBIOS, VirtIO SCSI disks, fixed RAM with ballooning disabled, VirtIO network adapters and an enabled QEMU Guest Agent channel. AlmaLinux requires x86-64-v3 CPU features. The installed OS includes QEMU Guest Agent and SSH; GDeploy waits for the management address, pins the observed SSH host key and verifies the installed OS, sudo, SSH, guest agent and installer-completion evidence. It then detaches/deletes its temporary ISO and installs the selected applications. The source ISO remains unchanged.

The Docker server must reach Proxmox HTTPS and each VM's SSH address. Guests need the same repository/internet access as the equivalent ESXi deployment. DHCP must supply working routing/DNS; static addresses must be unused and reachable. QEMU Guest Agent reports the management NIC's IPv4 address. It does not scan the LAN or prove an IP is unused.

Software Sensor gets separate management and monitoring VirtIO NICs, with distinct explicit MACs verified before media preparation. The monitoring NIC receives no IP configuration. Supply TAP/SPAN traffic on its selected bridge. Sensor management requires static IPv4 or a confirmed DHCP reservation. Fixed RAM and capacity checks do not create ESXi-style CPU reservations or guarantee dedicated scheduling; avoid competing workloads and validate packet capture under load.

## Recovery and updates

Deployment records retain their original target name/type and encrypted endpoint, node, authentication and ISO-storage snapshot. Editing or removing a Setup profile cannot redirect an existing job or its recovery. Credentials revoked on Proxmox still need operator attention; Setup edits do not overwrite historical snapshots. Active jobs prevent profile removal.

**Stop deployment** keeps existing VMs/disks and skips later work after the current operation reaches a safe boundary. It does not shut down a VM or stop an installer already running inside it. **Delete & redeploy** is a separate destructive action: it discovers UUID-owned VMs on the frozen target, validates disks and media ownership, and deletes only their resources. Unexpected disks/state storage, foreign media, changed configuration or attached/shared ISOs block cleanup. The adapter never requests deletion of unreferenced disks. Inspect the Proxmox task history if a request times out, because its task may still be running.

Update from the same clone:

```sh
git pull --ff-only
sudo docker compose up -d --build --wait
```

Back up the complete data volume and original encryption key first; finish or resolve active jobs. The encrypted `target_hosts` table is additive. Releases before v0.17.0 do not understand Proxmox jobs or named target profiles: finish/resolve those jobs and use a matching pre-upgrade backup for rollback. Retain ISO files, bootstrap/environment files, certificate approvals, history and guest credentials. Updating or rolling back the app does not resume failed jobs, change installed VMs, undo deleted disks, or reverse vendor pairing-token use.

## Validation status

Automated tests cover API contracts, real local HTTPS/certificate enforcement and multipart uploads, encrypted profiles, frozen targets, capacity isolation, Ubuntu/Alma guest-agent selection, application dispatch and browser behavior. They do not establish a successful unattended install on a live Proxmox host. Use the [lab checklist](LAB_VALIDATION.md) to validate both OS installers, each licensed application, bridge connectivity, sensor capture, stopping and cleanup on your Proxmox VE 9.2 system.

References: [Proxmox VE API](https://pve.proxmox.com/wiki/Proxmox_VE_API), [API viewer](https://pve.proxmox.com/pve-docs/api-viewer/).
