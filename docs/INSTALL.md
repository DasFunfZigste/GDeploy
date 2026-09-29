# Install GDeploy from a release

This guide installs **GDeploy 0.1.0** on an Ubuntu Server **22.04 or 24.04 LTS, amd64/x86_64** host. The prebuilt image is `ghcr.io/dasfunfzigste/gdeploy:0.1.0`; you do not need to clone the source, build the image, or install Python on the host. GDeploy provisions **Ubuntu Server 24.04 LTS amd64** guests on standalone **ESXi 8.0 Update 3**.

Find each version, its changes, image digest and downloadable files on the [GitHub Releases page](https://github.com/DasFunfZigste/GDeploy/releases). The repository and its package are private, so sign in with a GitHub account that has access.

Choose your starting point:

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
GDEPLOY_VERSION=0.1.0
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
docker pull ghcr.io/dasfunfzigste/gdeploy:0.1.0
```

Replace `YOUR_GITHUB_USERNAME` with your GitHub username. Paste the token at Docker's password prompt; do not place it directly in a shell command. The included Compose file pins `ghcr.io/dasfunfzigste/gdeploy:0.1.0`. The release page also records the registry digest for that exact build.

### Alternative: load the image from a release asset

If you prefer downloading the image from GitHub instead of authenticating to GHCR, download the image archive using the same repository access:

```sh
GDEPLOY_VERSION=0.1.0
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

This restores the same versioned image locally. Use `docker compose up -d --pull never` in section 5; the bootstrap and `docker run` commands also use the loaded image. Loading the image avoids a registry pull, but VM installation still needs the Ubuntu and Elastic package networks described in section 6.

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

## 4. Generate the administrator credentials

Run the bundled configuration script inside the downloaded image:

```sh
cd "$HOME/gdeploy"
docker run --rm --pull never \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD,target=/setup" \
  ghcr.io/dasfunfzigste/gdeploy:0.1.0 \
  python /app/scripts/configure.py --directory /setup
```

This creates:

- **`.env`** for Docker Compose, including the encryption key, administrator password hash and checksums of the installers already present.
- **`docker.env`** for the optional plain `docker run` command.
- **`bootstrap-credentials.txt`**, containing the clearly labeled web-app username and generated password.

These files are readable only by their owner. Find your initial sign-in details with:

```sh
cat bootstrap-credentials.txt
```

Store the password in your password manager, then remove `bootstrap-credentials.txt`. Back up `.env` and the data volume together: **the encryption key is required to read saved ESXi and VM credentials**. Do not regenerate it during an upgrade. The script refuses to overwrite an existing configuration.

Run the script after placing your media so it records their checksums. If you add or replace an installer later, verify it against the publisher, calculate `sha256sum media/ubuntu.iso` or `sha256sum media/splunk.tgz`, and update the matching `GDEPLOY_UBUNTU_SHA256` or `GDEPLOY_SPLUNK_SHA256` value in your active environment file before recreating the container.

## 5. Start GDeploy with Docker Compose

```sh
cd "$HOME/gdeploy"
docker compose up -d --pull never
docker compose ps
docker compose logs --tail=100 gdeploy
curl --fail http://127.0.0.1:8000/api/health
```

Wait for the container to become **healthy**. The app restarts automatically when Docker starts. It listens on the server's **127.0.0.1:8000** by default.

If your browser is on the same machine, open [http://localhost:8000](http://localhost:8000). For a remote Ubuntu server, open a terminal on **your own computer** and keep this SSH tunnel running:

```sh
ssh -N -L 8000:127.0.0.1:8000 YOUR_UBUNTU_USER@YOUR_SERVER_ADDRESS
```

Then open [http://localhost:8000](http://localhost:8000) on your computer and sign in with the credentials from section 4. If port 8000 is already in use locally, use `-L 8001:127.0.0.1:8000` and browse to `http://localhost:8001` instead.

For shared access, put the app behind an HTTPS reverse proxy and keep its port private to the proxy. Set `GDEPLOY_COOKIE_SECURE=true` and `FORWARDED_ALLOW_IPS` to the proxy address/network as seen by the container, and forward the original `Host` and `X-Forwarded-Proto` headers. Recreate the container after environment changes. Docker-published ports can bypass UFW rules, so do not assume UFW alone protects a port published on every interface.

## 6. Connect to ESXi and deploy your first VMs

1. Open **ESXi connection** and enter the standalone ESXi 8.0 Update 3 hostname, username and password. Use the connection check to load inventory.
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

This is an initial lab release. Automated tests and a healthy app container do not validate a full unattended installation against your ESXi host. Complete the repository's [lab acceptance checklist](https://github.com/DasFunfZigste/GDeploy/blob/v0.1.0/docs/LAB_VALIDATION.md) before relying on it for workloads.

## 7. Existing Docker: run without Compose

After sections 2–4, use this **instead of** `docker compose up`. Do not start two GDeploy containers against the same data volume. Run from `~/gdeploy`:

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
  ghcr.io/dasfunfzigste/gdeploy:0.1.0

docker ps --filter name=gdeploy
docker logs --tail=100 gdeploy
curl --fail http://127.0.0.1:8000/api/health
```

Continue with the browser/SSH instructions in section 5 and ESXi setup in section 6. The image already declares UID/GID 10001 and its health check. The named volume keeps the database and encrypted credentials when the container is replaced.

Use **`docker.env`**, not `.env`, with `docker run --env-file`: Docker's CLI retains literal quotes, whereas Compose removes them. The generated administrator password hash contains dollar signs; keep the generator's single quotes in the Compose `.env`, and the unquoted values in `docker.env`. Never source these files as shell scripts. When editing configuration, update the file used by your selected launch method; keep both in sync if switching methods.

To refresh `docker.env` from an existing `.env` without regenerating credentials, run:

```sh
docker run --rm --pull never \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD,target=/setup" \
  ghcr.io/dasfunfzigste/gdeploy:0.1.0 \
  python /app/scripts/configure.py --directory /setup --export-docker-env
```

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
| Container exits or remains unhealthy | Read `docker compose logs`; confirm section 4 completed and the correct environment file is used. |
| Media preflight fails | Check the exact filenames, recorded checksums and UID 10001's read access. |
| ESXi connection fails | Check the hostname, port 443, certificate trust, credentials and API license/permissions. |
| Remote browser cannot connect | Use the SSH tunnel; the default app port is bound only to the server's loopback interface. |

Before upgrading, finish or resolve active deployments, read the new release's changelog and compatibility notes, then stop the app. Take a consistent backup of the **data volume and its matching `.env`/`docker.env` encryption key**, plus your Compose configuration and media. Keep the backup somewhere outside the live volume.

Download and verify the next release's bundle in a separate download directory, then update the files in the **same installation directory**. Keep your existing secret files, source media, Compose project and data volume. Do not rerun the initial credential generator. Pull the new versioned image, review any required configuration changes, then run `docker compose up -d --pull never` and check health and history. If using an image archive, load the new version before starting. Avoid an unpinned `latest` tag for upgrades.

**Moving from the original source-build setup:** its Compose volume may be named `gdeploy_gdeploy-data`, while the release bundle defaults to `gdeploy-data`. Before replacing your Compose file, inspect the current container's mounts to find the volume attached to `/data`. Preserve the existing `.env` and add `GDEPLOY_DATA_VOLUME=YOUR_EXISTING_VOLUME_NAME` to it so the release Compose file reuses that volume. Preserve the media directory too. Export `docker.env` using the command in section 7 only if you need plain Docker. For plain Docker, use that same existing volume name in `--mount source=...,target=/data`. A newly created empty volume will not contain your previous deployment history.

For plain Docker, pull/load the new image, stop and remove only the old `gdeploy` container, then repeat section 7 with the new image version and the same volume and `docker.env`.

Rollback is release-dependent: if an upgrade changes stored data incompatibly, changing the image tag alone is insufficient. Restore the matching backup and encryption key according to that release's notes. Restoring GDeploy's database does **not** undo ESXi VM changes or restore guest application data; inspect ESXi state before resuming work.

Do not run `docker compose down -v` or remove `gdeploy-data` unless you intend to erase GDeploy's saved history and credentials. Run one app container with one worker per data volume.
