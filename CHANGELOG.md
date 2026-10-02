# Changelog

Each numbered release has a matching Git tag, Docker image, installation bundle and GitHub Release page. Versions follow semantic versioning; the 0.x series is for lab validation before production use.

## [Unreleased]

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
