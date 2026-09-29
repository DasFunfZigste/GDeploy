# Changelog

Each numbered release has a matching Git tag, Docker image, installation bundle and GitHub Release page. Versions follow semantic versioning; the 0.x series is for lab validation before production use.

## [Unreleased]

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
