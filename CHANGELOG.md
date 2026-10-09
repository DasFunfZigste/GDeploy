# Changelog

Each numbered release has a matching Git tag, Docker image, installation bundle and GitHub Release page. Versions follow semantic versioning; the 0.x series is for lab validation before production use.

## [Unreleased]

## [0.15.4] - 2026-10-09

### Fixed

- Remove the Software Sensor's dependency on ESXi generating MAC addresses before its first boot. Create its two VMXNET3 adapters with distinct manual MACs from VMware's supported range, excluding addresses already registered on the target ESXi host. Ordinary VM roles keep automatic MAC assignment.
- Read back and verify the powered-off sensor's actual MAC addresses before preparing its Ubuntu ISO. Preserve the management/monitoring mapping and the order of ISO attachment followed by first power-on; the monitoring adapter still receives no IP configuration. This check identifies network adapters and does not test whether an IP address is unused.
- Replace the generic `ESXi did not return valid generated sensor network addresses` error with specific MAC diagnostics identifying the adapter and reason. Log verified management and monitoring MACs in deployment details, and detect conflicting MACs in other VMs on the target host before proceeding.
- Add regressions modeling standalone ESXi that leaves generated MACs unset until first power-on, supported manual allocation and collision handling, unchanged ordinary VM creation, and the reported failure followed by successful recovery with a blank replacement pairing token.

### Installation and compatibility

- Update the existing clone with `git pull --ff-only` and `sudo docker compose up -d --build --wait`, then refresh the browser. Preserve the complete data volume and original encryption key. No database migration is required.
- Updating does not modify existing VMs or resume failed jobs. Use **Delete & redeploy** for the failed sensor job; a MAC-read failure occurs before sensor installation, so its replacement pairing token can remain blank when the saved state confirms this. Each replacement is a new VM; update any external DHCP reservation to its logged management MAC.
- MAC collision checks cover registered VMs on the target host, not the entire LAN. Automated request/lifecycle and Docker checks do not replace a full installation against ESXi 8.0 Update 3 with licensed sensor software.

## [0.15.3] - 2026-10-09

### Fixed

- Make the replacement Software Sensor pairing token optional in **Delete & redeploy** when sensor installation never started. Leave the field blank to reuse that job's encrypted sensor configuration, including its original token. This includes existing jobs that failed at VM creation with `configSpec.bootOptions.bootOrder`, during media preparation or during OS installation.
- Continue accepting a fresh replacement token with current Setup settings. If sensor installation may have started or the saved state is unknown, require a fresh token before deleting any resources. The dialog explains which path applies without revealing the saved token.
- Record the sensor installation boundary durably and preserve token reservations through cleanup failures, restarts and repeated early-failure retries. Transfer an unused token only along the direct replacement chain; unrelated jobs and concurrent duplicate recovery cannot reuse it. Other VMs' software stages do not falsely mark the sensor token as used.

### Installation and compatibility

- Update the existing clone with `git pull --ff-only` and `sudo docker compose up -d --build --wait`, then refresh the browser. Preserve the complete data volume and original encryption key. Two additive sensor state/recovery tables are created automatically; existing jobs, saved settings and credentials remain intact.
- Updating does not resume failed jobs or change existing VMs. **Delete & redeploy** still requires typed confirmation and permanently replaces any VMs/disks owned by that job. Finish or resolve active work before rollback; older workers do not maintain the new recovery state. Fleet Manager sensor records remain administrator-managed.
- Automated checks cover recovery, token protection, browser behavior and container startup. Live ESXi provisioning and licensed Fleet pairing remain lab acceptance steps.

## [0.15.2] - 2026-10-09

### Fixed

- Fix Software Sensor VM creation failing on ESXi with `vmodl.fault.InvalidArgument: configSpec.bootOptions.bootOrder`. The sensor VM is created before its customized ISO exists; its initial boot order now includes only the disk, instead of referencing a CD/DVD drive that has not been added yet.
- Add the ISO-backed CD/DVD drive and its boot entry together, before powering on the sensor VM. Use the actual ESXi disk device key and retain disk-first boot so the blank disk falls through to the installer and the installed OS boots after reboot. Reject missing/ambiguous boot disks and occupied installation-media slots before reconfiguration.
- Add VMware request regressions that reject boot entries without corresponding hardware and cover sensor creation, ISO attachment and ordinary deployments with their ISO already attached. Existing EFI settings, NIC MAC matching and guest installation behavior are preserved.

### Installation and compatibility

- Update the existing clone with `git pull --ff-only` and `sudo docker compose up -d --build --wait`, then refresh the browser. Preserve the data volume and encryption key. No database migration is required.
- Updating does not resume a failed job or modify an existing VM. Use the failed job's normal recovery flow for a replacement; sensor replacements require a fresh Fleet pairing token, while saved common repository/license settings remain reusable. Review the delete-and-redeploy confirmation for any resources retained by that job.
- Automated checks validate the ESXi request structure and application/container behavior. A full deployment against ESXi 8.0 Update 3 remains a lab acceptance step.

## [0.15.1] - 2026-10-09

### Fixed

- Keep FleetManager and Software Sensor configuration in the same deployment draft when going back to correct VM resources or a network conflict, retrying failed preflight, or visiting Setup and returning. Entered tokens, community strings, license keys, the selected FleetManager PEM file, version selection and sensor pairing token remain available without re-entry.
- Keep entered credentials masked after **Save & continue**, and clearly identify reusable saved credentials. Returning to a configuration step no longer makes saved values look missing or discards unsaved replacements. Changes still require fresh preflight before deployment.
- Retain draft configuration when temporarily deselecting and reselecting a software role. Draft secrets stay only in the open page's memory and are cleared when the wizard is discarded, the session ends or the deployment is queued. Explicitly clearing software setup still removes its credentials.

### Installation and compatibility

- From your existing clone, run `git pull --ff-only` and `sudo docker compose up -d --build --wait`, then refresh the browser. Preserve the data volume and encryption key. No database migration is required; existing jobs and VMs are unchanged.
- Browser refresh or closing the page still discards unsaved draft entries. Previously saved encrypted Setup credentials remain reusable; this update cannot recover unsaved input already lost by the old wizard.

## [0.15.0] - 2026-10-09

### Added

- **Corelight Software Sensor** as a deployment option on a dedicated minimal **Ubuntu 24.04 amd64** VM. Installs `corelightctl` and `corelight-sensor` from the signed online Corelight repository, prepares the guest and configures Fleet Manager pairing.
- A **Sensor configuration** step before preflight, plus encrypted common defaults in Setup. Requires a Software Sensor repository token, community string, sensor license key, Fleet pairing URL/server SSL name and a fresh per-deployment pairing token. FleetManager PEM files are not sensor licenses. Pairing tokens cannot be reused across jobs; delete-and-redeploy validates a new token before deleting resources.
- Separate management and monitoring VMXNET3 adapters. Installation media matches the actual ESXi-generated MAC addresses; management uses static or reserved DHCP addressing, and monitoring has no assigned IP, DHCP, router advertisements or default route. Administrators select both port groups and configure their own traffic mirror/tap.
- Sensor defaults of **4 vCPUs, 16 GiB RAM and a 600 GiB disk**, dedicated CPU/memory reservations, capacity preflight and guest checks for x86-64-v3 CPU support and 500 GB free under `/var`. SSD-backed storage is required by the vendor.
- Sensor service verification that checks the licensed sensor core, API and Fleet connection. The documented Suricata no-rules warning is permitted; license, pairing and other service failures stop deployment with sanitized diagnostics. The sensor manages its guest firewall through Kubernetes; API TCP 443 access has a configurable IPv4 source network.
- A [Software Sensor walkthrough](docs/CORELIGHT_SENSOR.md), lab acceptance checklist and automated coverage for configuration, token reuse, NIC creation, installation, secret handling and browser flows. The walkthrough is included in the release installation bundle.

### Installation and compatibility

- From the existing clone, run `git pull --ff-only` and `sudo docker compose up -d --build --wait`, then refresh the browser. Finish active deployments before updating and preserve the data volume and encryption key. New settings/token-tracking tables are created automatically; existing deployments are retained.
- This release supports Ubuntu 24.04 and online sensor installation. Debian and offline sensor installation are not enabled. Fleet Manager must already provide a sensor record and pairing details before preflight. Existing sensor VMs are not upgraded or reconfigured by updating GDeploy.
- Automated checks do not replace a licensed deployment on ESXi, verification in Fleet Manager or validation of real mirrored traffic. GDeploy does not alter existing port-group security or switch mirroring.

## [0.14.0] - 2026-10-09

### Fixed

- FleetManager installation adds and verifies persistent guest UFW allow rules for **443/tcp** (GUI) and **1443/tcp** (sensor connectivity). UFW is installed if needed; existing enablement, policies, SSH rules and unrelated rules are preserved. Firewall failures produce a deployment error instead of reporting success from local service checks alone.

### Changed

- FleetManager now installs exclusively from the online Corelight repository. Removed the offline selector, installer/dependency selection and new `.deb` uploads from Setup, the deployment wizard and installation flow. Repository token, version dropdown, community string and product identity PEM are configured before preflight.
- Existing offline defaults retain their license and community string for explicit online reconfiguration. Legacy offline job snapshots fail validation before provisioning with actionable guidance; their installation source is never silently changed. Existing VMs, logs, credentials and retained files are preserved, with cleanup available for unused legacy uploads.
- Updated FleetManager instructions, network requirements and container smoke checks for repository installation.

### Installation and compatibility

- From the existing clone, run `git pull --ff-only` and `sudo docker compose up -d --build --wait`, then refresh the browser. Finish active deployments before updating and preserve the data volume and encryption key. No schema migration is required.
- Existing FleetManager VMs are not changed by rebuilding GDeploy. On each VM that needs these firewall rules, run `sudo ufw allow 443/tcp`, `sudo ufw allow 1443/tcp` and `sudo ufw status verbose`. Upstream firewalls still need to permit GUI and sensor traffic. The [FleetManager guide](https://github.com/DasFunfZigste/GDeploy/blob/v0.14.0/docs/FLEETMANAGER.md#existing-vm-firewall-rules) includes the walkthrough.

## [0.13.0] - 2026-10-08

### Added

- A separate **Previous deployments** view for hidden records, shown as a child beneath **Deployments** in the sidebar when browsing deployments. Search, filter, inspect logs/credentials and restore records from this view. The main Deployments view contains visible records only; the former **Show hidden** checkbox is replaced by this navigation.
- Filter hidden records on the server before applying the history limit, so recent visible jobs do not crowd older hidden jobs out of Previous deployments.

### Fixed

- Keep Hide/Restore controls synchronized with the latest deployment status, including when a running job becomes **Stopped** while logs are selected or open for manual copying. Hiding/restoring preserves the log-copy buffer and updates the surrounding navigation and controls.
- Let finished and stopped jobs be hidden/restored without waiting for unrelated ESXi, queueing or redeploy operations. Visibility changes retain their atomic status checks, authentication and CSRF protection. **Stopping** jobs become hideable only after the worker acknowledges **Stopped**.

### Installation and compatibility

- From the existing clone, run `git pull --ff-only` and `sudo docker compose up -d --build --wait`, then refresh the browser. Preserve the data volume, encryption key, settings and media. No data migration is required; existing hidden records appear in Previous deployments.
- Hiding and restoring change only list visibility. VMs, disks, credentials, logs, job status and resource reservations remain intact. This release does not add permanent job deletion or resume stopped jobs.

## [0.12.1] - 2026-10-08

### Changed

- Generate new guest OS, Elasticsearch/Kibana and Splunk login passwords from a readable alphabet without common lookalikes, including I/i/L/l/1, O/o/0, B/b/8, G/g/6, Q/q/9, S/s/5 and Z/z/2. Passwords use 40 cryptographically random characters, include uppercase/lowercase letters and digits, and retain more than 192 bits of randomness. Machine tokens and encryption keys keep their existing formats.
- Add a collapsed **Preview** section below **Deployments** in the navigation, containing **Alma Linux** and **Proxmox** as planned items. These are reminders only; no deployment or platform support is enabled.

### Installation and compatibility

- Update from the existing clone with `git pull --ff-only` and `sudo docker compose up -d --build --wait`, then refresh the browser. Preserve the data volume and encryption key. No data migration is required.
- Readable passwords apply to newly queued deployments and replacements. Existing VM passwords, already queued credentials and user-entered credentials remain unchanged. FleetManager generates its own temporary administrator password and still requires changing it at first sign-in.

## [0.12.0] - 2026-10-08

### Added

- **Stop deployment** for queued and running jobs. Queued jobs stop before provisioning; running jobs show **Stopping** until GDeploy reaches a safe boundary, then **Stopped**. Stop requests are persisted and audited. Already-created VMs, disks, installation media, credentials and logs are retained, with separate history hiding and explicit delete/redeploy controls.
- A **Default port group** in Setup's ESXi connection section, selected from live host inventory. New VM forms use that default while retaining per-VM overrides. Without a valid default, choose a port group explicitly instead of silently using the first network. Defaults belong to their ESXi host; changes do not alter existing jobs or VM choices already entered in a draft.

### Deployment behavior

- Stopping ends GDeploy's orchestration without powering off VMs or undoing completed work. OS installation already running inside a VM can continue. In-flight ESXi tasks and guest installation commands may finish before the stop is acknowledged; additional stages and VMs are skipped. Stopped jobs do not automatically resume.
- Protect media and software packages while a stop is pending, retain VM ownership and address reservations after stopping, and prevent stale or repeated requests from turning completed jobs into stopped jobs.
- Show periodic, actionable deployment-log messages while VMware Tools has no guest IP or reports an address different from the requested static IP, including port-group and guest-network checks.

### Installation and compatibility

- From the existing clone, run `git pull --ff-only` and `sudo docker compose up -d --build --wait`, then refresh the browser. Preserve the full data volume, encryption key, settings, licenses and media. The default-port-group table is added alongside existing data.
- Finish or resolve active jobs before updating; restarting interrupts active provisioning. Resolve pending stop requests before rolling back to a version without stop support. Older versions do not apply the saved default port group or provide the new stop controls. Updating does not resume earlier failed or interrupted deployments.
- Automated checks use synthetic VM operations. Live ESXi and licensed guest installation acceptance remain lab checks.

## [0.11.1] - 2026-10-08

### Added

- Choose FleetManager online versions from a repository-backed dropdown in Setup and the deployment wizard. Use the entered token or saved token to load available `corelight-fleet` versions before preflight; the lookup does not save draft credentials. Keep **Latest available** as an option and preserve existing saved version choices when a lookup fails. Offline mode uses the selected `.deb` package version.
- Add an optional **Start deployment automatically when preflight passes** checkbox to review. With this option selected, explicitly running preflight queues the deployment when all checks pass, without another confirmation. It starts unchecked for each new job. Failed checks stop the flow, and server-side checks still run before queueing.

### Fixed

- Make the SSH cleanup regression test independent of shared-runner scheduling delays while retaining its watchdog and completed-result assertions. Guest command timeout behavior is unchanged.
- Start each new deployment with no software roles selected. Select the desired roles before continuing; explicitly choosing Kibana still includes its required Elasticsearch VM. Back navigation and returning from Setup retain intentional choices.
- Make **Copy logs** work on LAN HTTP through a browser-compatible copy fallback. Report success only when a copy operation succeeds; if the browser blocks copying, provide the full formatted logs for manual copying.

### Installation and compatibility

- From the existing clone, run `git pull --ff-only` and `sudo docker compose up -d --build --wait`, then refresh the browser. Preserve the data volume, encryption key, environment files, credentials, licenses and media. No database migration is required, and existing queued jobs keep their captured settings.
- Version discovery requires outbound HTTPS from the GDeploy container to Corelight's repository and its metadata download destinations, using the customer's repository entitlement. A displayed version is not proof of guest compatibility or installability; the guest still verifies signed repository data and installs the exact chosen version. The guest's existing repository/network requirements continue to apply.
- Automated tests use synthetic repository data and local TLS servers. Live Corelight repository listings and licensed installations remain lab acceptance checks.

## [0.11.0] - 2026-10-08

- Source tag only; no Docker image or GitHub Release was published because an existing timing-sensitive SSH regression test blocked validation. The deployment refinements are included in **0.11.1** above.

## [0.10.1] - 2026-10-07

### Fixed

- Follow HTTPS redirects when retrieving the FleetManager repository signing key, including Corelight's reported CloudFront download redirect. Previous releases rejected every redirect even though the vendor's documented download command follows them.
- Keep the repository token on the original origin only. After a redirect changes origin, remove authentication permanently for that download, including any later redirect back. Verify TLS on each request, reject insecure or malformed destinations and embedded URL credentials, bound the redirect chain and key size, and keep signed URLs out of diagnostic output.
- Distinguish rejection of the original repository token from a failure at a redirected download. A redirect alone does not establish successful authentication or a valid signing key; downloaded bytes must still pass the key parser.

### Installation and compatibility

- From the existing clone, run `git pull --ff-only` and `sudo docker compose up -d --build --wait`, then refresh the browser. Preserve the existing data volume, encryption key, environment files, licenses, credentials and media. No data migration is required; online version selection and offline installation are unchanged.
- If the failed job reported HTTP 401, save the current token from Corelight Customer Portal → Downloads → Fleet Manager before creating another deployment. Blank token fields retain the saved token; existing jobs keep the token captured when queued. This redirect fix does not correct invalid credentials, resume failed jobs or alter an existing VM.
- Automated coverage includes real local TLS redirects and credential isolation. Customer-token, vendor-package and ESXi installation acceptance still requires the lab environment. Earlier releases will again reject signing-key redirects if rolled back.

## [0.10.0] - 2026-10-07

### Added

- Choose an exact FleetManager Debian package version in **FleetManager configuration → Online repository → Version to install**, before running preflight. The same field is available in Setup defaults. Leave it blank to install the repository's latest candidate; offline mode continues to use the selected `.deb` package version.
- Show the saved version choice during review and preflight, and retain it in each queued deployment. Changing defaults later does not change existing jobs or VMs.
- Install the requested version without falling back to a newer release. The guest checks repository availability and verifies the installed version, with actionable errors when the requested package is unavailable or does not match. Preflight validates configuration; repository availability and dependencies are checked inside the VM during installation.

### Installation and compatibility

- From the existing clone, run `git pull --ff-only` and `sudo docker compose up -d --build --wait`, then refresh the browser. Preserve the existing volume, encryption key, environment files, credentials, licenses and media. No database migration or license re-upload is required.
- Existing online settings and queued jobs without a version keep their previous latest-candidate behavior. Clearing the version field explicitly returns to that behavior. Offline installs ignore the online version field.
- This choice controls the initial installation. It does not put the package on hold or upgrade existing FleetManager VMs. Finish jobs requesting a specific version before rolling back to an older GDeploy release, which ignores this choice. Live Corelight repository, licensed-package and ESXi acceptance remain lab validation tasks.

## [0.9.2] - 2026-10-07

### Changed

- Make the public-repository source build the default installation path: install Git and Docker from Ubuntu 24.04 packages, clone GDeploy, then run `sudo docker compose up -d --build --wait`. Existing Docker/Git users can skip package installation. This path requires no GitHub credential linking or Docker registry login.
- Replace the primary installation guide with a short setup/update walkthrough and put that guide first on new release pages. Preserve detailed media, software, networking, storage, account recovery and backup instructions in `docs/OPERATIONS.md`.
- Keep optional release image archives available for local loading without a registry pull. Public repository/release access does not imply that the GHCR package is public. Published historical releases and tags are unchanged.

### Installation and compatibility

- From the same existing clone, run `git pull --ff-only` and `sudo docker compose up -d --build --wait`, then refresh the browser. These commands track the current default branch and rebuild the app locally.
- Preserve the data volume, `.env`, media directory, encryption key, credentials and deployment history. This release changes documentation and release presentation; application behavior, saved settings, queued jobs and existing VMs are unchanged.

## [0.9.1] - 2026-10-06

### Fixed

- Add **FleetManager configuration** between **Configure VMs** and **Review & deploy** whenever FleetManager is selected. Choose online/offline installation and supply its community string, product identity PEM, repository token or packages before running preflight, without first triggering a missing-configuration error.
- Reuse saved FleetManager defaults and save changes before continuing to review. **Setup → Software packages → FleetManager** remains available for default settings and package management. Deployments without FleetManager retain the three-step wizard.
- Keep preflight an explicit action on **Review & deploy** after configuration is saved; saving configuration alone does not queue a deployment.
- Preserve FleetManager drafts during Back navigation, block navigation during saves/uploads, and discard unsaved credential fields when the wizard closes or the session ends.

### Installation and compatibility

- Update with `git pull --ff-only` and `docker compose up -d --build --wait`, refresh the browser, and check **v0.9.1**. Preserve the existing data volume, encryption key, `.env`, credentials, certificates, SSH keys, media and uploaded packages.
- This patch changes the wizard flow and instructions. FleetManager API validation, encrypted settings, queued snapshots, package installation and existing VMs are unchanged. Saved settings from v0.9.0 remain usable; no license or package re-upload is required. The v0.9.0 backup and compatibility requirements still apply.

## [0.9.0] - 2026-10-05

### Added

- Deploy **Corelight FleetManager** on its own Ubuntu VM, with separate name, network and resource settings. Defaults are 2 vCPUs, 8 GiB RAM and an 80 GiB disk; minimum inputs are 2 vCPUs, 8 GiB RAM and 60 GiB disk. Guest checks verify free space at `/var` and `/tmp` before installation.
- Add **Setup → Software packages → FleetManager** with **Online repository** and **Offline package** modes, a shared community string and customer product identity PEM license. Online mode uses the customer repository token and retains the signed apt repository for later administration.
- Upload or select the offline `corelight-fleet` amd64 `.deb` and additional amd64/`all` dependency packages. Uploads are limited to 4 GiB each. Read actual Debian control metadata, compute SHA-256 and optionally verify an expected checksum. Transfer integrity checks precede guest package installation; calculated hashes do not establish publisher provenance.
- Offline FleetManager uses the regular Ubuntu live-server source without installer mirror downloads and installs only supplied or already installed dependency packages. Missing packages stop the deployment with an actionable error; no online dependency fallback is used.
- Encrypt FleetManager defaults, license PEM and per-job snapshots. Verify the PEM certificate/private-key pair and dates locally, then let Fleet Manager validate its product entitlement. Selected and active-job package references are protected from deletion; clearing Setup preserves uploaded files and existing jobs/VMs.
- Configure and enable `corelight-fleetd`, verify its service and ports 443/1443, and show its generated temporary `admin` password in **Credentials**. Fleet Manager requires changing that password on first sign-in. Sensor enrollment remains an administrator task.
- Include the online/offline FleetManager walkthrough in the release bundle and link it from the installation guide.

### Installation and compatibility

- Update with `git pull --ff-only` and `docker compose up -d --build --wait`, refresh the browser, and check **v0.9.0**. Keep the existing volume, encryption key, `.env`, credentials, ESXi trust, SSH keys and installation media. Include encrypted FleetManager settings and `/data/fleetmanager-packages` in complete backups; back up mounted packages separately.
- FleetManager is optional. Existing Splunk, Elastic and OS-only deployments retain their previous behavior. A FleetManager job captures its mode, credentials, license and offline package set; changing Setup does not update existing guests. Updating GDeploy does not upgrade guest Fleet Manager installations.
- Finish or resolve FleetManager jobs before rolling back to an older GDeploy release, which does not support that VM role or its settings. Retain a complete backup and existing encryption configuration; earlier schema rollback caveats still apply. Deleting packages or VMs cannot be undone by changing the app image.
- Requires a valid Corelight entitlement, compatible vendor package and appropriate network access. Live vendor-package/license, sensor and ESXi acceptance has not been performed for this release; use the lab checklist before relying on it for workloads.

## [0.8.1] - 2026-10-05

### Fixed

- Accept Splunk's publisher **SHA-512** checksum in **Setup → Software packages**, for both uploaded and server-selected packages. The field accepts the 128-character SHA-512 supplied by Splunk or an existing verified 64-character SHA-256.
- Explain how to retrieve Splunk's publisher checksum by appending `.sha512` to the official installer's download URL. GDeploy calculates and compares the package hash; users do not need to generate their own checksum.
- Retain an internally computed SHA-256 for queued package snapshots and guest transfer verification after validating the publisher checksum.

### Installation and compatibility

- Update with `git pull --ff-only` and `docker compose up -d --build --wait`, refresh the browser, and check **v0.8.1**. Preserve the existing data volume, configuration, encryption key, credentials, SSH keys, ESXi trust and media.
- Existing saved SHA-256 package selections and queued snapshots continue to work without re-uploading a verified package. OS ISO checksums and the legacy `GDEPLOY_SPLUNK_SHA256` environment variable remain SHA-256; use Setup for a publisher SHA-512. The v0.8.0 package-storage and backup requirements still apply.

## [0.8.0] - 2026-10-05

### Added

- **Setup → Software packages** configures the licensed Splunk Enterprise Linux x86_64 `.tgz` used for future deployments. Upload a package from your computer or select one from the GDeploy server, retaining its vendor filename. Enter the publisher's verified SHA-256; GDeploy checks the checksum, archive safety and expected Splunk binaries before allowing deployment.
- Persist uploaded packages under `/data/packages`, with a 4 GiB upload limit. Clear the saved default or delete unused uploads in Setup. Selected packages and uploads referenced by queued, running or cleaning jobs are protected; server-mounted files cannot be deleted through the app.
- Snapshot the selected package path/checksum for each new Splunk deployment. Changing the default later does not switch the package used by an already queued job.
- Verify the transferred package's SHA-256 inside the guest against the queued checksum before package-manager or extraction work. Archive validation rejects conflicting duplicate paths and nonregular Splunk launcher/daemon entries while allowing safe internal links.

### Fixed

- A missing Splunk package in preflight now links to **Configure Splunk package**. The deployment draft is retained while completing Setup; use **Return to deployment** and rerun preflight before starting.
- Splunk configuration can be completed in the browser without renaming the package to `splunk.tgz`, editing `.env` or restarting the container.

### Installation and compatibility

- Update with `git pull --ff-only` and `docker compose up -d --build --wait`, refresh the browser, and check **v0.8.0**. Preserve the existing data volume, configuration, encryption key, credentials, SSH keys, ESXi trust and media. Back up uploaded packages with the complete data volume.
- Existing `GDEPLOY_SPLUNK_PACKAGE` / `GDEPLOY_SPLUNK_SHA256` settings, including the default `/media/splunk.tgz` path, remain the fallback when no package is selected in Setup. Jobs created before this feature keep their environment-configured package behavior. The application does not download Splunk, bundle its installer or provide a license; license acceptance remains required per deployment. Elasticsearch and Kibana installation is unchanged.
- Earlier releases do not understand saved package selections or queued package snapshots. Finish queued work before rollback, retain a complete backup and supply the older release's verified environment package settings if needed. Deleting an uploaded package removes that copy; changing app versions cannot restore it.

## [0.7.1] - 2026-10-02

### Fixed

- Parse cloud-init's JSON stdout separately from stderr. Permission warnings on stderr could previously make a valid status response fail with **Could not verify Ubuntu cloud-init completion**.
- Recognize Ubuntu's clean **disabled-by-marker-file** state after installation. Acceptance also requires completed first-boot evidence, installer artifacts, an installed root filesystem, working sudo, and active SSH and VMware Tools services. A disabled status alone is insufficient, and reported cloud-init errors still stop the deployment.
- Retry transient first-boot readiness states within the existing OS-installation timeout. Record sanitized status and readiness diagnostics in **Deployment logs** so a failure identifies the check that needs attention.

### Installation and compatibility

- Update with `git pull --ff-only` and `docker compose up -d --build --wait`, refresh the browser, and check **v0.7.1**. Preserve the existing data volume, `.env`, credentials, certificate approvals, SSH public keys and saved media.
- Keep your verified Ubuntu Server 24.04 LTS amd64 live-server ISO, including `ubuntu-24.04.5-live-server-amd64.iso`; this repair does not require replacing or re-uploading the ISO. The patch changes readiness verification, with no database or installation-media format change.
- Existing failed records are not resumed or marked completed automatically. Keep a healthy VM intact and use **Hide from history** if desired. Hiding does not finish pending software installation or detach/remove deployment media; **Delete & redeploy** remains a deliberate, destructive replacement action.

## [0.7.0] - 2026-10-01

### Added

- **Setup → SSH access** saves one or more SSH public keys for the `gdeploy` Linux user on every VM in future deployments. Paste one OpenSSH public key per line, review its fingerprint, and save the list. Edit the list to remove keys or clear it for future deployments.
- Validate public-key encoding and supported key types before saving; reject private keys and malformed entries without exposing their contents in errors. Support Ed25519, RSA of at least 2048 bits, and ECDSA keys. Duplicate key material is installed once, regardless of its comment.
- Capture the saved key list when each deployment is queued and include it in the unattended OS installation alongside that VM's generated automation key. Later Setup edits do not change already queued deployments or existing VMs. New delete-and-redeploy jobs use the current saved key list.

### Installation and compatibility

- Update with `git pull --ff-only` and `docker compose up -d --build --wait`, refresh the browser, and check **v0.7.0**. SSH access configuration is optional; leaving it empty keeps the existing generated automation key and password login behavior.
- Public-key settings persist in the existing encrypted data volume. Keep private keys on your workstation. Saving or removing public keys in Setup does not grant or revoke access on VMs that already exist.
- Existing jobs without an SSH-key snapshot retain their original behavior. This release does not modify cloud-init readiness checks or automatically repair older failed deployments. Earlier GDeploy versions do not install these saved administrator keys; finish queued work before rolling back.

## [0.6.0] - 2026-10-01

### Added

- **Hide** finished deployment records from the default history without deleting their VMs. Use **Show hidden** to find them and **Restore** to bring them back. Hidden records keep their original status, logs, credentials, VM ownership and resource reservations. Queued, running and cleaning work cannot be hidden.

### Changed

- Place **Setup** above **Deployments** in the main navigation so initial ESXi and OS ISO configuration appears first.
- Show a separate, visible VM configuration section for every selected role. Elasticsearch and Kibana each have their own VM name, CPU, RAM, disk, datastore, port group and DHCP/static settings. All selected VM forms remain visible together, with navigation links for longer deployments.

### Installation and compatibility

- Update a source installation with `git pull --ff-only` and `docker compose up -d --build --wait`, then refresh the browser and check **v0.6.0** in the footer. Preserve the existing data volume, `.env`, account, encryption key, certificate approvals and media.
- History visibility is persistent and reversible. Hiding a failed record does not mark it completed, retry provisioning, detach installation media or delete any VM. A working VM can remain intact while its failed record is hidden; use **Delete & redeploy** only when intentionally replacing its VMs and disks.
- The upgrade adds a history-visibility column to the database. Back up the complete data volume before upgrading; rollback requires the matching pre-upgrade backup because older releases cannot create deployments against the migrated schema. The per-VM deployment format, Ubuntu installer support and default `0.0.0.0:8000` binding are unchanged. This release does not change the cloud-init readiness check.

## [0.5.0] - 2026-09-30

### Added

- An expandable **Deployment logs** view with sanitized event/error details and **Copy logs**. Failed deployments link directly to it. Previously recorded failures retain their original detail; updating cannot recover diagnostic output that was not saved.
- **Storage & saved ISOs** in **Setup → OS installation media**, showing total, used and available space on the container-visible GDeploy data filesystem, plus saved ISO and deployment-workspace usage.
- Delete unused uploaded or ESXi-imported ISO copies after confirmation. The selected ISO and copies referenced by queued, running or cleaning deployments are protected. Server-mounted media and original ESXi datastore files cannot be deleted through this control.
- **Clear saved selection** removes the default media choice without deleting its file, so the only unused saved copy can then be deleted without uploading a replacement first. Existing environment media settings become the fallback; active deployments keep their original ISO snapshots and deletion protection.

### Fixed

- Make privately extracted GRUB configuration and manifest files writable before patching installation media. ISO metadata can preserve read-only permissions during extraction, causing media preparation to fail for the non-root container account. The source ISO remains unchanged.
- Keep installation-media subprocess temporary files in the private deployment workspace under `/data/artifacts`, avoiding the separate 256 MiB `/tmp` memory filesystem for that work.
- Retain bounded, sanitized media-preparation diagnostics so new failures show their underlying tool error instead of only a generic preparation message. This addresses a reproduced permissions failure; an earlier generic error alone does not establish its cause on another installation.

### Installation and compatibility

- Update a source installation with `git pull --ff-only` and `docker compose up -d --build --wait`, then refresh the browser and check **v0.5.0** in the footer. Open a deployment's **View logs**, or **Setup → OS installation media → Storage & saved ISOs** for storage and media management.
- The existing `/data` volume already has no GDeploy-imposed capacity limit; it uses its backing filesystem's available space. This release does not expand host disks or expose every host filesystem. Check `docker compose exec gdeploy df -h /data /tmp` if space is low.
- Preserve the existing data volume, `.env`, encryption key, account, certificate approvals, media and history. Finish or resolve active jobs before upgrading. Deleting a saved ISO removes that local copy; restore it from backup or upload/import it again if needed. Ubuntu installer support and the default `0.0.0.0:8000` binding are unchanged.

## [0.4.0] - 2026-09-30

### Added

- An **ESXi datastore** source in **Setup → OS installation media**. Browse datastores and folders on the saved standalone ESXi host, select an existing ISO and enter its publisher-verified SHA-256 checksum.
- Download and verify a persistent local copy in `/data/media` for unattended-install preparation. The copy retains its ESXi origin details and can be reused after restarts. GDeploy leaves the original datastore ISO unchanged and creates separate installation media for each deployment.
- Use the saved ESXi credentials and certificate verification for browsing and downloads. Changing the saved host requires a fresh media selection, preventing a stale selection from importing from another endpoint.
- Show import activity and errors while copying and verifying media. Failed imports retain the previous saved choice. Browser uploads and GDeploy server media remain available alongside the new source.

### Installation and compatibility

- Datastore media requires `Datastore.Browse` and file download access. Imports accept ISOs up to **16 GiB** and need enough free space on the GDeploy host for the retained copy plus later remastering workspace. Copying a large ISO can take several minutes.
- Existing administrator credentials, encryption keys, ESXi certificate approvals, mounted/uploaded media and deployment history are preserved. Back up imported copies with the complete data volume; no environment edit or container restart is required to select media.
- Automated installation still supports **Ubuntu Server 24.04 LTS amd64 live-server** media. ESXi import supplies a local source for the existing installer workflow; it does not add operating-system support.
- Versions before **0.4.0** cannot use ESXi-origin media selections. Finish or resolve active jobs and choose a verified uploaded or server-mounted ISO before rollback, preserving the data volume and encryption configuration.
- The installation and lab-acceptance guides now cover datastore browsing, copy verification, TLS/permission failures and source changes. Live ESXi acceptance remains required; automated checks do not establish compatibility with a particular host.

## [0.3.1] - 2026-09-30

### Fixed

- Give **Setup** separate, visible **ESXi connection** and **OS installation media** tabs so the ISO picker and upload form are easy to find. When the host is configured but no ISO is ready, Setup initially opens the media tab. The media tab also has a direct `#settings/media` link.
- Version static asset URLs and require browser revalidation of the application page and assets so a refreshed page loads the installed release's interface after an upgrade.
- Display the running release number in the app footer to make update verification straightforward.

### Installation and compatibility

- Update a source installation with `git pull --ff-only` and `docker compose up -d --build --wait`, then refresh the browser and check the displayed version. Open **Setup**, then **OS installation media** to select or upload an ISO.
- Preserve the existing `.env`, data volume and `media/` directory. This patch retains administrator credentials, ESXi certificate approvals, media selections, uploaded files and deployment history; it does not change installer support or the database format.

## [0.3.0] - 2026-09-30

### Added

- A **Setup** section containing **ESXi connection** and **OS installation media**, bringing initial GDeploy configuration into one place.
- Upload an OS ISO from the browser or choose an existing `.iso` in the server's read-only media directory. Enter the publisher's verified SHA-256 checksum; GDeploy validates the file before saving the selection and retains the previous choice if validation fails.
- Persist browser uploads under `/data/media` and the media selection in the app data volume. Changing the selected ISO needs no environment edit or container restart.
- Show upload progress and errors, discard rejected/incomplete uploads, and clean up abandoned upload files after a restart while keeping registered media.
- Save each new deployment's selected ISO path and checksum so later Setup changes do not switch media for queued or running jobs.

### Changed

- Use generic **OS ISO**, **OS only** and operating-system installation labels throughout the interface. Automated installation still supports **Ubuntu Server 24.04 LTS amd64 live-server** media; this release does not add other installers.
- Add preferred `GDEPLOY_OS_ISO` and `GDEPLOY_OS_SHA256` environment names while preserving `GDEPLOY_UBUNTU_ISO` and `GDEPLOY_UBUNTU_SHA256` compatibility. A saved Setup selection takes precedence over environment media settings.
- Update the installation walkthrough for browser-based media setup and simple source-image rebuilds using `git pull --ff-only` followed by `docker compose up -d --build --wait`.

### Installation and compatibility

- Existing administrator credentials, encryption keys, ESXi connection and certificate approvals, deployment history and environment media settings are preserved. Existing installations continue using their environment media configuration until an ISO is saved in Setup. When both environment naming schemes are set, `GDEPLOY_OS_*` takes precedence.
- Keep source files available and unchanged for queued/running jobs. **Delete & redeploy** creates a new job using the current media selection. Back up uploaded ISOs with the complete data volume and preserve the server's `media/` directory.
- Versions before **0.3.0** ignore saved media selections and the new environment names; provide the older version's verified media path/checksum configuration before rollback. Finish or resolve active jobs before changing versions.
- Docker continues to publish `0.0.0.0:8000` by default. Existing bind overrides, data volumes and the read-only `/media` mount are retained.

## [0.2.0] - 2026-09-29

### Added

- Retrieve and review the ESXi host certificate from **ESXi connection**, including subject, issuer, SHA-256 fingerprint, validity dates and DNS/IP names, then explicitly trust it for that exact host endpoint.
- Store approved certificates in the app database and use them for both ESXi API connections and datastore uploads. No CA file, environment edit or container restart is required. The optional manual private-CA setup remains available.
- Enforce the exact approved certificate and valid dates. Changed or renewed certificates require review and approval again. Removing an approval restores normal system/private CA verification.

### Fixed

- Honor `REQUESTS_CA_BUNDLE` for datastore uploads that use normal CA verification, while continuing to ignore proxy environment settings.

### Installation and compatibility

- Compare the displayed fingerprint against an independently trusted ESXi source before the first approval; fetching a certificate alone does not establish host identity. Endpoint-specific approval also supports host/IP names absent from the certificate's subject alternative names.
- The new trust table is additive. Existing administrator credentials, encryption keys and deployment data are preserved; include the full data volume in backups to retain approvals.
- The settings API now requires `verify_tls=true`, and new connections always verify TLS, including those using older saved settings or deployment snapshots with `verify_tls=false`. Approve the host certificate or configure a trusted CA for those installations; stored passwords and settings are not rewritten.
- Existing deployment snapshots retain their host credentials and use the current certificate approval for that endpoint on new connections. An already-open connection may finish with its previously authenticated certificate.
- Versions before **0.2.0** ignore saved certificate approvals and use their previous TLS settings, including any saved `verify_tls=false`. Review those settings and ensure certificate verification is enabled and trusts the intended host before rolling back.

## [0.1.5] - 2026-09-29

### Changed

- Source and release Compose now default to `0.0.0.0:8000`, publishing the web app on all host IPv4 interfaces. Fresh installations can be opened at `http://SERVER_LAN_IP:8000` without adding bind settings to `.env`.
- Updated the installation walkthrough and plain Docker example to use LAN access by default. A specific LAN address, `127.0.0.1`, or an SSH tunnel remains optional.

### Installation and compatibility

- Existing explicit `GDEPLOY_BIND_IP` and `GDEPLOY_PORT` values in `.env` are still respected. Installations without a bind override adopt the new all-interface default when the updated Compose configuration is applied.
- Apply binding changes with `docker compose up -d --force-recreate --wait`; no image rebuild is required for the mapping change. Preserve existing `.env` contents, administrator credentials, encryption keys and the data volume. The database format is unchanged.

## [0.1.4] - 2026-09-29

### Fixed

- Source Compose now supports `GDEPLOY_BIND_IP` and `GDEPLOY_PORT`, matching the release bundle. The default remains `127.0.0.1:8000`; set the server's LAN IPv4 address to enable access from another computer.
- Added LAN access instructions, direct Docker port-publishing examples, and troubleshooting for a loopback binding that remains unreachable despite a UFW allow rule.

### Installation and compatibility

- Edit the existing `.env` without overwriting keys or other settings, then apply the binding with `docker compose up -d --force-recreate --wait`. Changing the port mapping does not require rebuilding the image.
- This is a compatible configuration change. Administrator credentials, encryption keys, application data and the database format remain unchanged. Existing installations retain their current binding unless explicitly reconfigured.

## [0.1.3] - 2026-09-29

### Added

- Fresh automatic and manual installations now start with username `admin` and password `admin`, with a unique random encryption key for every installation.
- The first sign-in requires a new username and password before ESXi settings, deployments or their APIs can be used. Usernames must be 3–100 letters, digits, periods, underscores or hyphens, start with a letter or digit, and differ from `admin` regardless of capitalization. Passwords must be 12–1024 characters with matching confirmation.
- Completing account setup signs out every session. Sign in again with the new credentials; the default login no longer works. The chosen account persists across restarts and container rebuilds.

### Installation and compatibility

- Existing random/custom credentials are preserved on upgrade and are not reset to `admin`/`admin`. Only accounts still using the default password require the initial account change.
- A new administrator record is initialized once in the existing database. Later starts use that saved account; the original encryption key, bootstrap files and environment files remain unchanged.
- `bootstrap-credentials.txt` records only the initial login and is not updated with the chosen credentials. Back up the complete data volume and its matching encryption configuration.
- Do not blindly roll back below **0.1.3** after account setup: older versions ignore the saved administrator record and use the old bootstrap/environment credentials, which may be `admin`/`admin`. Restore a matching backup and review the login configuration before starting an older version.
- Docker Compose **2.24 or later**, `linux/amd64` images, installation-media requirements and live ESXi lab-validation requirements remain unchanged.

## [0.1.2] - 2026-09-29

### Fixed

- Start a fresh installation with `docker compose up --build -d` without first generating `.env`. GDeploy now creates private, persistent administrator credentials and an encryption key in the data volume, fixing the `GDEPLOY_SECRET_KEY is required` startup error.
- Reuse saved bootstrap credentials across restarts and container replacement. Partial environment credentials, missing keys for existing databases, corrupt saved credentials and conflicting credential sources stop startup with recovery guidance instead of silently generating a new key.
- Query draft releases directly through GraphQL to avoid GitHub release-list replication delays immediately after creation. API errors and incomplete responses continue to stop publication safely.

### Installation and compatibility

- Docker Compose **2.24 or later** is required for optional environment-file support. The source quick start and missing-key recovery commands are now first in the installation documentation.
- Retrieve automatically generated sign-in details with `docker compose exec gdeploy cat /data/bootstrap-credentials.txt`. The optional manual configuration script continues to write `bootstrap-credentials.txt` in the host setup directory.
- Existing valid `.env` credentials, encryption keys and data volumes remain in use; do not delete or regenerate them. Automatic installations must back up the complete data volume, including `/data/bootstrap.json`. An existing database whose original key is lost requires restoration from backup.
- Installation media and verified checksums are still required before VM deployment. They can be added to the optional `.env` after the web app starts without changing its administrator credentials.
- The application database format is unchanged. Docker host platform remains `linux/amd64`; live ESXi deployment still requires the documented lab acceptance checks.

## [0.1.1] - 2026-09-29

### Added

- First complete packaged release of GDeploy, including the application introduced in 0.1.0, the versioned Docker image, downloadable image archive and Ubuntu installation walkthrough.

### Fixed

- Detect draft releases using GitHub's authenticated release listing. GitHub's release-by-tag lookup can report a draft as missing, which stopped the initial 0.1.0 publication after its image and archive tests passed.
- Preserve published-version protection and handle paginated release history when preparing future iterations.

### Compatibility and validation

- Same application configuration and database format as 0.1.0; preserve the existing encryption key and data volume if upgrading from source.
- Docker host platform: `linux/amd64`. Live ESXi deployment still requires the lab acceptance checks described in the installation guide.

## [0.1.0] - 2026-09-29

### Added

- Docker-hosted dark interface with administrator sign-in, generated credentials, preflight checks, deployment history and recovery.
- Standalone ESXi 8.0 Update 3 provisioning with unattended Ubuntu Server 24.04 LTS installation, CPU/RAM/disk/datastore choices and DHCP/static IPv4.
- Separate Splunk, Elasticsearch and Kibana VMs; Kibana connects to Elasticsearch with a service token and verified TLS.
- Explicit credential reveal, encrypted secrets, ownership-checked delete/redeploy and restart recovery.
- Versioned `linux/amd64` image on GitHub Container Registry, plus a downloadable Docker image archive and SHA-256 checksums.
- Installation bundle and walkthroughs for a fresh Ubuntu server, existing Docker/Compose, and direct `docker run`.
- Automatic release packaging and publication on a validated `vX.Y.Z` tag.

### Packaging fixes

- Bootstrap produces separate Compose and Docker CLI environment files so generated passwords containing dollar signs work with both launch methods.
- The bootstrap tool can run inside the published image and export updated Docker environment settings without regenerating secrets.

### Known limitations

- Real ESXi boot, guest networking and application installation require the documented lab acceptance checks; automated tests do not replace that validation.
- One standalone ESXi host, one administrator and one worker per app installation. No vCenter, saved profiles or multi-replica operation.
- This release image supports `linux/amd64` Docker hosts. Ubuntu guest media and Splunk packages are supplied separately by the administrator.
