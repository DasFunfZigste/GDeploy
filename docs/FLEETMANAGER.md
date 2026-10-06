# Deploy Corelight FleetManager

GDeploy creates a dedicated Ubuntu Server 24.04 LTS amd64 VM for **Corelight Fleet Manager**, labeled **FleetManager** in the UI. Select it in **New deployment** to use this flow: **Choose software → Configure VMs → FleetManager configuration → Review & deploy**. The FleetManager step appears before preflight and reuses saved defaults; complete and save it before continuing to review.

**Setup → Software packages → FleetManager** remains available for saving defaults and managing packages outside a deployment, including at `http://SERVER_LAN_IP:8000/#settings/packages/fleetmanager`. Deployments without FleetManager retain the three-step wizard without the FleetManager configuration step.

This walkthrough follows the **Corelight Fleet Manager User Guide, release 29.2.2, updated October 2, 2026**, especially the requirements and Linux installation instructions on PDF pages 7–15. Use the version of Fleet Manager supported by your Corelight entitlement and Ubuntu release. GDeploy's automated checks do not establish that a particular licensed package works in your environment; complete the [lab acceptance checklist](LAB_VALIDATION.md#fleetmanager-installation) with your actual ESXi host and Corelight license.

## Prepare the VM and network

The deployment wizard defaults to **2 vCPUs, 8 GiB RAM and an 80 GiB disk**. GDeploy requires at least **2 vCPUs, 8 GiB RAM and a 60 GiB disk**; allow more for sensor count, retention, backups and offline sensor updates. Corelight's small-fleet guidance covers 1–10 sensors at 2 vCPUs/8 GB, with larger fleets needing additional resources.

GDeploy uses the full VM disk for the Ubuntu installation. `/var` and `/tmp` normally share the root filesystem; they are not separate reserved partitions. Before installing Fleet Manager, the guest check requires at least **30 GiB available at `/var`** and **20 GiB available at `/tmp`**. An ESXi disk allocation alone does not prove those amounts are free inside Ubuntu.

| Connection | Required access |
| --- | --- |
| GDeploy to ESXi | HTTPS 443 for VM creation and installation media |
| GDeploy to the new VM | SSH 22 for configuration and verification |
| Administrator browser to Fleet Manager | HTTPS 443 |
| Corelight sensor management interfaces to Fleet Manager | TCP 1443, using the shared community string and product identity |
| Online Fleet Manager guest | DNS, Ubuntu package mirrors, and HTTPS to `pkgrepos.corelight.cloud` |
| Offline Fleet Manager guest | Local ESXi/GDeploy/network services remain reachable; installation uses the ISO and supplied packages |

Choose a static address or DHCP reservation. GDeploy does not create upstream firewall rules, enroll sensors, configure external data-feed connectivity or install your organization's monitoring and backup tools.

## Gather the required credentials

Both installation modes need:

- A strong, random **community string**, shared later with the sensors you choose to enroll. GDeploy accepts 1–4096 printable characters and disallows single/double quotes. Keep the value in your password manager; Setup shows whether it is saved without revealing it, and the deployment's authenticated **Credentials** panel can reveal the value used by that job.
- The customer-specific **product identity `.pem` license** supplied by Corelight. This file contains its certificate and unencrypted private key and must be no larger than **64 KiB**. GDeploy verifies that the first certificate matches the key and is currently valid. The key may appear before or after the certificate in the file; any additional certificate chain must follow the matching product identity certificate. A regular web certificate does not substitute for a Corelight entitlement. Only Fleet Manager can validate the actual product license.

**Online repository** additionally needs the customer repository token. Sign in to [Corelight Cloud](https://my.corelight.cloud/), open **Downloads → Fleet Manager**, and copy the authentication token at the top of that page. Paste it into GDeploy's repository token field. Do not put it into a browser URL, shell command, screenshot or support log.

Setup stores the community string, repository token and PEM encrypted in the existing GDeploy database. Responses show saved-state flags and license name/checksum/expiry, never the raw license, key or token. When editing an existing configuration, blank secret fields and no replacement PEM retain the saved values.

## Online installation

1. Open **New deployment**, select **FleetManager** in **Choose software**, then continue to **Configure VMs**.
2. Set its distinct VM name, CPU, RAM, disk, datastore, port group and DHCP/static settings. Other selected applications keep their own VMs and resources. Continue to **FleetManager configuration**.
3. Choose **Online repository**. Enter the community string and Corelight repository token, and choose the product identity `.pem` file. Existing saved values are reused; leave a secret field blank and omit a replacement PEM to retain them.
4. Select **Save & continue** to validate and save the configuration, then open **Review & deploy**. These settings also become the defaults for future FleetManager deployments.
5. In **Review & deploy**, explicitly run preflight, review its results and start the deployment. Saving configuration does not run preflight or queue a job automatically. If a later check reports changed or missing configuration, return to the FleetManager step, correct it and run preflight again.

The new VM installs Ubuntu, obtains the Corelight stable repository signing key over verified HTTPS, configures the authenticated apt repository and installs `corelight-fleet`. Ubuntu and repository access are required from the guest. Saving online mode clears unused offline package selections; it does not delete their uploaded files.

The repository remains configured for ordinary package administration:

- `/etc/apt/sources.list.d/corelight_fleet-stable.list`
- `/etc/apt/keyrings/corelight_fleet-stable-archive-keyring.gpg`
- `/etc/apt/auth.conf.d/corelight_fleet-stable.conf`, readable only by root

GDeploy does not automatically upgrade an existing Fleet Manager VM. Before a later upgrade, back up the guest using Corelight's instructions and check supported upgrade paths. A deliberate apt upgrade that keeps the existing configuration can use:

```sh
sudo apt-get update
sudo apt-get -o Dpkg::Options::=--force-confold install --only-upgrade corelight-fleet
sudo systemctl is-active corelight-fleetd
```

Review any new vendor configuration requirements after an upgrade. Keep `/etc/corelight-fleetd.conf`, its community string, `/etc/corelight-fleetd.pem` and the Fleet Manager data/backup files. Updating the GDeploy Docker container is separate from upgrading software already installed on a guest.

## Offline installation

Offline mode performs no repository downloads during the Fleet Manager VM's unattended OS/package installation. It still requires LAN access between GDeploy, ESXi and the new guest. Building or downloading the GDeploy Docker image is a separate host preparation step.

1. On a connected workstation or staging server, obtain the **Ubuntu-compatible `corelight-fleet` amd64 `.deb`** through your Corelight entitlement. The vendor guide supports downloading from the customer portal or running `apt download corelight-fleet` on an Ubuntu host already configured for the Corelight repository. Keep the actual file produced by that command; `apt download` normally writes into the working directory.
2. Obtain any additional **Ubuntu 24.04 amd64 or `all` dependency `.deb` files** needed by that Fleet Manager version. Include recursive dependencies absent from the new VM, not just the main package. Resolve them on a matching Ubuntu 24.04 staging environment and validate the set in an isolated lab; a package already installed on the staging machine may otherwise be missed.
3. In **Setup → OS installation media**, select the regular **Ubuntu Server 24.04 LTS amd64 live-server ISO** with its publisher SHA-256. The ISO must contain the base packages needed for OpenSSH Server, open-vm-tools, Python and CA certificates. Offline mode selects the regular `ubuntu-server` source and disables repository mirror candidates and automatic country-mirror selection, using the installer's offline fallback. This governs package sources, not every unrelated network lookup the installer may attempt. Unsupported/minimal media or a missing base package can stop the OS installation.
4. In **New deployment**, select FleetManager, complete **Configure VMs**, then choose **Offline package** in **FleetManager configuration**. Enter the community string and choose the product identity PEM, or retain the saved values. A repository token is unnecessary.
5. Under **Offline packages**, select one or more `.deb` files and choose **Upload packages**, or place them in the Docker host's `media/` directory and choose **Refresh**. Server-mounted files must be readable by container UID 10001, normally directory mode 0755 and file mode 0644. Uploads accept **4 GiB per file** and persist in `/data/fleetmanager-packages`.
6. Select the main **FleetManager .deb package** and the needed **Additional dependency packages**. Select one version of each dependency. Select **Save & continue** to validate and save the choices, then open **Review & deploy**. Uploading alone registers files; saving stores the selections as defaults and makes them available for deployment.
7. Explicitly run preflight, review its results and start the deployment. Keep all chosen files present and unchanged until the job finishes. You can also prepare and save the same package selections in Setup before starting a deployment; the wizard then reuses those defaults.

GDeploy reads Debian control metadata without installing the package in the app container. The main package must identify itself as `corelight-fleet` for `amd64`; dependencies can be `amd64` or `all`. An optional expected SHA-256 can be supplied for a single uploaded file. If no trusted checksum is available, GDeploy computes one for later integrity checks. That calculated checksum detects changed bytes; it does not establish publisher provenance. Obtain packages from trusted, authenticated sources. The vendor guide does not provide a mandatory publisher checksum workflow.

The worker transfers the saved main/dependency package set and verifies every SHA-256 before any package-manager operation in the guest. Apt then uses only the supplied files and already installed dependencies, with repository sources/indexes disabled and `--no-download`. It does not run an online update or fetch missing dependencies. If packages are missing, the deployment stops with a dependency error in **View logs**; obtain the missing compatible files and update Setup. Do not use **Delete & redeploy** on a VM whose data you need to preserve.

Offline mode applies to the FleetManager VM. Other selected roles, such as Elasticsearch or Kibana, retain their own online installation requirements. Features that later download threat intelligence or other external datasets need the connectivity described in Corelight's guide.

## Installation result and first sign-in

GDeploy writes the shared community string into `/etc/corelight-fleetd.conf` and stores the product identity as `/etc/corelight-fleetd.pem`, owned by `corelight-fleetd` with mode **0400**. It enables and starts the `corelight-fleetd` service. Completion requires an active service, a working HTTPS endpoint presenting the supplied certificate, and the local sensor port 1443 listening. This does not test a real sensor connection or prove upstream firewall access.

Open the deployment's **Credentials** panel. The Fleet Manager URL is **`https://VM_IP`**; sign in as **`admin`** with the temporary password created by Fleet Manager and shown there. The panel also reveals the community string captured for that deployment. **Change the administrator password at first sign-in.** Save the new password yourself: GDeploy's original deployment credential record does not track subsequent changes inside Fleet Manager. The separate Ubuntu SSH account remains `gdeploy` with sudo access.

The supplied product identity certificate may not match the VM's IP or the browser's public trust store. Configure an appropriate web certificate through Corelight's supported procedure for normal browser use. GDeploy's local readiness check verifies the exact supplied identity certificate; it does not install browser trust.

Sensor enrollment is a separate administrator step in Fleet Manager. Configure each sensor to use the chosen Fleet Manager address and the same community string according to Corelight's instructions.

## Storage, updates and recovery

- Back up the complete GDeploy data volume, including its encrypted FleetManager settings, queued snapshots, uploaded `.deb` files and original encryption key. Back up server-mounted `media/` separately. Preserve the vendor PEM and community string in your secure administration records.
- Each queued job retains the configuration, license and local package checksums captured when it was created. Later Setup edits or **Clear FleetManager setup** do not change that job or an existing VM. A new replacement deployment uses current Setup settings.
- Clearing Setup removes the saved defaults and secrets, while retaining uploaded packages. Delete only unused uploads; selected files and those used by queued, running or cleaning jobs are protected. Mounted server files are managed on the host.
- GDeploy's settings are for new deployments. Changing a token, community string or license in Setup does not rotate the values already installed on a guest or upgrade that guest.
- For failures, expand **View logs** and inspect the failed stage. A valid PEM syntax/pair does not guarantee a valid Corelight license. A missing dependency or repository rejection must be fixed at its source. **Delete & redeploy** permanently replaces the job's VMs and disks; hiding a record preserves the VM but does not complete failed work.

Live deployment with a customer Corelight license, actual vendor packages, sensor enrollment and ESXi has not been performed as part of this release. Synthetic package, API, browser and installer tests cover implementation behavior; the lab checklist records the remaining integration validation.
