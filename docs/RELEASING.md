# Publishing an iteration of GDeploy

User-facing releases live at https://github.com/DasFunfZigste/GDeploy/releases. Each release includes a version-specific changelog, complete Ubuntu/Docker installation walkthrough, versioned image and immutable digest, deploy bundle, Docker image archive and SHA-256 checksums.

## Prepare a version

1. Choose a new semantic version (`X.Y.Z`). Patch versions fix compatible behavior, minor versions add compatible features, and major versions change compatibility. The initial 0.x series remains for lab validation.
2. Update `version` in `pyproject.toml` and `__version__` in `gdeploy/__init__.py`. The API reads the latter. Also update the default image version in `compose.release.yaml` and `Dockerfile`, and `GDEPLOY_VERSION` examples in `docs/INSTALL.md`.
3. Add a dated `## [X.Y.Z] - YYYY-MM-DD` section in `CHANGELOG.md`, describing user-visible changes, installation/upgrade implications and remaining limitations. Move completed items out of Unreleased.
4. Run `pytest -q`, `ruff check .`, and `python scripts/build_release.py --tag vX.Y.Z --validate-only`. Review installation examples and any database compatibility changes.
5. Commit and push the reviewed changes, then tag that exact commit:

   ```sh
   git tag -a vX.Y.Z -m "GDeploy vX.Y.Z"
   git push origin vX.Y.Z
   ```

The tag workflow tests the code, builds the image, verifies the image's bootstrap/Compose/Docker CLI login and persistence, publishes to GHCR, verifies a pull by digest and a Docker archive load, then builds and uploads release assets. It publishes the GitHub Release only after all those checks pass. A normal branch commit does not create a release; the tag defines a finished iteration.

## Distribution and permissions

The repository and GHCR package remain private. The workflow uses GitHub's short-lived `GITHUB_TOKEN` with `contents: write` and `packages: write`; no personal publishing token is stored. OCI metadata links the package to this repository. Users can pull with a classic PAT with `read:packages`, or download the private release's image archive using their repository access and load it locally.

The image currently targets `linux/amd64`. Versioned image tags are convenient references; the release also records the immutable registry digest. No moving `latest` image tag is published, so deployment examples always select an explicit version. The latest-release page is available at https://github.com/DasFunfZigste/GDeploy/releases/latest.

The release bundle uses an explicit file allowlist. It must never contain `.env`, `docker.env`, bootstrap credentials, data volumes, private certificates, Ubuntu ISOs or Splunk installers. Checksums cover each downloadable payload; GitHub's authenticated release transport supplies the download source, while the checksums detect corrupted or mismatched files.

## Failed publication

Before publication, the workflow may leave a private image tag or draft release. Inspect the failed run and rerun only if the same tagged source is still correct; changing code requires a new version/tag. Published release versions are never overwritten by the workflow. If a draft is retained, a successful rerun refreshes its assets and notes before publishing. A release does not assert that live ESXi acceptance has passed unless that testing has actually been completed and recorded.

Existing installations preserve `.env` and their chosen data volume through upgrades. Never regenerate the encryption key for an existing database. Document backup or migration requirements in each release's changelog before publication.
