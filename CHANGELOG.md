# Changelog

Each numbered release has a matching Git tag, Docker image, installation bundle and GitHub Release page. Versions follow semantic versioning; the 0.x series is for lab validation before production use.

## [Unreleased]

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
