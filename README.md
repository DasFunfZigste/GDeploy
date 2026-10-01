# GDeploy

A dark, Docker-hosted control panel that creates VMs on **standalone VMware ESXi 8.0 Update 3**, installs the operating system from your selected OS ISO, and installs selected applications. Splunk, Elasticsearch and Kibana each receive their own VM. Kibana connects to Elasticsearch using a service account token and verified TLS; Splunk runs independently.

**Supported installer:** Ubuntu Server **24.04 LTS amd64 live-server** installation media. The generic OS ISO labels do not add support for other installers. GDeploy supports one administrator, one ESXi host and one VM per selected role per deployment. This is implementation-ready lab software; real ESXi deployment must be validated with your host, license, installation media and network before relying on it for production.

## Quick start from source

With Git, Docker Engine and **Docker Compose 2.24 or later** installed, run:

```sh
git clone https://github.com/DasFunfZigste/GDeploy.git
cd GDeploy
docker compose up --build -d --wait
```

The private repository requires your existing GitHub access. Building from source does not require a GHCR login. Open **`http://SERVER_LAN_IP:8000`** from another computer, or **http://localhost:8000** on the Docker host, and sign in with **username `admin` and password `admin`** on a fresh installation. The first sign-in requires a new username and password before you can use the app. After saving them, sign in again with your new credentials. The default settings enable LAN access immediately; replace `SERVER_LAN_IP` with the Docker host's actual address.

On a fresh data volume, GDeploy creates the initial `admin`/`admin` account and a unique random encryption key automatically. Choose a username of 3–100 letters, digits, periods, underscores or hyphens, starting with a letter or digit; it cannot be `admin` in any capitalization. Choose a password of 12–1024 characters and enter it again to confirm. Completing setup signs out all sessions, and `admin`/`admin` no longer works. Open **Setup**, use the **ESXi connection** tab to configure your host, then select the **OS installation media** tab to choose an OS ISO. No initial `.env` file or separate configuration command is required.

### Default network access and optional restrictions

Source and release Compose publish **`0.0.0.0:8000`** by default, so a fresh installation is available on every host IPv4 interface without creating `.env`. Browse to **`http://SERVER_LAN_IP:8000`** using the Docker host's LAN address.

Existing explicit `GDEPLOY_BIND_IP` values in `.env` are still respected. To restrict access, edit that value to a specific LAN IPv4 address or `127.0.0.1` for local access/an SSH tunnel. `GDEPLOY_PORT` optionally changes port 8000. **Preserve other settings and encryption keys**, and keep one value per setting. Apply a changed binding without rebuilding the image:

```sh
docker compose up -d --force-recreate --wait
docker compose port gdeploy 8000
```

Keep `GDEPLOY_COOKIE_SECURE=false` for direct HTTP, or use `true` with an HTTPS reverse proxy. Docker-published ports can bypass UFW rules. The [installation guide](docs/INSTALL.md) includes the optional SSH tunnel and proxy setup.

**Already cloned, but startup failed with `GDEPLOY_SECRET_KEY is required`?** Run these commands from your existing GDeploy checkout:

```sh
git pull --ff-only
docker compose up --build -d --wait --force-recreate
```

Keep your existing `.env` and data volume. A fresh installation starts with `admin`/`admin` and the required account setup. Existing random or custom credentials remain valid when upgrading; they are not reset to the default. If an existing database has lost its original key, restore that key from backup; GDeploy refuses to replace it with a new one. The installation guide includes initial-password lookup for older installations.

## Included

- A guided form with a visible configuration section for every selected VM, each with its own name, vCPUs, RAM, disk, datastore, port group, DHCP or static IPv4.
- A **Setup** section with **ESXi connection**, **OS installation media** and **SSH access** tabs. Choose an ISO from an ESXi datastore, upload it through the browser, or select it from the GDeploy server's media directory. Optionally save your SSH public keys for the `gdeploy` user on future VMs.
- An optional **OS only** VM alongside the application roles.
- Media checksum, inventory, name, capacity and network-input checks before provisioning.
- Retrieve, inspect and explicitly trust an ESXi certificate from Setup, with no container restart.
- Unattended Ubuntu installation using a remastered, bootable ISO and NoCloud autoinstall configuration.
- Unique generated guest/application credentials, encrypted storage and an explicit **Credentials** view in every deployment.
- Stage progress, persisted events and expandable, copyable deployment logs with sanitized error details. Interrupted work is identified on restart.
- Reversible history hiding for finished jobs, with **Show hidden** and **Restore**. VMs, logs and credentials remain available.
- Data-filesystem capacity and saved ISO usage in Setup, with protected deletion of unused uploaded or ESXi-imported copies.
- Failed-deployment **delete & redeploy**, with typed confirmation and strict resource ownership checks.
- Web-app authentication, required replacement of default sign-in credentials, expiring sessions, CSRF protection and sign-in throttling.

Saved deployment profiles are outside this initial scope.

![GDeploy deployment dashboard](docs/overview.png)

## Install a published release

Open [GitHub Releases](https://github.com/DasFunfZigste/GDeploy/releases/latest) for the current version, changelog, exact Docker image digest, downloadable deployment bundle and Docker image archive. Each release page includes the complete installation walkthrough.

The [installation guide](docs/INSTALL.md) covers installing Docker on a fresh Ubuntu server, downloading the release, starting it with an existing Docker/Compose setup, and using plain `docker run`. The published image is `ghcr.io/dasfunfzigste/gdeploy:0.7.0` for `linux/amd64`. Repository and image access are private; the guide includes both registry authentication and an image-archive alternative.

See [CHANGELOG.md](CHANGELOG.md) for version history and [RELEASING.md](docs/RELEASING.md) for the repeatable release process.

## Prepare installation media and deploy

1. Use a Docker host that can reach both ESXi and the guest network. Allow space for the OS ISO and several GiB more for temporary installation media.
2. Open **Setup**, select the **ESXi connection** tab, and enter your ESXi hostname or IP. If its certificate is not already trusted, retrieve it, compare its SHA-256 fingerprint against a trusted ESXi source, and select **Trust certificate**. Then save your ESXi username/password and test the connection to load inventory.
3. Select the **OS installation media** tab at the top of Setup. Choose **ESXi datastore** to browse your saved host's datastores and folders for an existing ISO, upload the supported live-server ISO from your computer, or select a `.iso` file from the GDeploy server's `media/` directory. Enter its publisher's verified **SHA-256 checksum**, then save the selection. ESXi ISOs are copied to GDeploy for verification and unattended-install preparation; the original datastore file is kept unchanged. Browser uploads and ESXi copies persist in `/data/media`; server files use the read-only `/media` mount. No environment edit or container restart is needed. Copying and hashing a large ISO can take several minutes.
4. For Splunk, place your licensed **Splunk Enterprise Linux x86_64 .tgz** at `media/splunk.tgz`, verify its publisher checksum, and set `GDEPLOY_SPLUNK_SHA256` in the optional `.env` to that value. Preserve existing settings and apply this environment change with `docker compose up -d --force-recreate --wait`. GDeploy does not download Splunk or supply a license. Server-mounted media must be readable by container UID 10001: use directory mode 0755 and file mode 0644, or equivalent ACLs. Keep secrets out of `media/`.
5. Select **New deployment**, give the deployment a label and choose application roles. In **Configure VMs**, fill in the separate section for each role: its VM name, CPU, RAM, disk, datastore, port group and network settings. Selecting Elasticsearch and Kibana displays two VM sections; the deployment label does not replace either VM name. Run preflight, review the results, and deploy. Open the resulting deployment for progress, logs, endpoints and **Credentials**.

ESXi imports and browser uploads support ISOs up to **16 GiB**. The GDeploy host needs room for the retained local copy plus temporary remastering space. Datastore browsing requires `Datastore.Browse` and permission to download the selected file, using the saved ESXi account and certificate trust.

The saved OS ISO and checksum are used for future deployments. Each queued deployment retains its selected local ISO and checksum, so later Setup changes do not switch its source media. Keep that local file available and unchanged until the deployment finishes. An imported ESXi copy can be reused without downloading the original again; changes to the original after a completed import do not change that copy. Existing environment configuration still works when no selection has been saved in Setup: use `GDEPLOY_OS_ISO` and `GDEPLOY_OS_SHA256`, or the compatible legacy `GDEPLOY_UBUNTU_ISO` and `GDEPLOY_UBUNTU_SHA256` names.

To update a source installation for testing, run these commands in the same checkout:

```sh
git pull --ff-only
docker compose up -d --build --wait
```

Keep the existing `.env`, `media/` directory and data volume. This rebuilds the image and replaces the app container while preserving your account, ESXi trust, ISO selection, uploaded/imported media and history. Refresh the browser and check **v0.7.0** in the app footer. Open **Setup**, then **OS installation media**; you can also go directly to `http://SERVER_LAN_IP:8000/#settings/media`.

If you prefer to supply credentials through environment files, run `python3 scripts/configure.py` with Python 3.12 or later **before the first start on a fresh volume**. This optional method creates `.env`, `docker.env` and a host-side `bootstrap-credentials.txt`, with the same initial `admin`/`admin` login and a unique random encryption key; the [installation guide](docs/INSTALL.md) also shows how to run it inside Docker. Existing manually configured installations retain their original credentials, `.env` and encryption key. Do not run the generator after automatic setup or as an upgrade step.

The default Compose binding is `0.0.0.0:8000`. You can optionally restrict the bind address as described above or add an HTTPS reverse proxy. For a proxy, forward the original `Host` and `X-Forwarded-Proto`, set `GDEPLOY_COOKIE_SECURE=true`, and set `FORWARDED_ALLOW_IPS` to the actual proxy address or trusted proxy network as seen by the container. Keep the app port private to that proxy; the cookie setting must match how the browser accesses the application.

## ESXi and network preparation

- Use a standalone ESXi 8.0 U3 endpoint. vCenter, distributed switches and clusters are outside this release.
- The ESXi account needs permission to read inventory, create/configure/power/delete VMs, allocate datastore space and manage the deployment's ISO files. Selecting existing ESXi media also requires `Datastore.Browse` and file download access. ESXi licensing must permit provisioning through the vSphere API. Inventory checks prove authentication and read access; actual deployment operations validate write privileges and license capabilities.
- The app connects to ESXi over HTTPS **443** for API operations and datastore downloads/uploads. It verifies the certificate using the exact certificate you approved for that endpoint, or normal system/private CA trust when no certificate is approved.
- The app must reach guests on SSH **22**. Guests need DNS and HTTPS/HTTP access to Ubuntu mirrors and the official Elastic package repository; installation cannot complete on an isolated network without corresponding mirror support.
- Kibana must reach Elasticsearch on HTTPS **9200**. Your browser needs access to Kibana **5601** and Splunk Web **8000**. Splunk's local verification uses its management API on **8089**. GDeploy does not configure your network firewall or switch.
- Use static addresses or DHCP reservations for application VMs. Elasticsearch's certificate and Kibana's configuration use the assigned Elasticsearch IP; a later DHCP address change requires reconfiguration.
- Static networking takes an IPv4 CIDR address, gateway and DNS servers. Preflight checks syntax and in-app reservations; it cannot guarantee an address is unused elsewhere on your LAN.
- ESXi and guest time should be synchronized for TLS.

### Trust an ESXi certificate in the app

In **Setup → ESXi connection**, retrieve the certificate for the entered host. Review its subject, issuer, validity dates, DNS/IP names and **SHA-256 fingerprint**. Compare that fingerprint with the certificate shown through a trusted ESXi management session before selecting **Trust certificate**; retrieving a certificate alone does not establish the host's identity. Save the connection credentials and test the connection.

The approval is stored in the app database for that exact host/IP and survives restarts. API connections and datastore downloads/uploads require the approved certificate and valid dates. A changed or renewed certificate must be retrieved, reviewed and trusted again. Explicit approval also supports an ESXi IP address that is absent from the certificate's DNS/IP names. Removing trust in the same screen restores normal system/private CA verification. No CA file, environment edit or restart is needed for this workflow.

As an optional alternative for a private CA, make a PEM bundle containing the normal public roots plus that CA and mount it read-only (for example under `/media/esxi-ca-bundle.pem`). Set both `SSL_CERT_FILE=/media/esxi-ca-bundle.pem` and `REQUESTS_CA_BUNDLE=/media/esxi-ca-bundle.pem` in `.env`, then recreate the container. This allows the Python SOAP client and HTTPS file transfers to validate the same certificate chain and hostname. Do not commit private keys.

## Credentials and application behavior

- Open **Setup → SSH access** to save one or multiple OpenSSH public keys, one per line. GDeploy installs the saved keys for the `gdeploy` user on every VM in newly queued deployments, alongside its separate automation key. You can review fingerprints and edit or clear the saved list. Ed25519, RSA (2048 bits or larger) and ECDSA are supported, with up to 50 keys; duplicate key material is saved once. Keep private keys on your own workstation. See the [SSH setup instructions](docs/INSTALL.md#configure-ssh-access-for-new-vms) for key creation and login examples.
- Each queued deployment captures its key list. Later Setup edits do not change queued jobs or existing VMs, and removing a saved key does not revoke access on an existing VM. A new delete-and-redeploy job uses the latest saved keys. Older queued jobs with no saved-key snapshot keep their original automation key only.
- The chosen web-app username and password hash are stored in the database and survive container rebuilds. The initial `bootstrap-credentials.txt` is not updated after account setup; save your chosen credentials in your password manager. Account setup leaves the encryption key and bootstrap/environment files unchanged.
- Each VM receives the OS administrator **`gdeploy`**, a different random password, and a separate automation SSH key. VM names remain hostnames; they are not passwords.
- Open a deployment and select **Reveal credentials** to see the exact VM username/password, IP, SSH host key, application URL and application credentials. Reveals are recorded in the deployment events. The browser conceals the panel automatically after one minute and when navigating away. Copying a value leaves it in your operating-system clipboard until replaced.
- ESXi credentials, guest passwords, SSH private keys and application secrets are encrypted using a persistent key. Automatic installations keep it in `/data/bootstrap.json` inside the data volume; manually configured installations use `.env`. Back up the data volume, including `bootstrap.json`, and any existing environment files; losing the original key prevents decryption. Application administrators can intentionally reveal deployment passwords.
- Splunk receives its own `admin` password. Selecting Splunk requires explicit acceptance of its software license. License activation beyond the supplied package's initial behavior is a separate administration step.
- Elasticsearch is a single-node install from the official **9.x** apt repository. Kibana uses the exact installed Elasticsearch package version. Both packages are held to prevent uncoordinated upgrades. Plan upgrades and backups separately.
- Elasticsearch uses a deployment CA; Kibana verifies that CA and uses a dedicated Elastic service token. Initial interactive Kibana and Elasticsearch sign-in uses the generated `elastic` administrator credentials. Create narrower application users for ongoing use.
- Application browser endpoints use generated certificates. Trust the appropriate certificates through your normal administration process, or replace them with your organization's certificates. The generated service certificates expire after 825 days; the Elasticsearch CA expires after 10 years.
- First SSH contact uses trust on first use over your managed guest network. The observed key is persisted and required for later connections. This is not a substitute for a trusted provisioning network.

## Failure recovery

To remove a finished record from the default **Deployment history**, select **Hide** in its row or **Hide from history** on its detail page. Check **Show hidden** to include hidden records, then select **Restore** to show one normally again. Direct links to hidden deployments still open their details. Queued, running and cleaning work cannot be hidden.

Hiding preserves the VMs, original job status, logs, credentials and resource reservations. It does not complete failed checks, resume provisioning or remove attached installation media. If a VM is healthy but its deployment record failed a readiness check, you can keep that VM and hide the record. Inspect the guest and logs before deciding whether any unfinished work needs attention.

When a stage fails, select **View deployment logs** in its error banner, or **View logs** in the **Deployment logs** card. Expand and copy the sanitized diagnostics to identify the failing step; older entries can show only the detail recorded at the time. Check the ESXi console before selecting **Delete & redeploy**. Type the deployment name to confirm permanent deletion of that deployment's VMs and virtual disks, including any data already written. A fresh deployment uses the saved VM choices, the current OS ISO selection and new credentials.

Version 0.5.0 fixes media preparation when extracted GRUB/manifest files retain read-only permissions, and places that work's temporary files under `/data/artifacts`. It modifies only private working files. If preparation still fails, use the expanded logs to distinguish permissions, storage, source-media and tool errors; the ISO filename or an older generic error cannot identify the cause alone.

Cleanup verifies both the deployment UUID annotation and datastore paths before deleting a VM. It discovers tagged VMs even if the app stopped before recording their IDs. Shared datastores, networks, source media and unrelated VMs are retained. Manually moving a disk outside its deployment directory causes cleanup to stop for inspection. If deletion fails, no replacement is started; retry after fixing the cause. If cleanup succeeds but the new preflight fails, the error still appears under delete/redeploy and a later retry is safe.

A timed-out ESXi operation can still be running on the host. Inspect its tasks before retrying. OS installation has a configurable one-hour limit (`GDEPLOY_OS_TIMEOUT`); software stages also use bounded waits.

## Persistence and operation

The Compose data volume mounted at `/data` contains the SQLite administrator account, approved ESXi certificates, saved OS ISO selection and history, encrypted secrets, uploaded/imported ISOs under `/data/media`, and temporary media work. Automatic installations also keep `bootstrap.json` and the initial `bootstrap-credentials.txt` there. Source Compose normally names the volume `gdeploy_gdeploy-data`; the release bundle defaults to `gdeploy-data`. GDeploy removes its deployment-specific installation ISO from the datastore after the OS is ready. Original ESXi source ISOs and server files in `media/` are never modified or deleted by that cleanup. Deployment history is retained after cleanup.

Open **Setup → OS installation media → Storage & saved ISOs** to check capacity and remove unused copies. The displayed capacity belongs to the filesystem backing `/data` as seen inside the container; it is not a whole-host disk inventory. The default volume has no GDeploy storage quota and uses available space on that filesystem. The separate 256 MiB `/tmp` mount is not the ISO workspace limit. You can also check with `docker compose exec gdeploy df -h /data /tmp`.

Select **Delete** beside an unused upload or ESXi copy, then confirm **Delete ISO**. Selected media and files referenced by queued, running or cleaning deployments are protected. To remove the current default, select **Clear saved selection** or choose another ISO first. Clearing does not delete the file; existing environment media settings become the fallback. This lets you clear and delete your only unused copy without needing room for a replacement upload. Active deployments keep their original source and protection. Deletion removes only GDeploy's saved copy and leaves original ESXi files and the read-only server media mount untouched. The [storage walkthrough](docs/INSTALL.md#manage-storage-and-saved-isos) explains capacity limits, cleanup and expanding the backing storage when needed.

Run **one app container with one worker** against a data volume. Jobs are serialized; this release does not support multiple replicas. The container runs without root privileges or Linux capabilities, with a read-only root filesystem. Stop the app before taking a consistent backup of its complete data volume, including automatic bootstrap files, and any existing `.env`/`docker.env`. Store your chosen web-app credentials in your password manager; the private bootstrap credentials file is not a substitute for a backup. Do not use `docker compose down -v` unless you intend to erase the app's database and credentials.

## Development and validation

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
pip install -e '.[test]'
pytest -q
ruff check .
```

Backend tests cover sign-in/CSRF, secret redaction and encryption, recovery, topology validation, media generation, VMware task failures and ownership boundaries. Guest scripts are syntax-checked and VMware configuration uses the real SDK types in tests. GitHub CI runs the test suite and builds/smoke-tests the Docker image. Mocked tests cannot prove firmware boot, unattended installation, ESXi licensing, networking or package behavior on your host; use the [lab acceptance checklist](docs/LAB_VALIDATION.md) before the first real workload.

Architecture: FastAPI serves the same-origin static interface; SQLite persists jobs/events and encrypted secrets; one background worker uses pyVmomi for ESXi and SSH for Ubuntu/application setup. See [architecture notes](docs/ARCHITECTURE.md).
