# Install GDeploy

This guide installs **GDeploy 0.6.0** on an Ubuntu Server **22.04 or 24.04 LTS, amd64/x86_64** host. Use the source quick start below, or download the prebuilt image `ghcr.io/dasfunfzigste/gdeploy:0.6.0` using the numbered walkthrough. Both methods require Docker Engine; Compose examples require **Docker Compose 2.24 or later**. GDeploy provisions guests on standalone **ESXi 8.0 Update 3**. The supported OS ISO is currently **Ubuntu Server 24.04 LTS amd64 live-server**; the generic media labels do not add support for other operating systems.

Find each version, its changes, image digest and downloadable files on the [GitHub Releases page](https://github.com/DasFunfZigste/GDeploy/releases). The repository and its package are private, so sign in with a GitHub account that has access.

## Quick start: clone and run

With Git, Docker Engine and Docker Compose 2.24 or later already installed:

```sh
git clone https://github.com/DasFunfZigste/GDeploy.git
cd GDeploy
docker compose up --build -d --wait
```

Use your existing GitHub access for the private repository. This source build needs no GHCR login, host Python installation or initial `.env` file. On a fresh data volume, GDeploy creates the initial `admin`/`admin` account and a unique random encryption key automatically. The clone follows the default branch; use the prebuilt walkthrough for the specific release shown on this page.

Open **`http://SERVER_LAN_IP:8000`** from another computer, replacing `SERVER_LAN_IP` with the Docker host's actual LAN address. On the Docker host itself, use [http://localhost:8000](http://localhost:8000). Fresh installations publish port 8000 on every host IPv4 interface (`0.0.0.0`), so LAN access needs no `.env` changes.

Sign in with **username `admin` and password `admin`** on a fresh installation, then complete the required account setup described below. If Docker is not installed yet, complete section 1 and return here. An SSH tunnel remains optional; section 5 shows how to use one.

The UI can start before installation media is ready. Open **Setup**, configure your host in the **ESXi connection** tab, then select the **OS installation media** tab. Choose an ISO from an **ESXi datastore**, upload it from your computer, or select a `.iso` file in the GDeploy server's `media/` directory. Enter its publisher's verified SHA-256 checksum and save it. An ESXi ISO is copied to GDeploy for unattended-install preparation, leaving the original untouched. The OS ISO workflow needs no `.env` edit or container restart. Sections 3 and 6 explain media preparation and Setup. Splunk still uses a separate licensed package at `media/splunk.tgz` and its `GDEPLOY_SPLUNK_SHA256` environment setting.

To update and rebuild for testing later, run these commands in the same clone:

```sh
git pull --ff-only
docker compose up -d --build --wait
```

Keep the existing `.env`, `media/` directory and data volume. Rebuilding preserves your account, ESXi connection and certificate approvals, selected ISO, uploaded/imported copies and deployment history. Refresh the browser and check **v0.6.0** in the app footer to confirm the update. Before upgrading with live deployments, complete the backup steps in section 8.

## Optional: restrict the bind address or change the port

Both source and release Compose default to **`0.0.0.0:8000`**, publishing the app on all host IPv4 interfaces. Existing explicit `GDEPLOY_BIND_IP` values in `.env` are still respected; an older `127.0.0.1` setting continues to allow only local connections.

To restrict access, edit the existing `.env` in your setup directory, or create it if it is absent. **Preserve existing contents and encryption keys**, and keep one value per setting. For example, restrict access to the Docker host or an SSH tunnel with:

```dotenv
GDEPLOY_BIND_IP=127.0.0.1
GDEPLOY_PORT=8000
```

You can instead set `GDEPLOY_BIND_IP` to a specific LAN IPv4 address, or `0.0.0.0` to use the default on all interfaces. `GDEPLOY_PORT` changes the host port. Recreate the container to apply changed mappings; no image rebuild is needed:

```sh
docker compose up -d --force-recreate --wait
docker compose port gdeploy 8000
```

Use the selected address and port in the browser URL. If binding to one LAN address, also use that address in the host-side health checks later in this guide. Docker-published ports can bypass UFW rules; a firewall allow rule does not override an explicit loopback-only Docker binding.

For direct HTTP, keep `GDEPLOY_COOKIE_SECURE=false`. For an HTTPS reverse proxy, use `true` and the proxy settings in section 5.

## Complete the first sign-in

Fresh automatic installations and fresh manual configuration both start with **`admin` / `admin`**. The first sign-in opens account setup. Before you can access ESXi settings, deployments or their APIs, choose:

- A **new username** with 3–100 letters, digits, periods, underscores or hyphens, starting with a letter or digit. It cannot be `admin` in any capitalization.
- A **new password** with 12–1024 characters, entered twice to confirm.

Save the changes, then sign in again with your new credentials. Completing setup signs out every existing session, and the default login no longer works. Store the chosen credentials in your password manager.

The administrator account is saved in the database and survives restarts and container rebuilds. Account setup does not change the encryption key or rewrite `.env`, `docker.env` or `bootstrap.json`. Any `bootstrap-credentials.txt` records the **initial login only**; it does not contain your new password.

Existing random or custom credentials from an older installation remain valid on upgrade, with no forced rename or reset to `admin`/`admin`. Accounts still using the default password must complete account setup.

## Fix an existing source checkout that cannot start

If the container reports **`GDEPLOY_SECRET_KEY is required`**, run this from the existing checkout:

```sh
git pull --ff-only
docker compose up --build -d --wait --force-recreate
```

Keep your existing `.env` and data volume. On a fresh volume, both credential settings may be absent or empty; sign in with `admin`/`admin` and complete account setup. Existing installations retain their current credentials, and a completed setup is not repeated after restart or rebuild.

For an older automatic installation whose initial password you have not changed, retrieve its original sign-in details with:

```sh
docker compose exec gdeploy cat /data/bootstrap-credentials.txt
```

If you previously used `scripts/configure.py`, its initial password is in **`bootstrap-credentials.txt` in the host setup directory** instead. These files show only the initial login. After completing account setup, use your chosen credentials; bootstrap files and environment settings do not reset the saved account. Do not run the configuration generator to repair an existing installation.

If a database already exists without its original encryption key, startup stops with a recovery message. Restore the matching original `.env`/`docker.env`, or `/data/bootstrap.json` for an automatic installation, from backup. Supplying only one of `GDEPLOY_SECRET_KEY` and `GDEPLOY_ADMIN_PASSWORD_HASH`, damaged saved credentials, or conflicting environment and saved credentials also stops startup instead of rotating the key. Preserve the files and use the logged recovery guidance; deleting the volume would erase history and credentials.

If Compose rejects `env_file.required`, upgrade to Compose 2.24 or later using the Docker installation method in section 1.

## Install a prebuilt release

The remaining sections cover prebuilt deployment; they do not require a source checkout or local build.

- **New Ubuntu server:** complete section 1, then continue with section 2.
- **Docker already running:** skip section 1. Confirm `docker version` and `docker compose version` work, then begin at section 2.
- **Prefer `docker run`:** complete sections 2–4, then use section 7 instead of the Compose startup commands in section 5.

## 1. Install Docker on a new Ubuntu server

Use a normal account with `sudo` access. These steps follow [Docker's official Ubuntu installation guide](https://docs.docker.com/engine/install/ubuntu/). Check the architecture; this release requires `amd64`:

```sh
dpkg --print-architecture
```

This section is for a fresh host. If another Docker or container runtime is already installed, follow Docker's guidance for conflicting packages before replacing it; an existing Docker deployment can use section 2 directly.

Add Docker's signing key and package repository:

```sh
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
```

The rest of this guide uses `docker` without `sudo`. To allow your account to do that, add it to Docker's group:

```sh
sudo usermod -aG docker "$USER"
```

**Log out and reconnect to the server**, then check:

```sh
docker version
docker compose version
```

Docker group membership grants root-level control of the host. If your administration policy requires `sudo docker` instead, use it consistently for image login, pulling, configuration and startup below. The configuration command still uses your normal user's UID/GID so its files belong to you.

## 2. Download the release and its image

### Download the deployment files

Install the GitHub CLI if it is not already present. On Ubuntu:

```sh
sudo apt update
sudo apt install -y gh
gh auth login --hostname github.com --git-protocol https --web
```

Complete the browser sign-in with an account that can read `DasFunfZigste/GDeploy`. On a headless server, open the displayed device-login URL on your own computer and enter the code. If the Ubuntu package is unavailable, use the [GitHub CLI's official Linux installation instructions](https://github.com/cli/cli/blob/trunk/docs/install_linux.md).

Download the selected release into a stable installation directory:

```sh
GDEPLOY_VERSION=0.6.0
mkdir -p "$HOME/gdeploy/downloads"
cd "$HOME/gdeploy"

gh release download "v${GDEPLOY_VERSION}" \
  --repo DasFunfZigste/GDeploy \
  --pattern "gdeploy-${GDEPLOY_VERSION}-deploy.tar.gz" \
  --pattern SHA256SUMS \
  --dir downloads

cd downloads
sha256sum --check --ignore-missing SHA256SUMS
cd ..
tar -xzf "downloads/gdeploy-${GDEPLOY_VERSION}-deploy.tar.gz"
```

Continue only if the downloaded archive reports **OK**. `--ignore-missing` skips checksum entries for optional release assets you have not downloaded. The bundle includes `compose.yaml`, `scripts/configure.py`, documentation, `.env.example` and an empty `media/` directory. It does not contain passwords, Ubuntu or Splunk installation media, or an existing GDeploy database.

Alternatively, use your signed-in browser to download the bundle and `SHA256SUMS` from the release's **Assets**, copy both files to the server's `~/gdeploy/downloads` directory, then run the checksum and extraction commands above. This avoids installing the GitHub CLI.

### Pull the Docker image

GitHub Container Registry authentication is separate from `gh auth login`. Create a [personal access token (classic)](https://github.com/settings/tokens) with **`read:packages`** and access to the private package, then run:

```sh
docker login ghcr.io --username YOUR_GITHUB_USERNAME
docker pull ghcr.io/dasfunfzigste/gdeploy:0.6.0
```

Replace `YOUR_GITHUB_USERNAME` with your GitHub username. Paste the token at Docker's password prompt; do not place it directly in a shell command. The included Compose file pins `ghcr.io/dasfunfzigste/gdeploy:0.6.0`. The release page also records the registry digest for that exact build.

### Alternative: load the image from a release asset

If you prefer downloading the image from GitHub instead of authenticating to GHCR, download the image archive using the same repository access:

```sh
GDEPLOY_VERSION=0.6.0
cd "$HOME/gdeploy"
gh release download "v${GDEPLOY_VERSION}" \
  --repo DasFunfZigste/GDeploy \
  --pattern "gdeploy-${GDEPLOY_VERSION}-linux-amd64.image.tar.gz" \
  --dir downloads

cd downloads
sha256sum --check --ignore-missing SHA256SUMS
cd ..
docker load --input "downloads/gdeploy-${GDEPLOY_VERSION}-linux-amd64.image.tar.gz"
```

This restores the same versioned image locally. Use `docker compose up -d --wait --pull never` in section 5; the bootstrap and `docker run` commands also use the loaded image. Loading the image avoids a registry pull, but VM installation still needs the Ubuntu and Elastic package networks described in section 6.

## 3. Prepare an OS ISO and optional Splunk installer

On the Ubuntu server, working in `~/gdeploy`:

1. Obtain the supported **Ubuntu Server 24.04 LTS amd64 live-server ISO** from [Ubuntu's official release directory](https://releases.ubuntu.com/24.04/), or use your existing copy on an ESXi datastore. Verify the publisher checksum using Ubuntu's [verification instructions](https://ubuntu.com/tutorials/how-to-verify-ubuntu). Keep the verified SHA-256 value for Setup.
2. Choose an existing ESXi ISO through Setup's datastore browser, upload it from your computer after sign-in, or copy it into the GDeploy server's **`media/`** directory for the server picker. An `.iso` filename such as `os.iso` is suitable; no specific name is required for the picker.
3. To deploy Splunk, download your licensed **Splunk Enterprise Linux x86_64 `.tgz`** from [Splunk](https://www.splunk.com/en_us/download/splunk-enterprise.html), verify its vendor checksum, and copy it to **`media/splunk.tgz`**. GDeploy does not provide Splunk installation media or a license.

For example, replace these source paths with the files you downloaded:

```sh
cd "$HOME/gdeploy"
mkdir -p media
# Only if using the server picker instead of browser upload:
cp /path/to/downloaded-live-server-amd64.iso media/os.iso
# Only if deploying Splunk:
cp /path/to/splunk-linux-amd64.tgz media/splunk.tgz

chmod 0755 media
# Only if the ISO was copied to the server:
chmod 0644 media/os.iso
# Only if the Splunk file is present:
chmod 0644 media/splunk.tgz
```

The app container runs as UID 10001 and needs read access to server-mounted media. Equivalent ACLs are also suitable. The server directory is mounted read-only at `/media`; browser uploads and copies imported from ESXi are saved separately in `/data/media` within the persistent data volume. Keep passwords and private keys out of `media/`. Allow local space for each retained ISO plus several GiB for temporary media preparation, and enough storage on ESXi for the selected VMs.

For Splunk, enter the publisher-verified SHA-256 in the optional `.env` as `GDEPLOY_SPLUNK_SHA256`. Preserve existing contents and use one value per setting. If the app is already running, apply this environment change with `docker compose up -d --force-recreate --wait`. An OS ISO selected through Setup does not need an environment setting.

## 4. Choose automatic or manual credentials

**Automatic setup:** skip the configuration script and continue to section 5. GDeploy creates the initial `admin`/`admin` account and a unique random encryption key on its first start with a fresh data volume. Select the OS ISO through Setup after signing in. Leave both administrator credential settings absent or empty. If using Splunk, supply the verified package and its environment checksum as described in section 3. Preserve existing settings and restrict `.env` to its owner if it contains private settings.

**Optional manual setup:** if you prefer credentials in host environment files, run the following script **before the first start on a fresh volume**. It uses the same initial `admin`/`admin` account, creates a unique random encryption key, and records the checksums of the media already present. The first sign-in still requires replacing the default credentials. Do not run it after automatic setup, against existing app data, or to recover a missing key.

Run the bundled configuration script inside the downloaded image:

```sh
cd "$HOME/gdeploy"
docker run --rm --pull never \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD,target=/setup" \
  ghcr.io/dasfunfzigste/gdeploy:0.6.0 \
  python /app/scripts/configure.py --directory /setup
```

This creates:

- **`.env`** for Docker Compose, including the unique encryption key, initial administrator password hash and checksums of the installers already present.
- **`docker.env`** for the optional plain `docker run` command.
- **`bootstrap-credentials.txt`**, recording the initial `admin`/`admin` login and the required account setup.

These files are readable only by their owner. Sign in with `admin`/`admin` after startup, complete account setup, and store your chosen credentials in your password manager. The bootstrap credentials file remains an initial-login record; it is not updated with your chosen password. Back up `.env` and the data volume together: **the encryption key is required to read saved ESXi and VM credentials**. Do not regenerate it during an upgrade. The script refuses to overwrite an existing configuration.

The optional script records checksums for its expected `media/ubuntu.iso` and `media/splunk.tgz` paths if present. You can instead choose or replace the OS ISO in Setup at any time without editing environment files. Verify replacement Splunk packages against the publisher and update `GDEPLOY_SPLUNK_SHA256` in your active environment file before recreating the container.

## 5. Start GDeploy with Docker Compose

```sh
cd "$HOME/gdeploy"
docker compose up -d --wait --pull never
docker compose ps
docker compose logs --tail=100 gdeploy
curl --fail http://127.0.0.1:8000/api/health
```

Wait for the container to become **healthy**. The app restarts automatically when Docker starts. Docker publishes it on **`0.0.0.0:8000`** by default, making it available through the server's LAN IPv4 address.

On a fresh installation, sign in with **username `admin` and password `admin`**, then complete the required account setup near the top of this guide. Existing installations use their current credentials. Automatic installations save their encryption key and initial administrator password hash in **`/data/bootstrap.json`** with owner-only permissions. The chosen account is stored separately in the database; back up the complete data volume, including both. Losing the original encryption key prevents decryption of saved ESXi and VM credentials.

From another computer, open **`http://SERVER_LAN_IP:8000`** using the Ubuntu server's actual LAN address. On the Docker host, open [http://localhost:8000](http://localhost:8000).

An **optional SSH tunnel** is useful if you have restricted the bind address to `127.0.0.1`. Open a terminal on your own computer and keep this running:

```sh
ssh -N -L 8000:127.0.0.1:8000 YOUR_UBUNTU_USER@YOUR_SERVER_ADDRESS
```

When using the tunnel, open [http://localhost:8000](http://localhost:8000) on your computer and sign in as described above. If port 8000 is already in use locally, use `-L 8001:127.0.0.1:8000` and browse to `http://localhost:8001` instead.

LAN access works with the default binding. For HTTPS access, put the app behind a reverse proxy and keep its port private to the proxy. Set `GDEPLOY_COOKIE_SECURE=true` and `FORWARDED_ALLOW_IPS` to the proxy address/network as seen by the container, and forward the original `Host` and `X-Forwarded-Proto` headers. Recreate the container after environment changes. Docker-published ports can bypass UFW rules, so do not assume UFW alone protects a port published on every interface.

## 6. Complete Setup and deploy your first VMs

### Trust the certificate and connect

After completing first-sign-in account setup:

1. Open **Setup**, select the **ESXi connection** tab, and enter the standalone ESXi 8.0 Update 3 hostname or IP address.
2. Retrieve its certificate. The screen shows the **subject, issuer, SHA-256 fingerprint, validity dates and DNS/IP names** (subject alternative names).
3. Compare the SHA-256 fingerprint with the certificate shown through a trusted ESXi management session or another independently trusted source. Retrieving a certificate alone does not establish the host's identity. Select **Trust certificate** once you have verified it.
4. Enter and save the ESXi username/password, then test the connection to load inventory.

The approval applies to the exact entered host/IP on HTTPS **443** and is saved in GDeploy's database. It remains available after restarting or replacing the container with the same data volume. This workflow needs **no CA file, `.env` edit or container restart**. If your ESXi certificate already validates with the system or configured private CA, you can save and test the connection without adding a certificate approval.

Each new ESXi API or datastore file-transfer connection verifies the approved certificate exactly and checks its validity dates. This includes downloading an ISO through Setup. Explicit approval supports a hostname or IP that is absent from the certificate's DNS/IP names. An expired, not-yet-valid, changed or renewed certificate blocks the connection; retrieve and review a replacement, then trust it before retrying. Approval for one address does not automatically cover another alias of the same server.

You can remove the saved trust from **Setup → ESXi connection** to restore normal system/private CA verification. Trust changes apply to subsequent connections, including connections for existing deployments to that saved endpoint; an already-open connection may finish using the certificate it authenticated earlier.

The settings API requires `verify_tls=true`. Connections also verify TLS when older saved settings or deployment snapshots contain `verify_tls=false`. If an earlier setup relied on disabling verification, approve its certificate here or configure a trusted CA before connecting. This does not rewrite stored passwords or settings.

### Select OS installation media

Open **Setup** in the main navigation, then select the **OS installation media** tab at the top. If an ESXi host is configured and no ISO is ready, Setup opens this tab automatically. You can also go directly to `http://SERVER_LAN_IP:8000/#settings/media`, replacing the server address and port as needed.

Choose the source that matches where your ISO is stored:

| Source | What to do | Where GDeploy uses it |
| --- | --- | --- |
| **ESXi datastore** | Select a datastore on the saved ESXi host, open its folders, then select an existing ISO. | GDeploy downloads and verifies a retained local copy in `/data/media`. |
| **Upload an ISO** | Select the ISO on your computer. | GDeploy uploads and verifies a retained copy in `/data/media`. |
| **GDeploy server** | Select a `.iso` in `/media`, or a previously uploaded/imported copy. | Server-mounted files use the read-only `media/` directory beside the Compose file; saved copies use `/data/media`. |

Enter the **SHA-256 checksum from the publisher's verified checksum list**, then select **Copy & use ISO** for ESXi, **Upload & use ISO** for a browser upload, or **Use selected ISO** for GDeploy server media. Computing a hash from an unverified download alone does not establish its source. GDeploy checks the bytes against that expected hash before saving. Keep the page open while copying and verification finish; large ISOs can take several minutes. Browser-upload progress and cancellation remain available. Check the saved media details before deploying. An import or validation error appears in Setup and keeps the previous media selection.

For **ESXi datastore**, first save the host credentials and establish certificate trust in the ESXi connection tab. Browsing and downloading use those same credentials and verified HTTPS connection. The ESXi account needs **`Datastore.Browse`** and file download access. If you change the saved ESXi host while browsing, refresh the browser listing and select the ISO again; GDeploy blocks stale selections from the former host.

ESXi imports and browser uploads accept ISOs up to **16 GiB**. The GDeploy data volume needs enough free space for the complete copy, plus temporary remastering space when deploying. GDeploy prepares a separate unattended-install ISO from the local copy. The original ESXi ISO is never modified or deleted, and a completed copy remains reusable after restarting GDeploy. Its origin host, datastore and path are retained with the media details.

The current unattended installer supports **Ubuntu Server 24.04 LTS amd64 live-server** media. A matching checksum verifies the file's integrity; it does not make an unsupported installer compatible. Uploaded/imported copies persist under `/data/media`, so back them up with the complete data volume. Server-mounted ISOs remain in your `media/` directory. Changing the selection needs no container restart.

Queued and running deployments retain the local ISO path and checksum selected when they were created. Keep those local files present and unchanged until the jobs finish; a later Setup selection applies to future jobs. After an ESXi import completes, changing or removing the original datastore ISO does not change the saved local copy. **Delete & redeploy** creates a new job with the current media selection.

Existing environment-configured media remains available when there is no saved Setup selection. Preferred names are `GDEPLOY_OS_ISO` and `GDEPLOY_OS_SHA256`; legacy `GDEPLOY_UBUNTU_ISO` and `GDEPLOY_UBUNTU_SHA256` continue to work. For example, a manual configuration can use:

```dotenv
GDEPLOY_OS_ISO=/media/os.iso
GDEPLOY_OS_SHA256=YOUR_PUBLISHER_VERIFIED_SHA256
```

Use a real 64-character SHA-256 value and recreate the container to apply environment changes. A saved Setup choice takes precedence over environment media settings. The compatible default path remains `/media/ubuntu.iso` when no ISO path is configured.

### Manage storage and saved ISOs

In **Setup → OS installation media**, open **Storage & saved ISOs** and select **Refresh storage** for current measurements. **GDeploy data filesystem** shows used, available and total space for the filesystem backing the data directory inside the container, normally `/data`. **Saved ISO files** and **Deployment workspace** show GDeploy's retained copies and temporary work separately. Filesystem usage can include other data sharing that filesystem; it is not the sum of those two categories or an inventory of every disk on the Docker host.

The default named volume has **no GDeploy-imposed capacity limit**. It uses available space on its backing filesystem, subject to any host/filesystem quota or Docker virtual-disk limit. GDeploy does not automatically expand host disks. Each uploaded/imported ISO still has a **16 GiB file-size limit**. The separate `/tmp` mount is a **256 MiB memory filesystem**; as of 0.5.0, installation-media subprocesses use a private workspace under `/data/artifacts` for temporary files.

Check the same storage from the installation directory:

```sh
docker compose exec gdeploy df -h /data /tmp
```

For a plain Docker installation, use `docker exec gdeploy df -h /data /tmp`. If you deliberately changed `GDEPLOY_DATA_DIR`, inspect that path instead of `/data` and ensure it is backed by persistent writable storage.

To reclaim space:

1. Find an unused upload or ESXi-imported copy under **Storage & saved ISOs**. Its original source is shown so you can identify the right file. If it is the current choice, use **Clear saved selection** in the saved ISO summary, or choose another ISO.
2. Select **Delete**, review **Delete saved ISO?**, then confirm **Delete ISO**. This permanently removes only GDeploy's local saved copy. To use it again, restore a backup or upload/import it again.
3. If deletion is disabled, follow the displayed reason. Clear the saved selection or choose another OS ISO if this is still the current default. Copies referenced by **queued, running or cleaning deployments** remain protected until that work is resolved, even after clearing the default.

**Clear saved selection** removes only the saved default, without deleting any file or changing active deployment snapshots. Existing environment-configured ISO settings become the fallback. If there is no valid fallback, select an ISO before starting another deployment. You can therefore clear and delete your only unused uploaded/imported copy without needing space to upload a replacement first. Clearing alone does not reclaim storage; the separate **Delete ISO** action does.

The storage controls cannot delete files from the read-only `/media` server mount or original ISOs on ESXi. Deleting a local copy does not delete VMs, credentials or deployment history. Temporary deployment files are managed by the worker; there is no manual workspace-delete control for active work.

If available space is still insufficient, arrange more capacity on the filesystem backing the Docker data volume, or migrate the complete volume to larger persistent storage using your Docker host's storage procedure. Docker Desktop may also have a separate virtual-disk size limit. Back up the stopped app's complete volume and encryption configuration first, preserve ownership for UID/GID 10001, and reuse the existing data after migration. Changing an environment path or creating an empty volume does not expand the original storage or preserve its contents. Keep space for both retained ISOs and each deployment's generated installation media.

### Deploy your VMs

1. Open **Setup**, listed above **Deployments** in the navigation, to finish ESXi and OS ISO configuration. Select **New deployment**, enter a deployment label and choose OS only, Splunk, Elasticsearch and/or Kibana. Each chosen role receives a separate VM. Kibana requires Elasticsearch and is configured to connect to it; Splunk runs independently.
2. In **Configure VMs**, fill in each role's visible section with its own VM name, CPU, RAM, disk size, datastore, port group and DHCP/static network settings. Elasticsearch and Kibana have separate sections and VM names. The deployment label groups the job; it is not a shared VM name. Use the section navigation to move between forms; all selected forms stay visible together. Review the separate values for every VM before continuing.
3. Run preflight, address any reported issues, and start deployment.
4. Open the deployment to follow its stages and errors. In **Deployment logs**, select **View logs** to expand recorded events and sanitized diagnostic details, and **Copy logs** to copy them. The failure banner's **View deployment logs** opens the same view. When ready, use the application links and **Reveal credentials** panel for the VM passwords and application sign-in details.

On an HTTP LAN address where automatic clipboard access is unavailable, **Copy logs** selects the text for manual copying. Older failures retain only the text originally recorded; upgrading cannot recover discarded tool output. New media-preparation errors include captured diagnostics where available, without exposing generated credentials or the autoinstall secret data.

The OS username is **`gdeploy`** with a different generated password per VM. The web-app administrator password, guest OS passwords and application passwords are separate. The deployment's Credentials panel is the ongoing place to find guest and application details.

The Docker host needs access to ESXi HTTPS **443** and guest SSH **22**. The guests need working DNS and outbound access to Ubuntu mirrors and the official Elastic package repository. Kibana needs access to Elasticsearch HTTPS **9200**; your browser needs access to Kibana **5601** or Splunk Web **8000**. GDeploy does not configure your firewall or switches. Prefer static IPs or DHCP reservations for application VMs because Kibana's configuration and Elasticsearch's certificate use Elasticsearch's assigned address.

The ESXi account and license must permit vSphere API provisioning, datastore uploads and creation/deletion of the deployment's resources. Existing datastore media additionally needs browse and download permission. Inventory access alone does not prove those permissions. Certificate verification uses your approved certificate for that exact endpoint, or normal CA and hostname verification when no certificate is approved.

### Optional: configure a private CA manually

To use CA verification instead of approving an individual certificate in the app, place a PEM trust bundle containing the normal public roots plus your trusted ESXi CA at `media/esxi-ca-bundle.pem`, with read access for UID 10001. Add these values to `.env` for Compose, or `docker.env` for plain Docker, then recreate the container:

```dotenv
SSL_CERT_FILE=/media/esxi-ca-bundle.pem
REQUESTS_CA_BUNDLE=/media/esxi-ca-bundle.pem
```

Use public certificates in this trust bundle; it does not require CA private keys. With this CA-based method, the ESXi hostname must match the certificate. Remove any saved certificate approval for that endpoint if you want these CA rules to determine trust.

For datastore downloads/uploads using CA verification, `REQUESTS_CA_BUNDLE` is honored explicitly. Proxy environment settings remain disabled; a configured CA bundle does not enable an HTTP/HTTPS proxy.

If a deployment fails, inspect its errors and the ESXi task/console state. **Delete & redeploy** requires the deployment name as confirmation and permanently deletes the VMs and disks owned by that deployment before trying again. It creates fresh credentials. Application data on those disks is also deleted.

### Manage deployment history

Select **Hide** beside a finished record in **Deployment history**, or **Hide from history** on its detail page. The record disappears from the default list. Enable **Show hidden** to include hidden records with a **Hidden** badge, then select **Restore** in the row or **Restore to history** on the detail page. Hidden records remain accessible through their direct links, and their visibility setting survives container restarts and rebuilds using the same data volume. Queued, running and cleaning jobs cannot be hidden.

Hiding changes only history visibility. It preserves VMs and their disks, the original status, logs, credentials, ownership and resource reservations. For example, a healthy OS-only VM can be kept while its failed readiness-check record is hidden. Hiding does not mark that job successful, retry its checks, finish provisioning or detach/delete its installation media. This release does not change the cloud-init readiness check. Use the deployment logs and ESXi console to assess unfinished work; **Delete & redeploy** is for deliberately replacing the deployment's VMs.

This is a lab release. Automated tests and a healthy app container do not validate a full unattended installation against your ESXi host. Complete the repository's [lab acceptance checklist](https://github.com/DasFunfZigste/GDeploy/blob/v0.6.0/docs/LAB_VALIDATION.md) before relying on it for workloads.

## 7. Existing Docker: run without Compose

After sections 2–4, use this **instead of** `docker compose up`. Do not start two GDeploy containers against the same data volume. The command below uses the **optional manual setup** and its `docker.env` from section 4. For automatic setup on a fresh volume, replace the `--env-file docker.env` line with **`--env GDEPLOY_COOKIE_SECURE=false`** for this direct HTTP setup; add a media-only environment file later if needed. Plain Docker defaults to secure cookies, so this explicit setting is required for HTTP sign-in. For an HTTPS reverse proxy, use `GDEPLOY_COOKIE_SECURE=true` and the proxy settings in section 5 instead. Preserve the original environment credentials when reusing a manually configured volume.

The plain Docker example publishes **`0.0.0.0:8000`** for LAN access by default. To restrict it, replace that address in `--publish` with a specific host LAN IPv4 address or `127.0.0.1`. When binding to one LAN address, use that address in the health-check URL too. The Compose `GDEPLOY_BIND_IP` setting does not change a plain `docker run` port mapping.

Run from `~/gdeploy`:

```sh
cd "$HOME/gdeploy"
docker volume create gdeploy-data
docker run -d --name gdeploy \
  --pull never \
  --restart unless-stopped \
  --init \
  --env-file docker.env \
  --publish 0.0.0.0:8000:8000 \
  --mount source=gdeploy-data,target=/data \
  --mount "type=bind,source=$PWD/media,target=/media,readonly" \
  --read-only \
  --tmpfs /tmp:rw,size=256m,mode=1777 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --stop-timeout 30 \
  ghcr.io/dasfunfzigste/gdeploy:0.6.0

docker ps --filter name=gdeploy
docker logs --tail=100 gdeploy
curl --fail --retry 30 --retry-connrefused --retry-delay 2 --retry-max-time 60 \
  http://127.0.0.1:8000/api/health
```

The health request retries while the app starts. Wait for it to succeed before signing in; if it fails, inspect the container logs first. Fresh automatic and manual installations both start with `admin`/`admin` and require account setup. For an older automatic installation, its initial login can be retrieved with `docker exec gdeploy cat /data/bootstrap-credentials.txt`; older manual setup uses the host-side file. After account setup, use the credentials you chose instead. Continue with the browser/SSH instructions in section 5 and ESXi setup in section 6. The image already declares UID/GID 10001 and its health check. The named volume keeps the database and encrypted credentials when the container is replaced.

Use **`docker.env`**, not `.env`, with `docker run --env-file`: Docker's CLI retains literal quotes, whereas Compose removes them. The generated administrator password hash contains dollar signs; keep the generator's single quotes in the Compose `.env`, and the unquoted values in `docker.env`. Never source these files as shell scripts. When editing configuration, update the file used by your selected launch method; keep both in sync if switching methods.

For a manually configured installation, refresh `docker.env` from its existing complete `.env` without regenerating credentials by running:

```sh
docker run --rm --pull never \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD,target=/setup" \
  ghcr.io/dasfunfzigste/gdeploy:0.6.0 \
  python /app/scripts/configure.py --directory /setup --export-docker-env
```

The export command expects the manual administrator credential pair in `.env`. For an automatic installation, use an optional `docker.env` containing only your media hashes and other settings as plain `KEY=value` lines; keep the administrator key/hash absent so the saved bootstrap encryption key and database account remain in use. Include `GDEPLOY_COOKIE_SECURE=false` for direct HTTP, or `true` behind HTTPS, and keep any command-line cookie setting consistent with it.

## 8. Check, stop, upgrade and recover

For a Compose installation:

```sh
cd "$HOME/gdeploy"
docker compose ps
docker compose logs --tail=200 gdeploy
docker compose stop
docker compose start
```

For plain Docker, use `docker inspect gdeploy`, `docker logs gdeploy`, `docker stop gdeploy` and `docker start gdeploy` instead. Do not paste environment files or revealed credentials into support logs.

Common startup issues:

| Symptom | What to check |
| --- | --- |
| GitHub release is missing or returns 404 | Sign in with an account that has access to the private repository; verify the release version. |
| GHCR returns `denied` or `unauthorized` | Log in to GHCR using an account with package access and a classic PAT with `read:packages`, or load the release image archive. |
| Docker reports permission denied | Reconnect after adding your user to the Docker group, or consistently use `sudo docker`. |
| Container exits or remains unhealthy | Read `docker compose logs`; verify Compose is at least 2.24 and any explicit credential settings are complete. Preserve the volume and restore original missing/damaged keys from backup. |
| `admin`/`admin` no longer signs in | After account setup, use the username and password you chose. Existing older installations retain their original credentials rather than adopting the new default. |
| Cannot find the OS ISO picker | Open Setup, then the OS installation media tab, or use `/#settings/media`. If the tab is absent, rebuild/restart the app from the updated source and refresh the browser. Check the app footer shows v0.3.1 or later. |
| ESXi datastore source is absent or cannot load | Check the app footer shows v0.4.0 or later, save and test the ESXi connection, and confirm certificate trust, `Datastore.Browse` and file download access. Refresh the listing after changing hosts. |
| ESXi ISO import fails | Check the selected file still exists, its publisher checksum matches, its size is at most 16 GiB, and the GDeploy data volume has room for the copy. Review the displayed TLS, permission or transfer error before retrying. The original datastore file and previous saved selection remain intact. |
| Media verification or preflight fails | Check the selected ISO in Setup, its publisher checksum, available disk space and UID 10001's read access to server files. Keep media used by queued/running jobs present and unchanged. |
| Media preparation reports permission denied or only a generic ISO error | Update to v0.5.0 or later, then open the deployment's **View logs**. These releases fix read-only extracted GRUB/manifest working files and use `/data/artifacts` for subprocess temporary files. Check the new diagnostic for the failing path/tool, plus `/data` free space and permissions. An older generic error alone does not establish which condition failed. |
| Data storage is full or appears smaller than the host disk | Check **Storage & saved ISOs** and run `docker compose exec gdeploy df -h /data /tmp`. The app sees the backing data filesystem, not all host disks. Delete eligible unused ISO copies or expand/migrate that backing storage; `/tmp` is a separate 256 MiB mount. |
| A saved ISO cannot be deleted | Use **Clear saved selection** or select another ISO if this is the current choice. Resolve any queued, running or cleaning deployment using it; clearing the default does not remove that protection. Only uploaded/ESXi-imported local copies can be deleted in Setup; server-mounted files and original ESXi ISOs are outside this control. |
| ISO upload is rejected or interrupted | Browser uploads are limited to 16 GiB. Check the data volume's free space and, if using a reverse proxy, its request-size and upload-timeout settings. The prior media selection remains saved. |
| ESXi connection fails | Check the hostname, port 443, credentials and API license/permissions. For certificate errors, retrieve the certificate in Setup → ESXi connection and verify its fingerprint and dates. A renewed certificate requires a new approval. |
| A finished deployment is missing from history | Enable **Show hidden** and check the search/status filters. Select **Restore** to return the record to the default list. Hiding retains the VM, credentials and logs. |
| Remote browser cannot connect | Check `docker compose port gdeploy 8000` and the server address/port in your URL. Existing `.env` overrides remain in effect; a `127.0.0.1` override accepts only local connections. Edit that value to `0.0.0.0` or a LAN address and recreate the container, or use the optional SSH tunnel. |

Version **0.6.0** adds persistent, reversible history visibility and displays every selected VM's independent configuration together. **Setup** appears first in navigation. Accounts, credentials, logs, media and resource ownership remain intact. The upgrade adds a database column; older versions may read the records but cannot create deployments against the migrated schema. Back up the complete data volume before upgrading and restore the matching pre-upgrade backup if rolling back. Inspect ESXi state before resuming work because restoring GDeploy's backup does not undo VM changes. Hiding a failed record does not resolve the failure or change the running VM. The cloud-init readiness check and deployment specification format are unchanged.

Version **0.5.0** adds expanded deployment diagnostics, data-filesystem usage, **Clear saved selection** and protected deletion of unused saved ISO copies, and fixes private installation-media work files that retain read-only permissions. Upgrading keeps accounts, encryption configuration, trust approvals, media selections and history. It does not enlarge or replace the data volume. A deleted ISO copy is not restored by rolling the app back; restore it from backup or import/upload it again if needed. Existing generic errors remain generic because their discarded output cannot be recovered.

Version **0.4.0** adds ESXi datastore media browsing and import. Saved credentials, certificate approvals and existing uploaded/mounted media keep working. Include imported copies in complete data-volume backups. Versions before **0.4.0** do not understand media selections with an ESXi origin; finish or resolve active jobs and select a verified uploaded or server-mounted ISO before rolling back. Preserve the data volume and existing environment keys.

Version **0.3.1** adds explicit Setup tabs, versioned browser assets and a visible release number. After updating the container, refresh the page and check the footer version. The database format and saved configuration are unchanged from 0.3.0.

Version **0.3.0** adds saved OS ISO selection and persistent browser uploads. Existing environment media settings remain a fallback until a selection is saved in Setup. The new `GDEPLOY_OS_ISO` and `GDEPLOY_OS_SHA256` names take precedence over their legacy `GDEPLOY_UBUNTU_*` equivalents when both are supplied. Include uploaded ISOs in the complete data-volume backup and preserve server-mounted `media/` files. Versions before **0.3.0** ignore Setup's media selection and the new environment names; supply the older version's verified media path/checksum settings before a rollback. Finish or resolve active jobs before changing versions.

Version **0.2.0** adds an ESXi certificate-trust table without changing existing credentials or encryption keys. Back up the complete data volume to preserve approved certificates. If rolling back below **0.2.0**, older versions ignore these approvals and use their previous TLS settings, including any saved `verify_tls=false`. Review those settings and ensure certificate verification is enabled and trusts the intended host before using the older version. The approvals do not become CA certificates in older releases.

Version **0.1.3** adds a persistent administrator account record to the existing database. On the first upgraded start, the record is initialized once from the existing credentials; later starts keep the database account. Existing random/custom credentials are preserved, and completed account setup is not overwritten by bootstrap or environment values.

Before upgrading, finish or resolve active deployments, read the new release's changelog and compatibility notes, then stop the app. Take a consistent backup of the **complete data volume**, including **`/data/bootstrap.json` for automatic installations**, and any existing **`.env`/`docker.env` with its matching encryption key**, plus your Compose configuration and media. Keep the backup somewhere outside the live volume.

Download and verify the next release's bundle in a separate download directory, then update the files in the **same installation directory**. Keep your existing secret files, saved bootstrap files, source media, Compose project and data volume. Do not rerun the initial credential generator. Pull the new versioned image, review any required configuration changes, then run `docker compose up -d --wait --pull never` and check health and history. If using an image archive, load the new version before starting. Avoid an unpinned `latest` tag for upgrades.

**Moving from the original source-build setup:** its Compose volume may be named `gdeploy_gdeploy-data`, while the release bundle defaults to `gdeploy-data`. Before replacing your Compose file, inspect the current container's mounts to find the volume attached to `/data`. Preserve any existing `.env` and add `GDEPLOY_DATA_VOLUME=YOUR_EXISTING_VOLUME_NAME` to it so the release Compose file reuses that volume; for an automatic installation, create the optional `.env` with only this setting if it does not exist. Preserve the media directory too. For manual credentials, export `docker.env` using the command in section 7 only if you need plain Docker. For plain Docker, use that same existing volume name in `--mount source=...,target=/data`. A newly created empty volume will not contain your previous deployment history.

For plain Docker, pull/load the new image, stop and remove only the old `gdeploy` container, then repeat section 7 with the new image version, the same volume and any existing `docker.env`. Automatic installations must keep their saved `bootstrap.json` in that volume; manually configured installations must keep their original environment credentials.

Do not blindly roll back below **0.1.3** after choosing your account: older versions ignore the database administrator record and use the old bootstrap/environment login, which may be `admin`/`admin`. Restore a matching backup and review the login configuration before starting an older version. Automatic bootstrap was introduced in version **0.1.2**; earlier versions also require explicit environment credentials. Rollback is release-dependent: if an upgrade changes stored data incompatibly, changing the image tag alone is insufficient. Restore the matching backup and encryption key according to that release's notes. Restoring GDeploy's database does **not** undo ESXi VM changes or restore guest application data; inspect ESXi state before resuming work.

Do not run `docker compose down -v` or remove `gdeploy-data` unless you intend to erase GDeploy's saved history and credentials. Run one app container with one worker per data volume.
