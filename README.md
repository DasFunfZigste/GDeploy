# GDeploy

A dark, Docker-hosted control panel that creates Ubuntu VMs on **standalone VMware ESXi 8.0 Update 3** and installs selected applications. Splunk, Elasticsearch and Kibana each receive their own VM. Kibana connects to Elasticsearch using a service account token and verified TLS; Splunk runs independently.

**Initial release:** Ubuntu Server **24.04 LTS amd64 live-server** installation media, one administrator, one ESXi host, one VM per selected role per deployment. This is implementation-ready lab software; real ESXi deployment must be validated with your host, license, installation media and network before relying on it for production.

## Included

- A guided form for applications, VM names, vCPUs, RAM, disks, datastore, port group, DHCP or static IPv4.
- Optional plain Ubuntu VM alongside the application roles.
- Media checksum, inventory, name, capacity and network-input checks before provisioning.
- Unattended Ubuntu installation using a remastered, bootable ISO and NoCloud autoinstall configuration.
- Unique generated guest/application credentials, encrypted storage and an explicit **Credentials** view in every deployment.
- Stage progress, persisted events and deployment history. Interrupted work is identified on restart.
- Failed-deployment **delete & redeploy**, with typed confirmation and strict resource ownership checks.
- Web-app authentication, expiring sessions, CSRF protection and sign-in throttling.

Saved deployment profiles are outside this initial scope.

![GDeploy deployment dashboard](docs/overview.png)

## Start with Docker

1. Install Docker Engine with the Compose plugin on a machine that can reach both ESXi and the guest network. Allow several GiB of free local space for a temporary copy of the Ubuntu ISO.
2. Place your vendor-verified **Ubuntu Server 24.04 amd64 live-server ISO** at `media/ubuntu.iso`. For Splunk, also place your licensed **Splunk Enterprise Linux x86_64 .tgz** at `media/splunk.tgz`. Verify downloads against the publishers' checksums before proceeding. GDeploy does not download Splunk or supply a license. On Linux, the container's UID 10001 must be able to read this bind mount: use directory mode 0755 and installer-file mode 0644, or equivalent ACLs. Keep secrets out of this public-media directory.
3. Generate the local configuration with Python 3.12 or later:

   ```sh
   python3 scripts/configure.py
   ```

   This creates an encryption key, a hashed administrator password and checksums for the media already present. The generated web-app password is clearly labeled in **`bootstrap-credentials.txt`** (owner access only). `.env`, credentials, installation media and database files are ignored by Git. Store the password in your password manager, then remove the bootstrap file.

   If Python is unavailable locally, run the same script with Docker:

   ```sh
   docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/project" -w /project python:3.12-slim python scripts/configure.py
   ```

4. Start the app:

   ```sh
   docker compose up --build -d
   ```

5. Open **http://localhost:8000**, sign in, then open **Settings** and save your ESXi hostname, username and password. Use the connection check to load inventory.
6. Select **New deployment**, choose application roles, and configure each separate VM. Run preflight, review the results, and deploy. Open the resulting deployment for progress, logs, endpoints and **Credentials**.

The default Compose binding is loopback-only. For access from another machine, use an HTTPS reverse proxy, forward the original `Host` and `X-Forwarded-Proto`, and set `GDEPLOY_COOKIE_SECURE=true`. Set Uvicorn's `FORWARDED_ALLOW_IPS` environment variable to the actual proxy address or trusted proxy network as seen by the container, so the app recognizes the browser's HTTPS origin. Keep the app port private to that proxy; do not trust forwarded headers from arbitrary clients. The cookie setting must match how the browser accesses the application.

## ESXi and network preparation

- Use a standalone ESXi 8.0 U3 endpoint. vCenter, distributed switches and clusters are outside this release.
- The ESXi account needs permission to read inventory, create/configure/power/delete VMs, allocate datastore space and manage the deployment's ISO files. ESXi licensing must permit provisioning through the vSphere API. Inventory checks prove authentication and read access; actual deployment operations validate write privileges and license capabilities.
- The app connects to ESXi over HTTPS **443** for both API operations and datastore uploads. TLS verification is enabled by default. Connect using the hostname on the certificate.
- The app must reach guests on SSH **22**. Guests need DNS and HTTPS/HTTP access to Ubuntu mirrors and the official Elastic package repository; installation cannot complete on an isolated network without corresponding mirror support.
- Kibana must reach Elasticsearch on HTTPS **9200**. Your browser needs access to Kibana **5601** and Splunk Web **8000**. Splunk's local verification uses its management API on **8089**. GDeploy does not configure your network firewall or switch.
- Use static addresses or DHCP reservations for application VMs. Elasticsearch's certificate and Kibana's configuration use the assigned Elasticsearch IP; a later DHCP address change requires reconfiguration.
- Static networking takes an IPv4 CIDR address, gateway and DNS servers. Preflight checks syntax and in-app reservations; it cannot guarantee an address is unused elsewhere on your LAN.
- ESXi and guest time should be synchronized for TLS.

For an ESXi certificate signed by a private CA, make a PEM bundle containing the normal public roots plus that CA and mount it read-only (for example under `/media/esxi-ca-bundle.pem`). Set both `SSL_CERT_FILE=/media/esxi-ca-bundle.pem` and `REQUESTS_CA_BUNDLE=/media/esxi-ca-bundle.pem` in `.env`, then recreate the container. This allows the Python SOAP client and HTTPS uploader to validate the same certificate chain. Do not commit private keys.

## Credentials and application behavior

- Each VM receives the OS administrator **`gdeploy`**, a different random password, and a separate automation SSH key. VM names remain hostnames; they are not passwords.
- Open a deployment and select **Reveal credentials** to see the exact VM username/password, IP, SSH host key, application URL and application credentials. Reveals are recorded in the deployment events. The browser conceals the panel automatically after one minute and when navigating away. Copying a value leaves it in your operating-system clipboard until replaced.
- ESXi credentials, guest passwords, SSH private keys and application secrets are encrypted using the persistent key in `.env`. Keep that key backed up together with the data volume; losing it prevents decryption. Application administrators can intentionally reveal deployment passwords.
- Splunk receives its own `admin` password. Selecting Splunk requires explicit acceptance of its software license. License activation beyond the supplied package's initial behavior is a separate administration step.
- Elasticsearch is a single-node install from the official **9.x** apt repository. Kibana uses the exact installed Elasticsearch package version. Both packages are held to prevent uncoordinated upgrades. Plan upgrades and backups separately.
- Elasticsearch uses a deployment CA; Kibana verifies that CA and uses a dedicated Elastic service token. Initial interactive Kibana and Elasticsearch sign-in uses the generated `elastic` administrator credentials. Create narrower application users for ongoing use.
- Application browser endpoints use generated certificates. Trust the appropriate certificates through your normal administration process, or replace them with your organization's certificates. The generated service certificates expire after 825 days; the Elasticsearch CA expires after 10 years.
- First SSH contact uses trust on first use over your managed guest network. The observed key is persisted and required for later connections. This is not a substitute for a trusted provisioning network.

## Failure recovery

When a stage fails, inspect its error and ESXi console before selecting **Delete & redeploy**. Type the deployment name to confirm permanent deletion of that deployment's VMs and virtual disks, including any data already written. A fresh deployment uses the saved choices and new credentials.

Cleanup verifies both the deployment UUID annotation and datastore paths before deleting a VM. It discovers tagged VMs even if the app stopped before recording their IDs. Shared datastores, networks, source media and unrelated VMs are retained. Manually moving a disk outside its deployment directory causes cleanup to stop for inspection. If deletion fails, no replacement is started; retry after fixing the cause. If cleanup succeeds but the new preflight fails, the error still appears under delete/redeploy and a later retry is safe.

A timed-out ESXi operation can still be running on the host. Inspect its tasks before retrying. OS installation has a configurable one-hour limit (`GDEPLOY_OS_TIMEOUT`); software stages also use bounded waits.

## Persistence and operation

The named `gdeploy-data` volume contains SQLite history, encrypted secrets and temporary media work. Installation media is removed from the datastore after the OS is ready. Source files in `media/` are never modified. Deployment history is retained after cleanup.

Run **one app container with one worker** against a data volume. Jobs are serialized; this release does not support multiple replicas. The container runs without root privileges or Linux capabilities, with a read-only root filesystem. Stop the app before taking a consistent backup of its data volume and `.env`. Do not use `docker compose down -v` unless you intend to erase the app's database and credentials.

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
