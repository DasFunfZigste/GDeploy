# Deploy and test AlmaLinux 10

GDeploy v0.16.0 adds **AlmaLinux 10 x86_64** installation to every deployment role. The Docker host can remain Ubuntu; this selection controls the new guest VMs. Ubuntu Server 24.04 remains available and stays the default for older settings and jobs.

| Role | Alma installation path | Verify in your lab |
| --- | --- | --- |
| OS only | Full DVD + Anaconda/Kickstart | Unattended boot, network, SSH, sudo and VMware Tools |
| Splunk | Your verified Linux x86_64 `.tgz` | Package compatibility, HTTPS sign-in and service after reboot |
| Elasticsearch | Elastic 9.x RPM repository | Authenticated HTTPS and installed version |
| Kibana | Elastic RPM matching Elasticsearch exactly | Verified TLS, service account connection and browser sign-in |
| FleetManager | Corelight `fleet-stable/rpm_any` | **Experimental:** RPM dependencies, license, GUI 443 and sensor listener 1443 |
| Software Sensor | Corelight `sensor-stable/el/9` and `sensor-stable/rpm_any` | **Experimental:** EL9 RPM compatibility, license, Fleet pairing and mirrored traffic |

**Corelight compatibility:** The supplied Corelight 29.2 guides support Alma/RHEL **8 and 9**, not Alma **10**. GDeploy offers the requested Alma 10 lab path using those documented repositories; it does not claim vendor certification or create an EL10 repository. Dependency, resource, service and pairing failures remain fatal and appear in deployment logs. Keep a working Ubuntu deployment available while testing.

## Select the installer

1. Update your existing clone using `git pull --ff-only` followed by `sudo docker compose up -d --build --wait`, then refresh the browser.
2. Open **Setup → OS installation media** and choose **AlmaLinux 10 · Kickstart** in **OS installer**.
3. Upload or select the original **AlmaLinux 10 x86_64 full DVD ISO**, from your computer, GDeploy's server media directory or an ESXi datastore. Enter the publisher's verified **SHA-256** and save.
4. Open **New deployment** and choose your roles. Every VM in that job uses the saved Alma installer; names, resources, datastore and port groups remain separate per VM.
5. Complete Splunk/license or Corelight configuration as applicable, then run preflight. FleetManager's version dropdown reads RPM choices for Alma; Ubuntu's Debian version choice is stored separately.

Obtain the ISO and checksum through [AlmaLinux's download page](https://almalinux.org/get-almalinux/) and verify them using the [official ISO verification instructions](https://wiki.almalinux.org/documentation/installation-guide.html). **Boot, minimal, netinstall, live and `x86_64_v2` ISOs are not supported by this installer.** GDeploy checks the selected file's internal release, architecture, kernel/initrd, installer stage2 and local BaseOS/AppStream repository metadata as well as the publisher checksum. Renaming an incompatible ISO does not make it compatible.

The guest CPU must expose **x86-64-v3** features. An ESXi CPU compatibility setting can mask features that are present on the physical host; confirm the features available to the VM. GDeploy uses ESXi 8.0 U3's RHEL-family guest identifier with EFI because that host version predates an Alma/RHEL 10 identifier. This is not VMware certification of Alma 10.

Keep room on GDeploy's data filesystem for the DVD copy and a temporary remastered DVD. Preflight estimates one build at the source size plus 20% and 512 MiB. The source is retained; deployment work uses `/data/artifacts` on the persistent data volume. No additional Docker disk-size option is needed when that filesystem has enough space. Application installation still needs outbound DNS/HTTPS and OS/vendor repository access.

## What the installer sets up

GDeploy preserves the source DVD's BIOS/UEFI metadata and label, adds a private Kickstart file and updates the boot menu for unattended Anaconda installation. Initial OS packages come from the full DVD. The VM boots its empty disk first, falls through to the DVD, and boots the installed disk after reboot.

The installed guest has OpenSSH Server, open-vm-tools, Python 3, NetworkManager, password-protected sudo for **`gdeploy`**, and GDeploy's separate automation key plus any administrator public keys captured when the job was queued. Root is locked and root SSH login is disabled. The installer generates SSH host keys and restores their SELinux contexts before validating sshd. Credentials are available in the deployment's authenticated **Credentials** view.

The installer creates the required firmware/boot partitions, a bounded 2 GiB swap partition and an XFS root that grows across the remaining disk, with no separate `/home` or automatic root-size cap. This leaves the sensor's 600 GiB disk available for its 500 GB free-space requirement. SELinux remains **enforcing**. Firewalld starts with SSH allowed; application rules are added to the management interface's zone. A dedicated Software Sensor disables competing host firewalls before its Kubernetes CNI takes over.

The sensor's two adapters are matched by their distinct ESXi MACs. Management becomes **`gdeploymgmt`** with static or reserved DHCP IPv4. Monitoring becomes **`gdeploymon`**, with IPv4 and IPv6 disabled and no default route. NetworkManager profiles and interface names persist across reboot. Configure the actual tap/SPAN/mirroring source separately.

Alma readiness checks use the protected root-owned **`/var/lib/gdeploy/os-install-complete.json`** Kickstart completion marker, the installed Alma 10 disk root, hostname, sudo and running **`sshd`/`vmtoolsd`**. Alma does not depend on Ubuntu cloud-init completion. Application installation begins only when the OS checks pass.

## Test each role

Use disposable VM names, an isolated datastore/network and valid vendor entitlements. Record the ISO filename/checksum, ESXi build, resources, network, package versions and expanded deployment logs for each case; omit tokens, licenses and passwords from shared results.

1. **OS only:** Test DHCP and static IPv4. Confirm unattended installation, expected hostname/address/default route/DNS, SSH as `gdeploy`, sudo and VMware Tools reporting the management address. Reboot and repeat. The deployment should complete and remove its installation ISO.
2. **Elasticsearch + Kibana:** Use separate VM names and resources. Confirm matching RPM versions, Elasticsearch authenticated HTTPS, Kibana sign-in and an available Elasticsearch connection with CA verification. Reboot both VMs and confirm reconnection.
3. **Splunk:** Use your Alma-compatible Linux x86_64 package and explicit license acceptance. Check the installed version, generated `admin` sign-in, HTTPS Web endpoint and `Splunkd` service after reboot. Archive validation alone cannot establish Alma compatibility for a licensed version.
4. **FleetManager:** Load RPM versions before preflight and test an exact selection as well as **Latest available**. Confirm `/etc/corelight-fleetd.pem` protection, the `corelight-fleetd` service, browser sign-in and a required first password change. Test GUI TCP 443 and actual sensor connectivity on TCP 1443 from the intended networks.
5. **Software Sensor:** Use at least 4 vCPUs, 16 GiB RAM and the 600 GiB SSD-backed default disk, with at least 500 GB free in the guest. Use a new Fleet sensor record and pairing token. Confirm `gdeploymgmt`/`gdeploymon` addresses and routes, healthy `sensor-core`/`connection-manager`, appearance in Fleet and traffic arriving at the monitoring NIC. Reboot and repeat service/pairing/capture checks.

Useful OS checks inside an Alma VM:

```sh
cat /etc/os-release
hostname
ip -br address
ip -4 route
findmnt -n -o SOURCE,FSTYPE /
sudo systemctl --no-pager status sshd vmtoolsd
sudo stat -c '%U %a %n' /var/lib/gdeploy/os-install-complete.json
sudo cat /var/lib/gdeploy/os-install-complete.json
sudo firewall-cmd --get-active-zones
getenforce
```

For applications use `rpm -q elasticsearch kibana corelight-fleet corelightctl corelight-sensor`, selecting packages present on that VM. For a sensor, also use `sudo corelightctl sensor status`, `ip -br address show gdeploymon` and `ip route show dev gdeploymon`. Full VM console boot, vendor repository, Fleet and traffic checks remain lab work; synthetic media tests establish remastering behavior only.

## Preserve existing deployments and recovery

Finish or resolve active jobs before an upgrade or rollback. Back up the **complete data volume with its original encryption key**, uploaded/imported media, separate server media and guest application data. Keep `.env`, bootstrap files and the existing Docker volume. Updating GDeploy neither resumes failed work nor upgrades software already on a VM.

Each queued job retains its ISO, checksum and installer profile. Changing Setup to Alma does not switch an existing Ubuntu job; changing back to Ubuntu does not switch a queued Alma job. A draft configured for a different Setup selection must be reopened and checked again. Credentials, software snapshots, SSH-key snapshots, pairing-token records and resource ownership remain encrypted/preserved as before.

Versions before v0.16.0 cannot provision Alma jobs or interpret the new Alma/RPM settings. Resolve those jobs and select a verified Ubuntu ISO before rolling back; retain the matching complete backup and encryption configuration. Rolling GDeploy back cannot undo guest installations, restore deleted VM disks or unconsume a Fleet pairing token. The existing **Delete & redeploy** flow replaces VMs/disks and still requires a fresh sensor token whenever sensor installation may have started.

Installer references: [AlmaLinux Kickstart guide](https://wiki.almalinux.org/documentation/kickstart-guide.html), [RHEL 10 boot-menu layout](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/customizing_anaconda/customizing-the-boot-menu), and [RHEL 10 Kickstart commands](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/automatically_installing_rhel/kickstart-commands-and-options-reference).
