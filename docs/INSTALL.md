# Install GDeploy

This guide installs **GDeploy 0.1.3** on an Ubuntu Server **22.04 or 24.04 LTS, amd64/x86_64** host. Use the source quick start below, or download the prebuilt image `ghcr.io/dasfunfzigste/gdeploy:0.1.3` using the numbered walkthrough. Both methods require Docker Engine; Compose examples require **Docker Compose 2.24 or later**. GDeploy provisions **Ubuntu Server 24.04 LTS amd64** guests on standalone **ESXi 8.0 Update 3**.

Find each version, its changes, image digest and downloadable files on the [GitHub Releases page](https://github.com/DasFunfZigste/GDeploy/releases). The repository and its package are private, so sign in with a GitHub account that has access.

## Quick start: clone and run

With Git, Docker Engine and Docker Compose 2.24 or later already installed:

```sh
git clone https://github.com/DasFunfZigste/GDeploy.git
cd GDeploy
docker compose up --build -d --wait
```

Use your existing GitHub access for the private repository. This source build needs no GHCR login, host Python installation or initial `.env` file. On a fresh data volume, GDeploy creates the initial `admin`/`admin` account and a unique random encryption key automatically. The clone follows the default branch; use the prebuilt walkthrough for the specific release shown on this page.

Open [http://localhost:8000](http://localhost:8000) and sign in with **username `admin` and password `admin`** on a fresh installation. Complete the required account setup described below. For a remote server, run this on your own computer and leave it open:

```sh
ssh -N -L 8000:127.0.0.1:8000 YOUR_UBUNTU_USER@YOUR_SERVER_ADDRESS
```

Then browse to `http://localhost:8000` on your computer. If Docker is not installed yet, complete section 1 and return here.

The UI can start before installation media is ready. Before deploying VMs, place your vendor-verified Ubuntu ISO at `media/ubuntu.iso` in the clone, and optional licensed Splunk package at `media/splunk.tgz`. Section 3 explains the downloads and permissions. Calculate `sha256sum media/ubuntu.iso` (and the Splunk file if used), then create or edit the optional `.env` with `GDEPLOY_UBUNTU_SHA256` and `GDEPLOY_SPLUNK_SHA256` set to the verified values. Preserve existing settings and use one value per key. Automatic installations leave `GDEPLOY_SECRET_KEY` and `GDEPLOY_ADMIN_PASSWORD_HASH` absent or empty, so the saved encryption key and database account remain in use. Apply the media settings with `docker compose up --build -d --wait --force-recreate`.

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
GDEPLOY_VERSION=0.1.3
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
docker pull ghcr.io/dasfunfzigste/gdeploy:0.1.3
```

Replace `YOUR_GITHUB_USERNAME` with your GitHub username. Paste the token at Docker's password prompt; do not place it directly in a shell command. The included Compose file pins `ghcr.io/dasfunfzigste/gdeploy:0.1.3`. The release page also records the registry digest for that exact build.

### Alternative: load the image from a release asset

If you prefer downloading the image from GitHub instead of authenticating to GHCR, download the image archive using the same repository access:

```sh
GDEPLOY_VERSION=0.1.3
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

## 3. Add the Ubuntu and optional Splunk installers

On the Ubuntu server, working in `~/gdeploy`:

1. Download the **Ubuntu Server 24.04 LTS amd64 live-server ISO** from [Ubuntu's official release directory](https://releases.ubuntu.com/24.04/). Verify it using Ubuntu's published checksums and [verification instructions](https://ubuntu.com/tutorials/how-to-verify-ubuntu).
2. Copy the verified ISO to **`media/ubuntu.iso`**.
3. To deploy Splunk, download your licensed **Splunk Enterprise Linux x86_64 `.tgz`** from [Splunk](https://www.splunk.com/en_us/download/splunk-enterprise.html), verify its vendor checksum, and copy it to **`media/splunk.tgz`**. GDeploy does not provide Splunk installation media or a license.

For example, replace these source paths with the files you downloaded:

```sh
cd "$HOME/gdeploy"
mkdir -p media
cp /path/to/ubuntu-24.04-live-server-amd64.iso media/ubuntu.iso
# Only if deploying Splunk:
cp /path/to/splunk-linux-amd64.tgz media/splunk.tgz

chmod 0755 media
chmod 0644 media/ubuntu.iso
# Only if the Splunk file is present:
chmod 0644 media/splunk.tgz
```

The app container runs as UID 10001 and needs read access to the directory and installers. Equivalent ACLs are also suitable. Keep passwords and private keys out of `media/`. Allow several GiB of free local disk beyond the source ISO for temporary media preparation, plus enough storage on ESXi for the selected VMs.

## 4. Choose automatic or manual credentials

**Automatic setup:** skip the configuration script and continue to section 5. GDeploy creates the initial `admin`/`admin` account and a unique random encryption key on its first start with a fresh data volume. For VM provisioning, create or edit the optional `.env` to set `GDEPLOY_UBUNTU_SHA256` and, if used, `GDEPLOY_SPLUNK_SHA256` to your publisher-verified media hashes. Use `sha256sum media/ubuntu.iso` or `sha256sum media/splunk.tgz` to calculate them. Preserve existing settings; leave both administrator credential settings absent or empty. You may add these media settings after first start and recreate the container. Restrict `.env` to its owner if it contains private settings.

**Optional manual setup:** if you prefer credentials in host environment files, run the following script **before the first start on a fresh volume**. It uses the same initial `admin`/`admin` account, creates a unique random encryption key, and records the checksums of the media already present. The first sign-in still requires replacing the default credentials. Do not run it after automatic setup, against existing app data, or to recover a missing key.

Run the bundled configuration script inside the downloaded image:

```sh
cd "$HOME/gdeploy"
docker run --rm --pull never \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD,target=/setup" \
  ghcr.io/dasfunfzigste/gdeploy:0.1.3 \
  python /app/scripts/configure.py --directory /setup
```

This creates:

- **`.env`** for Docker Compose, including the unique encryption key, initial administrator password hash and checksums of the installers already present.
- **`docker.env`** for the optional plain `docker run` command.
- **`bootstrap-credentials.txt`**, recording the initial `admin`/`admin` login and the required account setup.

These files are readable only by their owner. Sign in with `admin`/`admin` after startup, complete account setup, and store your chosen credentials in your password manager. The bootstrap credentials file remains an initial-login record; it is not updated with your chosen password. Back up `.env` and the data volume together: **the encryption key is required to read saved ESXi and VM credentials**. Do not regenerate it during an upgrade. The script refuses to overwrite an existing configuration.

Run the script after placing your media so it records their checksums. If you add or replace an installer later, verify it against the publisher, calculate `sha256sum media/ubuntu.iso` or `sha256sum media/splunk.tgz`, and update the matching `GDEPLOY_UBUNTU_SHA256` or `GDEPLOY_SPLUNK_SHA256` value in your active environment file before recreating the container.

## 5. Start GDeploy with Docker Compose

```sh
cd "$HOME/gdeploy"
docker compose up -d --wait --pull never
docker compose ps
docker compose logs --tail=100 gdeploy
curl --fail http://127.0.0.1:8000/api/health
```

Wait for the container to become **healthy**. The app restarts automatically when Docker starts. It listens on the server's **127.0.0.1:8000** by default.

On a fresh installation, sign in with **username `admin` and password `admin`**, then complete the required account setup near the top of this guide. Existing installations use their current credentials. Automatic installations save their encryption key and initial administrator password hash in **`/data/bootstrap.json`** with owner-only permissions. The chosen account is stored separately in the database; back up the complete data volume, including both. Losing the original encryption key prevents decryption of saved ESXi and VM credentials.

If your browser is on the same machine, open [http://localhost:8000](http://localhost:8000). For a remote Ubuntu server, open a terminal on **your own computer** and keep this SSH tunnel running:

```sh
ssh -N -L 8000:127.0.0.1:8000 YOUR_UBUNTU_USER@YOUR_SERVER_ADDRESS
```

Then open [http://localhost:8000](http://localhost:8000) on your computer and sign in as described above. If port 8000 is already in use locally, use `-L 8001:127.0.0.1:8000` and browse to `http://localhost:8001` instead.

For shared access, put the app behind an HTTPS reverse proxy and keep its port private to the proxy. Set `GDEPLOY_COOKIE_SECURE=true` and `FORWARDED_ALLOW_IPS` to the proxy address/network as seen by the container, and forward the original `Host` and `X-Forwarded-Proto` headers. Recreate the container after environment changes. Docker-published ports can bypass UFW rules, so do not assume UFW alone protects a port published on every interface.

## 6. Connect to ESXi and deploy your first VMs

1. After completing the required first-sign-in account setup, open **ESXi connection** and enter the standalone ESXi 8.0 Update 3 hostname, username and password. Use the connection check to load inventory.
2. Select **New deployment** and choose Ubuntu, Splunk, Elasticsearch and/or Kibana. Each chosen role receives a separate VM. Kibana requires Elasticsearch and is configured to connect to it; Splunk runs independently.
3. Enter each VM's name, CPU, RAM, disk size, datastore, port group and DHCP/static network settings.
4. Run preflight, address any reported issues, and start deployment.
5. Open the deployment to follow its stages and errors. When ready, use its application links and **Reveal credentials** panel for the VM passwords and application sign-in details.

The OS username is **`gdeploy`** with a different generated password per VM. The web-app administrator password, guest OS passwords and application passwords are separate. The deployment's Credentials panel is the ongoing place to find guest and application details.

The Docker host needs access to ESXi HTTPS **443** and guest SSH **22**. The guests need working DNS and outbound access to Ubuntu mirrors and the official Elastic package repository. Kibana needs access to Elasticsearch HTTPS **9200**; your browser needs access to Kibana **5601** or Splunk Web **8000**. GDeploy does not configure your firewall or switches. Prefer static IPs or DHCP reservations for application VMs because Kibana's configuration and Elasticsearch's certificate use Elasticsearch's assigned address.

The ESXi account and license must permit vSphere API provisioning, datastore uploads and creation/deletion of the deployment's resources. Inventory access alone does not prove write permissions. Use the hostname on the ESXi certificate; certificate verification is enabled.

For an ESXi certificate signed by a private CA, place a PEM trust bundle containing the normal public roots plus your trusted ESXi CA at `media/esxi-ca-bundle.pem`, with read access for UID 10001. Add these values to `.env` for Compose, or `docker.env` for plain Docker, then recreate the container:

```dotenv
SSL_CERT_FILE=/media/esxi-ca-bundle.pem
REQUESTS_CA_BUNDLE=/media/esxi-ca-bundle.pem
```

Use public certificates in this trust bundle; it does not require CA private keys. A self-signed certificate must be explicitly trusted and still match the ESXi hostname.

If a deployment fails, inspect its errors and the ESXi task/console state. **Delete & redeploy** requires the deployment name as confirmation and permanently deletes the VMs and disks owned by that deployment before trying again. It creates fresh credentials. Application data on those disks is also deleted.

This is an initial lab release. Automated tests and a healthy app container do not validate a full unattended installation against your ESXi host. Complete the repository's [lab acceptance checklist](https://github.com/DasFunfZigste/GDeploy/blob/v0.1.3/docs/LAB_VALIDATION.md) before relying on it for workloads.

## 7. Existing Docker: run without Compose

After sections 2–4, use this **instead of** `docker compose up`. Do not start two GDeploy containers against the same data volume. The command below uses the **optional manual setup** and its `docker.env` from section 4. For automatic setup on a fresh volume, replace the `--env-file docker.env` line with **`--env GDEPLOY_COOKIE_SECURE=false`** for this loopback HTTP setup; add a media-only environment file later if needed. Plain Docker defaults to secure cookies, so this explicit setting is required for HTTP sign-in. For an HTTPS reverse proxy, use `GDEPLOY_COOKIE_SECURE=true` and the proxy settings in section 5 instead. Preserve the original environment credentials when reusing a manually configured volume.

Run from `~/gdeploy`:

```sh
cd "$HOME/gdeploy"
docker volume create gdeploy-data
docker run -d --name gdeploy \
  --pull never \
  --restart unless-stopped \
  --init \
  --env-file docker.env \
  --publish 127.0.0.1:8000:8000 \
  --mount source=gdeploy-data,target=/data \
  --mount "type=bind,source=$PWD/media,target=/media,readonly" \
  --read-only \
  --tmpfs /tmp:rw,size=256m,mode=1777 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --stop-timeout 30 \
  ghcr.io/dasfunfzigste/gdeploy:0.1.3

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
  ghcr.io/dasfunfzigste/gdeploy:0.1.3 \
  python /app/scripts/configure.py --directory /setup --export-docker-env
```

The export command expects the manual administrator credential pair in `.env`. For an automatic installation, use an optional `docker.env` containing only your media hashes and other settings as plain `KEY=value` lines; keep the administrator key/hash absent so the saved bootstrap encryption key and database account remain in use. Include `GDEPLOY_COOKIE_SECURE=false` for loopback HTTP, or `true` behind HTTPS, and keep any command-line cookie setting consistent with it.

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
| Media preflight fails | Check the exact filenames, recorded checksums and UID 10001's read access. |
| ESXi connection fails | Check the hostname, port 443, certificate trust, credentials and API license/permissions. |
| Remote browser cannot connect | Use the SSH tunnel; the default app port is bound only to the server's loopback interface. |

Version **0.1.3** adds a persistent administrator account record to the existing database. On the first upgraded start, the record is initialized once from the existing credentials; later starts keep the database account. Existing random/custom credentials are preserved, and completed account setup is not overwritten by bootstrap or environment values.

Before upgrading, finish or resolve active deployments, read the new release's changelog and compatibility notes, then stop the app. Take a consistent backup of the **complete data volume**, including **`/data/bootstrap.json` for automatic installations**, and any existing **`.env`/`docker.env` with its matching encryption key**, plus your Compose configuration and media. Keep the backup somewhere outside the live volume.

Download and verify the next release's bundle in a separate download directory, then update the files in the **same installation directory**. Keep your existing secret files, saved bootstrap files, source media, Compose project and data volume. Do not rerun the initial credential generator. Pull the new versioned image, review any required configuration changes, then run `docker compose up -d --wait --pull never` and check health and history. If using an image archive, load the new version before starting. Avoid an unpinned `latest` tag for upgrades.

**Moving from the original source-build setup:** its Compose volume may be named `gdeploy_gdeploy-data`, while the release bundle defaults to `gdeploy-data`. Before replacing your Compose file, inspect the current container's mounts to find the volume attached to `/data`. Preserve any existing `.env` and add `GDEPLOY_DATA_VOLUME=YOUR_EXISTING_VOLUME_NAME` to it so the release Compose file reuses that volume; for an automatic installation, create the optional `.env` with only this setting if it does not exist. Preserve the media directory too. For manual credentials, export `docker.env` using the command in section 7 only if you need plain Docker. For plain Docker, use that same existing volume name in `--mount source=...,target=/data`. A newly created empty volume will not contain your previous deployment history.

For plain Docker, pull/load the new image, stop and remove only the old `gdeploy` container, then repeat section 7 with the new image version, the same volume and any existing `docker.env`. Automatic installations must keep their saved `bootstrap.json` in that volume; manually configured installations must keep their original environment credentials.

Do not blindly roll back below **0.1.3** after choosing your account: older versions ignore the database administrator record and use the old bootstrap/environment login, which may be `admin`/`admin`. Restore a matching backup and review the login configuration before starting an older version. Automatic bootstrap was introduced in version **0.1.2**; earlier versions also require explicit environment credentials. Rollback is release-dependent: if an upgrade changes stored data incompatibly, changing the image tag alone is insufficient. Restore the matching backup and encryption key according to that release's notes. Restoring GDeploy's database does **not** undo ESXi VM changes or restore guest application data; inspect ESXi state before resuming work.

Do not run `docker compose down -v` or remove `gdeploy-data` unless you intend to erase GDeploy's saved history and credentials. Run one app container with one worker per data volume.
