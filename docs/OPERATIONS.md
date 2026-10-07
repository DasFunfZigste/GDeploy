# GDeploy operations and reference

Install or update GDeploy using the [short Ubuntu 24.04 guide](INSTALL.md). This reference covers optional network settings, account recovery, installation media, software configuration, storage, backups and advanced launch methods for **GDeploy 0.9.2**. Commands for source installations assume the same clone at `~/GDeploy` and use `sudo docker`; users already authorized to run Docker directly may omit `sudo`.

- [Network binding](#optional-restrict-the-bind-address-or-change-the-port) and [HTTPS/SSH access](#https-and-optional-ssh-access)
- [Account setup](#complete-the-first-sign-in) and [startup recovery](#fix-an-existing-source-checkout-that-cannot-start)
- [Installation media](#prepare-an-os-iso-and-optional-application-installers) and [Setup](#complete-setup-and-deploy-your-first-vms)
- [Checks, backups, updates and recovery](#check-stop-upgrade-and-recover)
- [Manual credentials](#optional-manual-credentials), [plain Docker](#optional-plain-docker-launch) and [release image archives](#optional-release-image-archive)

## Optional: restrict the bind address or change the port

Both source and release Compose default to **`0.0.0.0:8000`**, publishing the app on all host IPv4 interfaces. Existing explicit `GDEPLOY_BIND_IP` values in `.env` are still respected; an older `127.0.0.1` setting continues to allow only local connections.

To restrict access, edit the existing `.env` in your setup directory, or create it if it is absent. **Preserve existing contents and encryption keys**, and keep one value per setting. For example, restrict access to the Docker host or an SSH tunnel with:

```dotenv
GDEPLOY_BIND_IP=127.0.0.1
GDEPLOY_PORT=8000
```

You can instead set `GDEPLOY_BIND_IP` to a specific LAN IPv4 address, or `0.0.0.0` to use the default on all interfaces. `GDEPLOY_PORT` changes the host port. Recreate the container to apply changed mappings; no image rebuild is needed:

```sh
sudo docker compose up -d --force-recreate --wait
sudo docker compose port gdeploy 8000
```

Use the selected address and port in the browser URL. If binding to one LAN address, also use that address in the host-side health checks later in this guide. Docker-published ports can bypass UFW rules; a firewall allow rule does not override an explicit loopback-only Docker binding.

For direct HTTP, keep `GDEPLOY_COOKIE_SECURE=false`. For an HTTPS reverse proxy, use `true` and the [HTTPS/proxy guidance](#https-and-optional-ssh-access).

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
sudo docker compose up -d --build --wait --force-recreate
```

Keep your existing `.env` and data volume. On a fresh volume, both credential settings may be absent or empty; sign in with `admin`/`admin` and complete account setup. Existing installations retain their current credentials, and a completed setup is not repeated after restart or rebuild.

For an older automatic installation whose initial password you have not changed, retrieve its original sign-in details with:

```sh
sudo docker compose exec gdeploy cat /data/bootstrap-credentials.txt
```

If you previously used `scripts/configure.py`, its initial password is in **`bootstrap-credentials.txt` in the host setup directory** instead. These files show only the initial login. After completing account setup, use your chosen credentials; bootstrap files and environment settings do not reset the saved account. Do not run the configuration generator to repair an existing installation.

If a database already exists without its original encryption key, startup stops with a recovery message. Restore the matching original `.env`/`docker.env`, or `/data/bootstrap.json` for an automatic installation, from backup. Supplying only one of `GDEPLOY_SECRET_KEY` and `GDEPLOY_ADMIN_PASSWORD_HASH`, damaged saved credentials, or conflicting environment and saved credentials also stops startup instead of rotating the key. Preserve the files and use the logged recovery guidance; deleting the volume would erase history and credentials.

If Compose rejects `env_file.required`, upgrade to Compose 2.24 or later using the [Ubuntu installation commands](INSTALL.md#install-docker-and-git).

## HTTPS and optional SSH access

An **optional SSH tunnel** is useful if you have restricted the bind address to `127.0.0.1`. Open a terminal on your own computer and keep this running:

```sh
ssh -N -L 8000:127.0.0.1:8000 YOUR_UBUNTU_USER@YOUR_SERVER_ADDRESS
```

When using the tunnel, open [http://localhost:8000](http://localhost:8000) on your computer and sign in as described above. If port 8000 is already in use locally, use `-L 8001:127.0.0.1:8000` and browse to `http://localhost:8001` instead.

LAN access works with the default binding. For HTTPS access, put the app behind a reverse proxy and keep its port private to the proxy. Set `GDEPLOY_COOKIE_SECURE=true` and `FORWARDED_ALLOW_IPS` to the proxy address/network as seen by the container, and forward the original `Host` and `X-Forwarded-Proto` headers. Recreate the container after environment changes. Docker-published ports can bypass UFW rules, so do not assume UFW alone protects a port published on every interface.

## Prepare an OS ISO and optional application installers

On the Ubuntu server, working in `~/GDeploy`:

1. Obtain the supported **Ubuntu Server 24.04 LTS amd64 live-server ISO** from [Ubuntu's official release directory](https://releases.ubuntu.com/24.04/), or use your existing copy on an ESXi datastore. Verify the publisher checksum using Ubuntu's [verification instructions](https://ubuntu.com/tutorials/how-to-verify-ubuntu). Keep the verified SHA-256 value for Setup.
2. Choose an existing ESXi ISO through Setup's datastore browser, upload it from your computer after sign-in, or copy it into the GDeploy server's **`media/`** directory for the server picker. An `.iso` filename such as `os.iso` is suitable; no specific name is required for the picker.
3. To deploy Splunk, obtain your licensed **Splunk Enterprise Linux x86_64 `.tgz`** from [Splunk](https://www.splunk.com/en_us/download/splunk-enterprise.html) and its publisher SHA-512 checksum. Splunk publishes that value at the package's official download URL with `.sha512` appended; see [Configure the Splunk package](#configure-the-splunk-package) for the steps. An existing verified publisher SHA-256 is also supported. Upload the package through **Setup → Software packages** after sign-in, or copy it to **`media/`** for the server picker. Keep its vendor filename if desired; renaming it to `splunk.tgz` is unnecessary. GDeploy does not download the package for you or provide a license.
4. To deploy **Corelight FleetManager**, obtain its product identity `.pem` from Corelight and choose a strong shared community string. Online installation also requires the customer repository token from [Corelight Cloud](https://my.corelight.cloud/) under **Downloads → Fleet Manager**. Offline installation requires the `corelight-fleet` amd64 `.deb` and any missing Ubuntu dependencies; upload these packages in the wizard's FleetManager step or Setup, or place them in `media/`. Keep the private PEM out of the server media directory and select it through the dedicated license field. See [Configure FleetManager](#configure-fleetmanager) below.

For example, replace these source paths with the files you downloaded:

```sh
cd ~/GDeploy
mkdir -p media
# Only if using the server picker instead of browser upload:
cp /path/to/downloaded-live-server-amd64.iso media/os.iso
# Only if using the server picker for Splunk instead of browser upload:
cp /path/to/splunk-VERSION-linux-amd64.tgz media/

chmod 0755 media
# Only if the ISO was copied to the server:
chmod 0644 media/os.iso
# Only if that Splunk file was copied to the server:
chmod 0644 media/splunk-VERSION-linux-amd64.tgz
```

Replace `splunk-VERSION-linux-amd64.tgz` with the downloaded package's actual filename. The app container runs as UID 10001 and needs read access to server-mounted media. Equivalent ACLs are also suitable. The server directory is mounted read-only at `/media`; ISO uploads and copies imported from ESXi are saved in `/data/media`, while Splunk uploads use `/data/packages`, within the persistent data volume. Keep passwords and private keys out of `media/`. Allow local space for retained ISOs/packages plus several GiB for temporary media preparation, and enough storage on ESXi for the selected VMs.

Enter the publisher-verified checksum for the OS ISO and Splunk package in their Setup tabs. FleetManager accepts an optional expected SHA-256 for a single uploaded `.deb` and always computes integrity checksums; a calculated hash alone does not establish publisher provenance. None of these UI workflows needs environment settings. Existing Splunk environment configuration remains a fallback; see [Configure the Splunk package](#configure-the-splunk-package) below.

## Complete Setup and deploy your first VMs

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
sudo docker compose exec gdeploy df -h /data /tmp
```

For a plain Docker installation, use `docker exec gdeploy df -h /data /tmp`. If you deliberately changed `GDEPLOY_DATA_DIR`, inspect that path instead of `/data` and ensure it is backed by persistent writable storage.

To reclaim space:

1. Find an unused upload or ESXi-imported copy under **Storage & saved ISOs**. Its original source is shown so you can identify the right file. If it is the current choice, use **Clear saved selection** in the saved ISO summary, or choose another ISO.
2. Select **Delete**, review **Delete saved ISO?**, then confirm **Delete ISO**. This permanently removes only GDeploy's local saved copy. To use it again, restore a backup or upload/import it again.
3. If deletion is disabled, follow the displayed reason. Clear the saved selection or choose another OS ISO if this is still the current default. Copies referenced by **queued, running or cleaning deployments** remain protected until that work is resolved, even after clearing the default.

**Clear saved selection** removes only the saved default, without deleting any file or changing active deployment snapshots. Existing environment-configured ISO settings become the fallback. If there is no valid fallback, select an ISO before starting another deployment. You can therefore clear and delete your only unused uploaded/imported copy without needing space to upload a replacement first. Clearing alone does not reclaim storage; the separate **Delete ISO** action does.

The storage controls cannot delete files from the read-only `/media` server mount or original ISOs on ESXi. Deleting a local copy does not delete VMs, credentials or deployment history. Temporary deployment files are managed by the worker; there is no manual workspace-delete control for active work.

If available space is still insufficient, arrange more capacity on the filesystem backing the Docker data volume, or migrate the complete volume to larger persistent storage using your Docker host's storage procedure. Docker Desktop may also have a separate virtual-disk size limit. Back up the stopped app's complete volume and encryption configuration first, preserve ownership for UID/GID 10001, and reuse the existing data after migration. Changing an environment path or creating an empty volume does not expand the original storage or preserve its contents. Keep space for both retained ISOs and each deployment's generated installation media.

### Configure the Splunk package

Open **Setup → Software packages**, or `http://SERVER_LAN_IP:8000/#settings/packages`. Under **Splunk Enterprise**:

1. Choose **Upload a package** to select the licensed Linux x86_64 `.tgz` on your computer, or **GDeploy server** to select an existing `.tgz` from the read-only server media directory. Vendor filenames are supported.
2. Paste the publisher's checksum into **Publisher checksum (SHA-512 or SHA-256)**. Use Splunk's **SHA-512** value (128 hexadecimal characters) or an existing verified **SHA-256** value (64 hexadecimal characters). GDeploy recognizes the algorithm from the length.
3. Select **Upload & use package** or **Use selected package**. GDeploy verifies the bytes and checks the archive's paths and expected Splunk Enterprise x86_64 binaries before saving. A rejected checksum or archive keeps the previous selection. The package must be compatible with the supported Ubuntu guest; archive validation does not replace testing that particular Splunk version.

To get Splunk's publisher checksum, copy the exact direct download URL for your chosen `.tgz` from Splunk's official download page. Open that URL in a browser with **`.sha512` appended**: a URL ending in `.tgz` becomes `.tgz.sha512`. Copy only the **128 hexadecimal characters** from the checksum file into GDeploy, without the filename or the rest of the line. These are [Splunk's documented checksum instructions](https://help.splunk.com/en/splunk-enterprise/administer/install-and-upgrade/10.4/secure-your-splunk-enterprise-installation/install-splunk-enterprise-securely). You do not need to calculate a checksum yourself: GDeploy hashes the uploaded or selected package and compares it with the publisher's value. A hash calculated from an unverified download alone would not verify its source.

Uploads accept packages up to **4 GiB**, need free space on GDeploy's data filesystem and persist under `/data/packages`. No `.env` edit, container restart or package rename is needed. Include uploaded packages in complete data-volume backups; back up server-mounted packages separately. GDeploy does not bundle or automatically download Splunk, supply a license, or bypass the license acceptance required in each Splunk deployment. Elasticsearch and Kibana continue to use the Elastic package repository.

If preflight reports a missing Splunk package, select **Configure Splunk package** in the deployment wizard, finish this Setup step, then select **Return to deployment**. The open browser retains your deployment draft, including each VM's resource and network choices. Rerun preflight before deploying. The draft is not a saved deployment profile; keep the browser page open while fixing the configuration.

After validating the publisher's checksum, GDeploy computes an internal SHA-256 for the package. Each new Splunk job captures its package path and that SHA-256 when queued. Later Setup changes apply to future jobs, and the selected file must remain available and unchanged until the original job finishes. After transfer, the guest verifies the package against the queued SHA-256 before package-manager or extraction work. New **Delete & redeploy** jobs use the current package selection. Jobs created before v0.8.0 keep their original environment-configured package behavior rather than adopting a new UI default.

To manage uploaded packages, use **Clear saved selection** or choose another package before deleting an unused upload, then confirm **Delete package**. Clearing removes only the saved default; it does not delete a file. Uploads referenced by queued, running or cleaning jobs remain protected even after clearing. Deletion removes only the eligible uploaded copy; it cannot delete server-mounted files, change a VM or uninstall Splunk already running on a VM. Restore a deleted copy from backup or upload it again if needed.

When no UI package choice is saved, existing environment settings remain the fallback:

```dotenv
GDEPLOY_SPLUNK_PACKAGE=/media/splunk.tgz
GDEPLOY_SPLUNK_SHA256=YOUR_PUBLISHER_VERIFIED_SHA256
```

The compatible default path is `/media/splunk.tgz`; an explicit environment path can select another filename. The server picker browses the directory containing that configured path, normally `/media`. The legacy `GDEPLOY_SPLUNK_SHA256` variable still requires a verified 64-character SHA-256; use **Setup → Software packages** for Splunk's publisher SHA-512. If maintaining these legacy settings, preserve existing `.env`/`docker.env` contents and recreate the container to apply environment changes. A saved Setup choice takes precedence; **Clear saved selection** restores the fallback. Without a valid saved choice or fallback, preflight blocks Splunk deployment.

### Configure FleetManager

Select **FleetManager** in **New deployment** and finish **Configure VMs**. The next step, **FleetManager configuration**, collects its installation settings before **Review & deploy** and before preflight. It reuses existing saved defaults. **Setup → Software packages → FleetManager** remains available for defaults and package management, including at `http://SERVER_LAN_IP:8000/#settings/packages/fleetmanager`.

This optional role creates a separate Corelight Fleet Manager VM. Its default resources are **2 vCPUs, 8 GiB RAM and an 80 GiB disk**; minimum inputs are 2 vCPUs, 8 GiB RAM and 60 GiB disk. The installer also checks for 30 GiB free at `/var` and 20 GiB free at `/tmp` inside the guest. These paths normally share the full root filesystem.

For **Online repository**:

1. Sign in to [Corelight Cloud](https://my.corelight.cloud/), open **Downloads → Fleet Manager**, and copy the customer authentication token.
2. Select **Online repository** in the wizard's **FleetManager configuration** step. Enter the token and a strong shared **community string** without single/double quotes. Choose the customer product identity `.pem` license from your computer. Leave secret fields blank and omit a replacement PEM to reuse saved values.
3. Select **Save & continue** to validate and save the settings, then open **Review & deploy**. Explicitly run preflight before starting the deployment. The guest must reach Ubuntu package mirrors and `pkgrepos.corelight.cloud` over HTTPS. GDeploy configures the signed repository, installs `corelight-fleet` and retains the repository for later administration.

For **Offline package**:

1. Obtain a Ubuntu-compatible **`corelight-fleet` amd64 `.deb`** through your Corelight entitlement, plus any additional amd64/`all` dependency `.deb` files absent from a fresh Ubuntu 24.04 VM. The [FleetManager walkthrough](FLEETMANAGER.md#offline-installation) explains package preparation.
2. Use the regular Ubuntu Server 24.04 LTS amd64 live-server ISO containing the base SSH, VMware Tools, Python and CA-certificate packages. Offline mode disables installer mirror selection and uses its offline fallback; minimal or incomplete media can fail before software installation.
3. Select **Offline package** in the wizard's **FleetManager configuration** step and supply or reuse the saved community string and product identity PEM. A repository token is unnecessary. Under **Offline packages**, use **Upload packages** for one or multiple `.deb` files, or place them in the host's `media/` directory and select **Refresh**. Uploads allow **4 GiB per file**. An optional expected SHA-256 applies to a single upload; otherwise GDeploy computes the integrity hash.
4. Select the main **FleetManager .deb package** and any **Additional dependency packages**, then select **Save & continue**. Uploading alone does not save a package selection. In **Review & deploy**, explicitly run preflight, review the results and deploy.

Saving in the wizard also updates defaults for future FleetManager deployments. You can prepare the same defaults outside the wizard with **Save FleetManager setup** in Setup. Saving configuration does not queue a deployment; review and preflight remain separate actions.

Offline FleetManager performs no repository downloads for its OS/package installation. Apt uses only supplied `.deb` files and installed dependencies; missing dependencies stop the deployment with a logged error. GDeploy, ESXi and the VM still need local connectivity, and other selected VM roles retain their own online installation requirements. Choose the complete matching dependency set before relying on an isolated deployment.

The PEM license is limited to **64 KiB**. It must include the currently valid product identity certificate and matching unencrypted private key, with the matching leaf certificate first if a chain is included. GDeploy validates the file structure and pair; Fleet Manager validates the actual Corelight entitlement. Setup stores secrets encrypted and shows only saved-state flags plus license name, checksum and expiry. Blank secret fields/no replacement license retain saved values. The saved license is not exposed as an ordinary downloadable file.

On the new VM, GDeploy writes `/etc/corelight-fleetd.conf` and the license `/etc/corelight-fleetd.pem`, enables `corelight-fleetd`, and checks HTTPS **443** and the sensor listener **1443**. Open the deployment's **Credentials** for the **`admin`** temporary password, saved community string and `https://VM_IP`; **change the administrator password at first sign-in**. Sensor enrollment, browser certificate configuration, existing-VM upgrades and ongoing guest backups remain administration tasks. See the [complete FleetManager walkthrough](FLEETMANAGER.md) for network, upgrade and troubleshooting details.

FleetManager/dependency uploads persist in `/data/fleetmanager-packages`. Each queued job retains its configuration, license and package checksums; editing or clearing Setup does not change existing jobs or VMs. **Clear FleetManager setup** removes defaults while keeping uploads. Selected or queued/running/cleaning packages cannot be deleted; mounted files are managed on the Docker host. Back up the complete data volume and original encryption key, and separately back up mounted packages and vendor license records. Live licensed-package and ESXi acceptance has not been performed for this feature; use the [lab checklist](LAB_VALIDATION.md#fleetmanager-installation).

### Deploy your VMs

1. Open **Setup**, listed above **Deployments** in the navigation, to finish ESXi and OS ISO configuration. Configure the package in **Software packages** if deploying Splunk. Select **New deployment**, enter a deployment label and choose OS only, Splunk, Elasticsearch, Kibana and/or FleetManager. Each chosen role receives a separate VM. Kibana requires Elasticsearch and is configured to connect to it; Splunk and FleetManager run independently.
2. In **Configure VMs**, fill in each role's visible section with its own VM name, CPU, RAM, disk size, datastore, port group and DHCP/static network settings. Elasticsearch and Kibana have separate sections and VM names. The deployment label groups the job; it is not a shared VM name. Use the section navigation to move between forms; all selected forms stay visible together. Review the separate values for every VM before continuing.
3. When FleetManager is selected, complete **FleetManager configuration** with its online/offline mode, community string, license and required token/packages, then select **Save & continue**. Existing defaults can be reused. This step appears before review even if FleetManager was configured previously.
4. In **Review & deploy**, explicitly run preflight, address any reported issues, and start deployment. Without FleetManager, the wizard goes directly from **Configure VMs** to this review step and remains three steps long.
5. Open the deployment to follow its stages and errors. In **Deployment logs**, select **View logs** to expand recorded events and sanitized diagnostic details, and **Copy logs** to copy them. The failure banner's **View deployment logs** opens the same view. When ready, use the application links and **Reveal credentials** panel for the VM passwords and application sign-in details.

On an HTTP LAN address where automatic clipboard access is unavailable, **Copy logs** selects the text for manual copying. Older failures retain only the text originally recorded; upgrading cannot recover discarded tool output. New media-preparation errors include captured diagnostics where available, without exposing generated credentials or the autoinstall secret data.

The OS username is **`gdeploy`** with a different generated password per VM. The web-app administrator password, guest OS passwords and application passwords are separate. The deployment's Credentials panel is the ongoing place to find guest and application details.

The Docker host needs access to ESXi HTTPS **443** and guest SSH **22**. Ordinary installations need working DNS and Ubuntu mirror access; Elastic roles also require the official Elastic package repository. Online FleetManager additionally needs `pkgrepos.corelight.cloud`; offline FleetManager uses its ISO and supplied packages without repository downloads. Kibana needs access to Elasticsearch HTTPS **9200**; your browser needs access to Kibana **5601**, Splunk Web **8000** or FleetManager **443**. Corelight sensor management interfaces need FleetManager **1443**. GDeploy does not configure your firewall or switches. Prefer static IPs or DHCP reservations for application VMs because Kibana's configuration and Elasticsearch's certificate use Elasticsearch's assigned address.

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

### Configure SSH access for new VMs

Open **Setup → SSH access**, or go directly to `http://SERVER_LAN_IP:8000/#settings/ssh`. Paste one or more **public** keys, one complete OpenSSH key per line, then save. Each key normally starts with `ssh-ed25519`, `ssh-rsa` or `ecdsa-sha2-`; an optional trailing comment identifies its owner. Review the saved fingerprints and key count. Duplicate key material is saved only once, even when comments differ. GDeploy supports up to 50 keys: Ed25519, RSA of at least 2048 bits, and ECDSA on the NIST P-256, P-384 or P-521 curves. Private keys, authorized-keys options and SSH certificates are not accepted.

If you already have a key pair, copy the contents of its `.pub` file. If you need a new key pair, run these commands **on the workstation you will SSH from**, choosing an unused filename and following the prompts:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/gdeploy_ed25519 -C "you@your-workstation"
cat ~/.ssh/gdeploy_ed25519.pub
```

Paste the output of `cat` into Setup. Keep the file without `.pub`, `~/.ssh/gdeploy_ed25519`, on your workstation; it is your private key and must not be pasted into GDeploy. After creating a VM, use its address from the deployment details:

```sh
ssh -i ~/.ssh/gdeploy_ed25519 gdeploy@VM_IP
```

The saved keys are installed for the **`gdeploy` Linux user** on every VM in newly queued deployments. The generated automation key, guest password and existing sudo behavior remain available. SSH keys do not sign you into the GDeploy web app. This section is optional and can remain empty.

Edit the list and save to add or remove keys; save an empty list to stop adding administrator keys to future deployments. Each job captures the saved list when queued, so subsequent edits do not affect already queued jobs or existing VMs. Removing a key here does **not** revoke it on a VM that already exists; manage that VM's `~gdeploy/.ssh/authorized_keys` separately. New **Delete & redeploy** jobs use the latest saved list and still permanently delete the original VMs as described above. Existing jobs created before this feature keep their original automation key only. Settings persist across rebuilds using the same data volume, without a container restart to apply changes to future jobs.

### Manage deployment history

Select **Hide** beside a finished record in **Deployment history**, or **Hide from history** on its detail page. The record disappears from the default list. Enable **Show hidden** to include hidden records with a **Hidden** badge, then select **Restore** in the row or **Restore to history** on the detail page. Hidden records remain accessible through their direct links, and their visibility setting survives container restarts and rebuilds using the same data volume. Queued, running and cleaning jobs cannot be hidden.

Hiding changes only history visibility. It preserves VMs and their disks, the original status, logs, credentials, ownership and resource reservations. For example, a healthy OS-only VM can be kept while its failed readiness-check record is hidden. Hiding does not mark that job successful, retry its checks, finish provisioning or detach/delete its installation media. Use the deployment logs and ESXi console to assess unfinished work; **Delete & redeploy** is for deliberately replacing the deployment's VMs.

### Check Ubuntu readiness after a deployment error

An older release could stop at **Could not verify Ubuntu cloud-init completion** even when the installed VM was accessible. GDeploy combined stdout and stderr before parsing cloud-init's JSON; a permission warning could therefore invalidate otherwise valid JSON. It also required `status: done`, rejecting Ubuntu's clean `status: disabled` / `boot_status_code: disabled-by-marker-file` state after installation.

Version **0.7.1** separates stdout and stderr and accepts that disabled state only after verifying first-boot completion evidence, installer artifacts, an installed root filesystem, working sudo, and active SSH and VMware Tools services. It still rejects cloud-init errors; simply finding a disabled marker is not sufficient. Transient startup states are retried within the existing OS-installation timeout. Open **Deployment logs → View logs** for sanitized status and readiness diagnostics from new attempts.

For an existing VM, sign in as `gdeploy` using its revealed credentials or your installed SSH key and inspect:

```sh
sudo cloud-init status --long
systemctl is-active ssh open-vm-tools
findmnt -n -o SOURCE,FSTYPE /
```

`disabled-by-marker-file` with no reported errors can be normal on an installed Ubuntu server. An epoch-like status timestamp alone does not establish a clock problem. Check the services, mounted root filesystem and installer/first-boot evidence together; SSH access alone does not prove every provisioning step finished.

After OS readiness passes, GDeploy detaches and deletes that VM's temporary installation ISO, finishes any remaining VMs, and installs/verifies the selected software before completing the job. An **OS only** VM has no application package installation after this check. A record stopped at readiness can therefore leave its temporary ISO attached/stored or selected applications unfinished.

Update GDeploy using the [source-update commands](INSTALL.md#update-gdeploy), keeping its data volume and configuration. Keep the same verified `ubuntu-24.04.5-live-server-amd64.iso`; the readiness fix does not require a new ISO upload. Existing failed jobs are not resumed automatically, and this release adds no resume/finalize button. Preserve a healthy VM and use **Hide from history** to remove its record from the default view if desired. Rebuilding the app container and hiding history do not delete the VM or complete its outstanding work.

This is a lab release. Automated tests and a healthy app container do not validate a full unattended installation against your ESXi host. Complete the repository's [lab acceptance checklist](https://github.com/DasFunfZigste/GDeploy/blob/v0.9.2/docs/LAB_VALIDATION.md) before relying on it for workloads.

## Optional manual credentials

The [simple installation guide](INSTALL.md) uses automatic credentials and does not need a configuration script. If you intentionally want credentials in host environment files, run the script **before the first start on a fresh volume**. It creates the same initial `admin`/`admin` login and a unique encryption key, with account replacement required at first sign-in. Do not run it after automatic setup, against existing app data, or to repair a missing key.

Build the local image and run the configuration script from your clone:

```sh
cd ~/GDeploy
sudo docker build -t gdeploy:local .
sudo docker run --rm --pull never \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD,target=/setup" \
  gdeploy:local \
  python /app/scripts/configure.py --directory /setup
```

This creates owner-readable `.env` for Compose, `docker.env` for plain Docker, and `bootstrap-credentials.txt` recording the initial login. Back up these files and the data volume together. The script refuses to overwrite existing configuration. It records checksums for `media/ubuntu.iso` and `media/splunk.tgz` if present; you can use other filenames through Setup and supply the publisher's verified checksum there.

Use `docker.env` with `docker run --env-file`. Compose `.env` quoting and Docker CLI environment-file quoting differ, particularly for password hashes containing dollar signs. Never source either file as a shell script. To refresh `docker.env` from an existing complete manual `.env` without changing credentials, rerun the command above with `--export-docker-env` appended to the Python command. Automatic installations keep their original key in `/data/bootstrap.json`; do not supply new administrator key/hash values when changing launch methods.

## Optional plain Docker launch

Use this only as an alternative to Compose. Do not start a second GDeploy container against the same data volume. For a fresh source installation, build a local image and run it:

```sh
cd ~/GDeploy
sudo docker build -t gdeploy:local .
sudo docker volume create gdeploy-data
sudo docker run -d --name gdeploy \
  --pull never \
  --restart unless-stopped \
  --init \
  --env GDEPLOY_COOKIE_SECURE=false \
  --publish 0.0.0.0:8000:8000 \
  --mount source=gdeploy-data,target=/data \
  --mount "type=bind,source=$PWD/media,target=/media,readonly" \
  --read-only \
  --tmpfs /tmp:rw,size=256m,mode=1777 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --stop-timeout 30 \
  gdeploy:local

sudo docker ps --filter name=gdeploy
sudo docker logs --tail=100 gdeploy
```

Open `http://YOUR_SERVER_IP:8000` and complete the first sign-in. The image runs as UID/GID 10001 and includes its health check. The `GDEPLOY_COOKIE_SECURE=false` setting permits direct HTTP sign-in; use `true` with an HTTPS reverse proxy. To restrict the binding, replace `0.0.0.0` with a particular LAN address or `127.0.0.1`.

For an existing installation, reuse its current `/data` volume and encryption configuration. A manually configured installation needs its existing `--env-file docker.env`; replace the cookie line with that option if the file already supplies the correct cookie setting. For an automatic installation, keep `bootstrap.json` in the reused volume and omit administrator key/hash settings. See [backup and update guidance](#check-stop-upgrade-and-recover) before replacing any existing container.

## Optional release image archive

The public [GitHub Releases page](https://github.com/DasFunfZigste/GDeploy/releases) provides each version's changelog, Docker image details and downloadable files. Building from source remains the default installation path. Repository visibility does not determine GHCR package visibility; the archive option below loads the image locally without a registry pull.

For a **fresh archive-based installation**, download these three files from release **v0.9.2** in your browser and copy them into a new directory on the Ubuntu server:

- `gdeploy-0.9.2-deploy.tar.gz`
- `gdeploy-0.9.2-linux-amd64.image.tar.gz`
- `SHA256SUMS`

From that directory, verify both archives:

```sh
sha256sum --check --ignore-missing SHA256SUMS
```

Continue only if both downloaded archives report **OK**, then extract the bundle and load the image:

```sh
tar -xzf gdeploy-0.9.2-deploy.tar.gz
sudo docker load --input gdeploy-0.9.2-linux-amd64.image.tar.gz
sudo docker compose up -d --wait --pull never
```

The deploy bundle includes `compose.yaml`, `.env.example`, the optional configuration script, documentation and an empty `media/` directory. It contains no credentials, database, operating-system ISO, vendor installer or product license. The image is tagged `ghcr.io/dasfunfzigste/gdeploy:0.9.2` when loaded, matching the release Compose file. The release page records its immutable digest. No registry login is needed for this path.

Open `http://YOUR_SERVER_IP:8000` and complete the first sign-in. Existing installations should follow [backup and update guidance](#check-stop-upgrade-and-recover) and retain their existing configuration and volume rather than extracting a new bundle over live files.

## Check, stop, upgrade and recover

For a Compose installation:

```sh
cd ~/GDeploy
sudo docker compose ps
sudo docker compose logs --tail=200 gdeploy
sudo docker compose stop
sudo docker compose start
```

For plain Docker, use `docker inspect gdeploy`, `docker logs gdeploy`, `docker stop gdeploy` and `docker start gdeploy` instead. Do not paste environment files or revealed credentials into support logs.

Common startup issues:

| Symptom | What to check |
| --- | --- |
| GitHub release is missing or returns 404 | Verify the repository URL and release version on the public Releases page. |
| A registry pull returns `denied` or `unauthorized` | Use the source build or load the release image archive. Registry-package access is separate from the public repository; neither alternative requires a registry login. |
| Docker reports permission denied | Reconnect after adding your user to the Docker group, or consistently use `sudo docker`. |
| Container exits or remains unhealthy | Read `docker compose logs`; verify Compose is at least 2.24 and any explicit credential settings are complete. Preserve the volume and restore original missing/damaged keys from backup. |
| `admin`/`admin` no longer signs in | After account setup, use the username and password you chose. Existing older installations retain their original credentials rather than adopting the new default. |
| Cannot find the OS ISO picker | Open Setup, then the OS installation media tab, or use `/#settings/media`. If the tab is absent, rebuild/restart the app from the updated source and refresh the browser. Check the app footer shows v0.3.1 or later. |
| ESXi datastore source is absent or cannot load | Check the app footer shows v0.4.0 or later, save and test the ESXi connection, and confirm certificate trust, `Datastore.Browse` and file download access. Refresh the listing after changing hosts. |
| ESXi ISO import fails | Check the selected file still exists, its publisher checksum matches, its size is at most 16 GiB, and the GDeploy data volume has room for the copy. Review the displayed TLS, permission or transfer error before retrying. The original datastore file and previous saved selection remain intact. |
| Media verification or preflight fails | Check the selected ISO in Setup, its publisher checksum, available disk space and UID 10001's read access to server files. Keep media used by queued/running jobs present and unchanged. |
| Splunk preflight reports a missing package such as `/media/splunk.tgz` | Open **Setup → Software packages** or follow **Configure Splunk package** from preflight. Upload or select your licensed Linux x86_64 `.tgz` with its publisher SHA-512 or verified SHA-256; vendor filenames are supported. Use **Return to deployment** and rerun preflight. UI selection needs no environment edit or restart. |
| Splunk package upload or verification fails | Check the package is at most 4 GiB, free space is sufficient, the publisher checksum matches, and the archive is Splunk Enterprise for Linux x86_64. Enter only the 128-character SHA-512 or 64-character SHA-256, without the filename from the checksum file. Use v0.8.1 or later for Splunk's `.sha512` value. Review the displayed archive error; a `.tgz` extension alone does not establish compatibility. The previous selection is retained. |
| A saved Splunk package cannot be deleted | Clear its saved selection or choose another package. Queued, running or cleaning jobs retain deletion protection for their own package snapshots. Only unused app uploads can be deleted; mounted server files remain outside this control. |
| FleetManager configuration does not appear before review | Update to v0.9.1 or later, refresh the browser and select the FleetManager role. After **Configure VMs**, its configuration step should appear even when defaults are already saved. Deployments without FleetManager keep the three-step flow. |
| FleetManager configuration is invalid or later preflight reports it changed | Return to **FleetManager configuration**, supply or retain the community string and valid product identity PEM, plus the online token or offline main package/dependencies, then choose **Save & continue**. Run preflight again from **Review & deploy**. Setup remains available for default settings and package management. |
| Offline FleetManager reports missing dependencies | Expand **View logs**, obtain the missing Ubuntu-compatible `.deb` dependencies from a trusted source and select them in Setup. Offline installation does not fetch repository packages. If the OS failed before SSH became available, check the regular live-server ISO contains the base packages. |
| FleetManager license or service validation fails | Use the customer product identity PEM, with matching leaf certificate/private key and valid dates. Confirm the Corelight entitlement, free guest space under `/var` and `/tmp`, and the `corelight-fleetd` service. Syntax validation alone does not prove the vendor license. |
| Media preparation reports permission denied or only a generic ISO error | Update to v0.5.0 or later, then open the deployment's **View logs**. These releases fix read-only extracted GRUB/manifest working files and use `/data/artifacts` for subprocess temporary files. Check the new diagnostic for the failing path/tool, plus `/data` free space and permissions. An older generic error alone does not establish which condition failed. |
| Cloud-init verification failed but the VM appears installed | Update to v0.7.1 or later and read the [readiness troubleshooting steps](#check-ubuntu-readiness-after-a-deployment-error). The fix separates stderr warnings from JSON and verifies clean installer-disabled states using independent guest checks. Keep a healthy existing VM; updating does not resume its failed job. |
| Data storage is full or appears smaller than the host disk | Check **Storage & saved ISOs** and run `docker compose exec gdeploy df -h /data /tmp`. The app sees the backing data filesystem, not all host disks. Delete eligible unused ISO copies or expand/migrate that backing storage; `/tmp` is a separate 256 MiB mount. |
| A saved ISO cannot be deleted | Use **Clear saved selection** or select another ISO if this is the current choice. Resolve any queued, running or cleaning deployment using it; clearing the default does not remove that protection. Only uploaded/ESXi-imported local copies can be deleted in Setup; server-mounted files and original ESXi ISOs are outside this control. |
| ISO upload is rejected or interrupted | Browser uploads are limited to 16 GiB. Check the data volume's free space and, if using a reverse proxy, its request-size and upload-timeout settings. The prior media selection remains saved. |
| ESXi connection fails | Check the hostname, port 443, credentials and API license/permissions. For certificate errors, retrieve the certificate in Setup → ESXi connection and verify its fingerprint and dates. A renewed certificate requires a new approval. |
| A finished deployment is missing from history | Enable **Show hidden** and check the search/status filters. Select **Restore** to return the record to the default list. Hiding retains the VM, credentials and logs. |
| Remote browser cannot connect | Check `docker compose port gdeploy 8000` and the server address/port in your URL. Existing `.env` overrides remain in effect; a `127.0.0.1` override accepts only local connections. Edit that value to `0.0.0.0` or a LAN address and recreate the container, or use the optional SSH tunnel. |

Version **0.9.2** changes the default installation guide and release presentation for public repository access. It does not change the application or stored data. Use the same source clone and preserve its existing data volume, environment files, media and encryption key when rebuilding.

Version **0.9.1** places FleetManager configuration inside the deployment wizard before review and preflight. It reuses the v0.9.0 settings API and saved defaults; no license/package re-upload or data migration is required. Backend installation, queued snapshots and existing VMs are unchanged. Preserve the existing data volume and encryption configuration; the v0.9.0 backup and compatibility requirements below still apply.

Version **0.9.0** adds the FleetManager VM role, encrypted configuration/license records and managed offline packages under `/data/fleetmanager-packages`. Include these in complete volume backups and preserve the encryption key; mounted packages and guest data require separate backups. Existing roles and their saved settings remain supported. Finish or resolve FleetManager jobs before rolling back, because older releases do not support the new role or its settings. Updating GDeploy does not upgrade Fleet Manager on an existing VM, and rollback cannot restore deleted files or undo guest changes.

Version **0.8.1** accepts Splunk's publisher SHA-512 in Setup alongside SHA-256. Existing saved SHA-256 selections and queued snapshots remain valid; a previously verified package does not need to be uploaded again. GDeploy retains SHA-256 internally for queued snapshots and guest transfer verification. OS ISO checksums and the legacy `GDEPLOY_SPLUNK_SHA256` variable are unchanged. Preserve the data volume and configuration through the update; the v0.8.0 backup requirements below still apply.

Version **0.8.0** adds persistent Splunk package selections, uploads under `/data/packages`, and package snapshots for new deployments. Back up these uploads with the complete data volume. Existing jobs without a snapshot continue using their environment package configuration. Earlier releases do not understand saved package selections or snapshots; finish queued work before rolling back and supply a verified package through that release's environment settings. Preserve your backup, encryption configuration and mounted media. Rolling back cannot restore a deleted upload or undo changes inside a VM.

Version **0.7.1** repairs cloud-init status parsing and verifies clean installer-disabled guests with independent readiness checks. It does not change the database schema or ISO format, replace your saved media, or resume older failed records. Existing VMs and their credentials remain intact; the earlier rollback requirements below still apply.

Version **0.6.0** adds persistent, reversible history visibility and displays every selected VM's independent configuration together. **Setup** appears first in navigation. Accounts, credentials, logs, media and resource ownership remain intact. The upgrade adds a database column; older versions may read the records but cannot create deployments against the migrated schema. Back up the complete data volume before upgrading and restore the matching pre-upgrade backup if rolling back. Inspect ESXi state before resuming work because restoring GDeploy's backup does not undo VM changes. Hiding a failed record does not resolve the failure or change the running VM. That release left the cloud-init readiness check and deployment specification format unchanged.

Version **0.5.0** adds expanded deployment diagnostics, data-filesystem usage, **Clear saved selection** and protected deletion of unused saved ISO copies, and fixes private installation-media work files that retain read-only permissions. Upgrading keeps accounts, encryption configuration, trust approvals, media selections and history. It does not enlarge or replace the data volume. A deleted ISO copy is not restored by rolling the app back; restore it from backup or import/upload it again if needed. Existing generic errors remain generic because their discarded output cannot be recovered.

Version **0.4.0** adds ESXi datastore media browsing and import. Saved credentials, certificate approvals and existing uploaded/mounted media keep working. Include imported copies in complete data-volume backups. Versions before **0.4.0** do not understand media selections with an ESXi origin; finish or resolve active jobs and select a verified uploaded or server-mounted ISO before rolling back. Preserve the data volume and existing environment keys.

Version **0.3.1** adds explicit Setup tabs, versioned browser assets and a visible release number. After updating the container, refresh the page and check the footer version. The database format and saved configuration are unchanged from 0.3.0.

Version **0.3.0** adds saved OS ISO selection and persistent browser uploads. Existing environment media settings remain a fallback until a selection is saved in Setup. The new `GDEPLOY_OS_ISO` and `GDEPLOY_OS_SHA256` names take precedence over their legacy `GDEPLOY_UBUNTU_*` equivalents when both are supplied. Include uploaded ISOs in the complete data-volume backup and preserve server-mounted `media/` files. Versions before **0.3.0** ignore Setup's media selection and the new environment names; supply the older version's verified media path/checksum settings before a rollback. Finish or resolve active jobs before changing versions.

Version **0.2.0** adds an ESXi certificate-trust table without changing existing credentials or encryption keys. Back up the complete data volume to preserve approved certificates. If rolling back below **0.2.0**, older versions ignore these approvals and use their previous TLS settings, including any saved `verify_tls=false`. Review those settings and ensure certificate verification is enabled and trusts the intended host before using the older version. The approvals do not become CA certificates in older releases.

Version **0.1.3** adds a persistent administrator account record to the existing database. On the first upgraded start, the record is initialized once from the existing credentials; later starts keep the database account. Existing random/custom credentials are preserved, and completed account setup is not overwritten by bootstrap or environment values.

Before upgrading, finish or resolve active deployments, read the new release's changelog and compatibility notes, then stop the app. Take a consistent backup of the **complete data volume**, including **`/data/bootstrap.json` for automatic installations**, and any existing **`.env`/`docker.env` with its matching encryption key**, plus your Compose configuration and media. Keep the backup somewhere outside the live volume.

For a source installation, update from the **same clone** so Compose reuses its existing project and data volume:

```sh
cd ~/GDeploy
git pull --ff-only
sudo docker compose up -d --build --wait
```

Keep the existing `.env`, `media/` directory, bootstrap files and data volume. Do not rerun the initial credential generator. Refresh the browser and check the app version and health. If Git refuses a fast-forward because you have local edits, review those edits before resolving the checkout; do not discard environment files or live data.

**Changing launch methods or directories:** the source Compose volume may be named `gdeploy_gdeploy-data`, while a release bundle or plain Docker example uses `gdeploy-data`. Inspect the current container's `/data` mount before switching. Reuse that exact existing volume rather than creating an empty replacement. Release Compose supports `GDEPLOY_DATA_VOLUME=YOUR_EXISTING_VOLUME_NAME`; plain Docker uses it in `--mount source=...,target=/data`. Preserve the original `.env`/`docker.env` or automatic `bootstrap.json`, and the mounted media directory. Never start two app containers against one volume.

For an installation using a release image archive, download and verify the new image/bundle in a separate directory. Load the new image before recreating the existing container with its original configuration and volume; use `--pull never` to keep the launch independent of registry access. For plain Docker, stop and remove only the old container, then repeat its existing `docker run` options with the new locally available image. Rebuilding or replacing the container does not update software inside existing guest VMs.

Do not blindly roll back below **0.1.3** after choosing your account: older versions ignore the database administrator record and use the old bootstrap/environment login, which may be `admin`/`admin`. Restore a matching backup and review the login configuration before starting an older version. Automatic bootstrap was introduced in version **0.1.2**; earlier versions also require explicit environment credentials. Rollback is release-dependent: if an upgrade changes stored data incompatibly, changing the image tag alone is insufficient. Restore the matching backup and encryption key according to that release's notes. Restoring GDeploy's database does **not** undo ESXi VM changes or restore guest application data; inspect ESXi state before resuming work.

Do not run `docker compose down -v` or remove `gdeploy-data` unless you intend to erase GDeploy's saved history and credentials. Run one app container with one worker per data volume.
