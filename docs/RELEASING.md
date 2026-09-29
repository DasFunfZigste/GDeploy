# Publishing an iteration of GDeploy

User-facing releases live at https://github.com/DasFunfZigste/GDeploy/releases. Each release includes a version-specific changelog, complete Ubuntu/Docker installation walkthrough, versioned image and immutable digest, deploy bundle, Docker image archive and SHA-256 checksums.

## Prepare a version

1. Choose a new semantic version (`X.Y.Z`). Patch versions fix compatible behavior, minor versions add compatible features, and major versions change compatibility. The initial 0.x series remains for lab validation.
2. Update `version` in `pyproject.toml` and `__version__` in `gdeploy/__init__.py`. The API reads the latter. Also update the default image version in `compose.release.yaml` and `Dockerfile`, and image/version examples in `README.md` and `docs/INSTALL.md`.
3. Add a dated `## [X.Y.Z] - YYYY-MM-DD` section in `CHANGELOG.md`, describing user-visible changes, installation/upgrade implications and remaining limitations. Move completed items out of Unreleased.
4. Run `pytest -q`, `ruff check .`, and `python scripts/build_release.py --tag vX.Y.Z --validate-only`. Review installation examples and any database compatibility changes.
5. Commit and push the reviewed changes, then tag that exact commit:

   ```sh
   git tag -a vX.Y.Z -m "GDeploy vX.Y.Z"
   git push origin vX.Y.Z
   ```

The tag workflow tests the code, builds the image, verifies automatic and manual bootstrap, required replacement of default credentials, Compose/Docker CLI login and account persistence, publishes to GHCR, verifies a pull by digest and a Docker archive load, then builds and uploads release assets. It publishes the GitHub Release only after all those checks pass. A normal branch commit does not create a release; the tag defines a finished iteration.

## Distribution and permissions

The repository and GHCR package remain private. The workflow uses GitHub's short-lived `GITHUB_TOKEN` with `contents: write` and `packages: write`; no personal publishing token is stored. OCI metadata links the package to this repository. Users can pull with a classic PAT with `read:packages`, or download the private release's image archive using their repository access and load it locally.

The image currently targets `linux/amd64`. Versioned image tags are convenient references; the release also records the immutable registry digest. No moving `latest` image tag is published, so deployment examples always select an explicit version. The latest-release page is available at https://github.com/DasFunfZigste/GDeploy/releases/latest.

The release Compose file requires Docker Compose 2.24 or later so `.env` can be optional. A fresh data volume starts with `admin`/`admin` and a unique random encryption key; the manual configuration script uses the same initial login. Verify that business APIs remain blocked until the required account change, the completed change invalidates existing sessions, and the default login then fails. Existing random/custom credentials must remain valid on upgrade, and a saved database account must survive restart without being overwritten by bootstrap settings. Check both configuration paths and ensure a missing key for existing data cannot cause silent key replacement. The source quick start and startup recovery instructions in `docs/INSTALL.md` are included directly on each release page.

For ESXi certificate trust, validate that both API and datastore upload connections enforce the approved certificate for the exact endpoint, reject changed/expired/not-yet-valid certificates, and retain approvals after restart. Check trust removal and the ordinary CA verification path. Record actual ESXi acceptance separately from local TLS transport tests.

The release bundle uses an explicit file allowlist. It must never contain `.env`, `docker.env`, `bootstrap.json`, bootstrap credentials, data volumes, private certificates, Ubuntu ISOs or Splunk installers. Checksums cover each downloadable payload; GitHub's authenticated release transport supplies the download source, while the checksums detect corrupted or mismatched files.

## Failed publication

Before publication, the workflow may leave a private image tag or draft release. Inspect the failed run and rerun only if the same tagged source is still correct; changing code requires a new version/tag. Published release versions are never overwritten by the workflow. If a draft is retained, a successful rerun refreshes its assets and notes before publishing. A release does not assert that live ESXi acceptance has passed unless that testing has actually been completed and recorded.

Existing installations preserve any `.env`/`docker.env` and their chosen data volume through upgrades. Automatic installations keep their encryption key and initial administrator hash in `/data/bootstrap.json`; their backup must include that file alongside the database containing the chosen administrator account. Bootstrap credential files record only the initial login and are not updated after account setup. Never regenerate the encryption key for an existing database or recommend deleting a volume to fix startup. Restore missing or damaged credentials from the matching backup. Document backup or migration requirements in each release's changelog before publication. The administrator-account migration introduced in version **0.1.3** keeps existing credentials, but older application versions ignore that database account and use the old bootstrap/environment login; rollback notes must address that difference explicitly.

The certificate-trust table introduced in version **0.2.0** is additive and belongs in the complete data-volume backup. Older versions ignore these approvals and use their previous TLS settings, including any saved `verify_tls=false`. Release and rollback notes must require reviewing those settings and ensuring certificate verification is enabled. A host trusted only through an app approval will need a CA trust configuration for verification in the older release.
