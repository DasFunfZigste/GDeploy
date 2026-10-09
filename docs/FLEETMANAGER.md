# Deploy Corelight FleetManager

GDeploy creates a dedicated Ubuntu Server 24.04 LTS amd64 VM for **Corelight Fleet Manager**, labeled **FleetManager** in the UI. Select it in **New deployment** to use this flow: **Choose software → Configure VMs → FleetManager configuration → Review & deploy**. The FleetManager step appears before preflight and reuses saved defaults; complete and save it before continuing to review.

**Setup → Software packages → FleetManager** remains available for saving defaults and cleaning up legacy uploaded packages outside a deployment, including at `http://SERVER_LAN_IP:8000/#settings/packages/fleetmanager`. Deployments without FleetManager retain the three-step wizard without the FleetManager configuration step.

This walkthrough follows the **Corelight Fleet Manager User Guide, release 29.2.2, updated October 2, 2026**, especially the requirements and Linux installation instructions on PDF pages 7–15. Use the version of Fleet Manager supported by your Corelight entitlement and Ubuntu release. GDeploy's automated checks do not establish that a particular licensed package works in your environment; complete the [lab acceptance checklist](LAB_VALIDATION.md#fleetmanager-installation) with your actual ESXi host and Corelight license.

## Prepare the VM and network

The deployment wizard defaults to **2 vCPUs, 8 GiB RAM and an 80 GiB disk**. GDeploy requires at least **2 vCPUs, 8 GiB RAM and a 60 GiB disk**; allow more for sensor count, retention, backups and offline sensor updates. Corelight's small-fleet guidance covers 1–10 sensors at 2 vCPUs/8 GB, with larger fleets needing additional resources.

GDeploy uses the full VM disk for the Ubuntu installation. `/var` and `/tmp` normally share the root filesystem; they are not separate reserved partitions. Before installing Fleet Manager, the guest check requires at least **30 GiB available at `/var`** and **20 GiB available at `/tmp`**. An ESXi disk allocation alone does not prove those amounts are free inside Ubuntu.

| Connection | Required access |
| --- | --- |
| GDeploy to ESXi | HTTPS 443 for VM creation and installation media |
| GDeploy to the new VM | SSH 22 for configuration and verification |
| GDeploy container for version choices | DNS and outbound HTTPS to `pkgrepos.corelight.cloud` and the download destination returned by the repository, which can be CloudFront |
| Administrator browser to Fleet Manager | HTTPS 443 |
| Corelight sensor management interfaces to Fleet Manager | TCP 1443, using the shared community string and product identity |
| Fleet Manager guest | DNS, Ubuntu package mirrors, and HTTPS to `pkgrepos.corelight.cloud` plus the signing-key download destination returned by the repository (which can be CloudFront) |

GDeploy installs UFW if needed and adds persistent **443/tcp** and **1443/tcp** allow rules on the FleetManager guest. It preserves UFW enablement, policies, SSH rules and other existing rules. Choose a static address or DHCP reservation. Upstream firewalls must also allow the required traffic. Sensor enrollment, external data feeds, monitoring and backups remain administrator tasks.

## Gather the required credentials

Every FleetManager deployment needs:

- A strong, random **community string**, shared later with the sensors you choose to enroll. GDeploy accepts 1–4096 printable characters and disallows single/double quotes. Keep the value in your password manager; Setup shows whether it is saved without revealing it, and the deployment's authenticated **Credentials** panel can reveal the value used by that job.
- The customer-specific **product identity `.pem` license** supplied by Corelight. This file contains its certificate and unencrypted private key and must be no larger than **64 KiB**. GDeploy verifies that the first certificate matches the key and is currently valid. The key may appear before or after the certificate in the file; any additional certificate chain must follow the matching product identity certificate. A regular web certificate does not substitute for a Corelight entitlement. Only Fleet Manager can validate the actual product license.

You also need the customer repository token. Sign in to [Corelight Cloud](https://my.corelight.cloud/), open **Downloads → Fleet Manager**, and copy the authentication token at the top of that page. Paste it into GDeploy's repository token field. Do not put it into a browser URL, shell command, screenshot or support log.

Setup stores the community string, repository token and PEM encrypted in the existing GDeploy database. Responses show saved-state flags and license name/checksum/expiry, never the raw license, key or token. When editing an existing configuration, blank secret fields and no replacement PEM retain the saved values.

## Online installation

1. Open **New deployment**, select **FleetManager** in **Choose software**, then continue to **Configure VMs**. A fresh deployment starts with no software roles selected.
2. Set its distinct VM name, CPU, RAM, disk, datastore, port group and DHCP/static settings. Other selected applications keep their own VMs and resources. Continue to **FleetManager configuration**.
3. Enter the community string and Corelight repository token, and choose the product identity `.pem` file. Existing saved values are reused; leave a secret field blank and omit a replacement PEM to retain them.
4. Select **Load versions** to fetch the available versions using the token you entered, or the saved token when the field is blank. You can do this before filling in the community string/license or saving the configuration. In the **Version to install** dropdown, choose **Latest available** for the repository candidate at installation time, or select an exact package version from the list. The list includes complete package revisions and epochs, newest first; no version number needs to be typed. Use **Refresh versions** to check again. The same controls appear in Setup defaults.
5. Select **Save & continue** to validate and save the configuration, then open **Review & deploy**. Review shows the version choice. These settings also become the defaults for future FleetManager deployments.
6. In **Review & deploy**, select **Run preflight**, review its results, then select **Deploy**. To combine those actions, first check **Start deployment automatically when preflight passes**, then select **Run preflight & deploy**. This optional setting starts unchecked and queues a job only when all checks pass. Saving configuration alone does not run preflight or queue a job. If a later check reports changed or missing configuration, return to the FleetManager step, correct it and run preflight again.

Loading versions reads repository metadata from the GDeploy container; it does not save the draft token or change Setup. Saved online settings trigger a lookup when the panel opens. If lookup fails, the UI shows the error and retains your current selection, including an exact version saved by an earlier release. A saved version missing from a refreshed list remains visible with a warning. Retry loading, choose another listed version, or explicitly select **Latest available**; the app never silently replaces an exact selection.

The new VM installs Ubuntu, obtains the Corelight stable repository signing key over verified HTTPS, configures the authenticated apt repository and installs `corelight-fleet`. For an exact version, it checks availability, installs `corelight-fleet=VERSION` and verifies the installed version. An unavailable version stops installation with a logged error; GDeploy does not substitute another release. The dropdown reports versions available when metadata was fetched. Preflight checks configuration and version syntax; the guest's signed apt checks remain authoritative for availability, dependencies and package installation. Ubuntu and repository access are required from the guest. Saving online settings clears any legacy offline package selections; it does not delete their uploaded files.

Each queued deployment keeps its saved version choice. **Latest available** is resolved to the repository candidate at installation time, so separate deployments may receive different releases. Choosing an exact version controls the initial install without creating an apt hold or preventing later administrator-managed upgrades. Choosing **Latest available** and saving returns future deployments to the latest candidate.

Repository metadata and signing-key endpoints can redirect to a download service such as CloudFront. GDeploy follows a bounded chain of HTTPS redirects and verifies TLS at every destination. It sends the repository token only while requests remain on the original origin; after the first origin change, the token is never reattached during that download. Signed download query strings are used for the request but are not logged. HTTP destinations, embedded URL credentials, invalid destinations and excessive redirects are rejected. Downloaded signing-key bytes must pass `gpg --dearmor` before repository trust files are written. The version list does not replace the guest's signed apt verification.

If the original signing-key request returns **HTTP 401**, confirm repository entitlement and save the current token from **Corelight Customer Portal → Downloads → Fleet Manager** before creating another deployment. Blank token fields preserve the saved token. A failed or queued job retains its original token even after Setup changes. A **302** response only establishes a redirect, not successful key retrieval or valid credentials. If a redirected download is rejected, check the vendor download service and guest access to that destination. Do not share the token, PEM, authorization headers or a full signed redirect URL in support logs.

Updating GDeploy fixes the redirect handling for future installation attempts; it does not resume an already failed job or repair an existing VM. **Delete & redeploy** replaces the VM and its disks; use it only when replacement is intended.

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

## Updating from an offline configuration

FleetManager installation now uses the online repository only. Offline mode, installer uploads and dependency selection are no longer available. Previously saved license and community-string values are retained. Open FleetManager configuration, enter a repository token, choose a version and save; leave the other secret fields blank to retain them. This clears obsolete package selections without deleting their files.

Previously queued offline jobs fail validation with instructions to configure online installation; GDeploy never silently changes their installation source or starts a new VM for them. Existing VMs and historical credentials/logs remain intact. Create a new online deployment when ready. **Delete & redeploy** is only for deliberately replacing an existing VM and its disks.

Setup provides cleanup for retained uploads. Only unused app-managed files can be deleted; selected files and active job references remain protected. Save online defaults or clear the old defaults before removing selected files. Resolve any old queued/running job before deleting its referenced packages. Server-mounted files are managed on the Docker host.

## Installation result and first sign-in

GDeploy writes the shared community string into `/etc/corelight-fleetd.conf` and stores the product identity as `/etc/corelight-fleetd.pem`, owned by `corelight-fleetd` with mode **0400**. It configures the UFW allow rules for TCP 443 and 1443, then enables and starts the `corelight-fleetd` service. Completion requires an active service, a working HTTPS endpoint presenting the supplied certificate, and the local sensor port 1443 listening. This does not test a real sensor connection or prove upstream firewall access.

Open the deployment's **Credentials** panel. The Fleet Manager URL is **`https://VM_IP`**; sign in as **`admin`** with the temporary password created by Fleet Manager and shown there. The panel also reveals the community string captured for that deployment. **Change the administrator password at first sign-in.** Save the new password yourself: GDeploy's original deployment credential record does not track subsequent changes inside Fleet Manager. The separate Ubuntu SSH account remains `gdeploy` with sudo access.

The supplied product identity certificate may not match the VM's IP or the browser's public trust store. Configure an appropriate web certificate through Corelight's supported procedure for normal browser use. GDeploy's local readiness check verifies the exact supplied identity certificate; it does not install browser trust.

Sensor enrollment is a separate administrator step in Fleet Manager. Configure each sensor to use the chosen Fleet Manager address and the same community string according to Corelight's instructions.

## Existing VM firewall rules

Updating the GDeploy container applies the firewall change to future FleetManager installations. To apply it to an already deployed FleetManager VM, run these commands inside that VM:

```sh
sudo ufw allow 443/tcp
sudo ufw allow 1443/tcp
sudo ufw status verbose
```

These commands preserve existing rules and do not enable a currently inactive firewall. Confirm GUI access from an administrator workstation and sensor connectivity from the sensor network; local service health cannot prove the upstream network path.

## Storage, updates and recovery

- Back up the complete GDeploy data volume, including its encrypted FleetManager settings, queued snapshots, retained legacy `.deb` files and original encryption key. Back up server-mounted `media/` separately. Preserve the vendor PEM and community string in your secure administration records.
- Each queued online job retains the configuration, license and repository version choice captured when it was created. Later Setup edits or **Clear FleetManager setup** do not change that job or an existing VM. A new replacement deployment uses current Setup settings.
- Clearing Setup removes the saved defaults and secrets, while retaining uploaded packages. Delete only unused uploads; selected files and those used by queued, running, stopping or cleaning jobs are protected. Mounted server files are managed on the host.
- GDeploy's settings are for new deployments. Changing a token, community string or license in Setup does not rotate the values already installed on a guest or upgrade that guest.
- For failures, expand **View logs** and inspect the failed stage. **Copy logs** supports HTTP LAN access through a browser copy fallback. If copying is still blocked, copy the selected **Full deployment log** with Ctrl+C (⌘C on Mac), then select **Done copying**. A valid PEM syntax/pair does not guarantee a valid Corelight license. Repository access or package dependency failures must be fixed at their source. **Delete & redeploy** permanently replaces the job's VMs and disks; hiding a record preserves the VM but does not complete failed work.

Live deployment with a customer Corelight license, actual vendor packages, sensor enrollment and ESXi has not been performed as part of this release. Automated API, browser and installer tests cover implementation behavior; the lab checklist records the remaining integration validation.
