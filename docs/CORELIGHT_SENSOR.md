# Deploy Corelight Software Sensor

Select **Corelight Software Sensor** in **New deployment** to create a dedicated Ubuntu Server **24.04 LTS amd64** VM, install the sensor from Corelight's online repository and pair it with an existing Fleet Manager. GDeploy requires a sensor license key before preflight. This release supports Ubuntu 24.04; Debian, other Linux distributions and offline sensor installation are not deployment options.

The implementation follows **Corelight Sensor Installation 29.2.1**, PDF pages **120–130** (printed pages 118–128), and the hardware requirements on PDF pages **192–194**. The vendor guide is a reference, not a bundled license or installer. Use packages and licenses covered by your Corelight entitlement.

## Prepare Fleet Manager and your networks

1. Have a working Fleet Manager instance reachable from the new sensor's management network. Keep Fleet Manager at a version that supports the sensor release. Creating a FleetManager VM in the same GDeploy job does not create the sensor's pairing record for you; the pairing information must be available before preflight.
2. In Fleet Manager, open **Sensors → + New Sensor**, enter a unique sensor name and save. Copy the values from **New Tethered Sensor Setup → corelightctl**: pairing token, URL and server SSL name. The token is unique and is not shown again. Keep it private.
3. Prepare a **management port group** with a static IPv4 address or a DHCP reservation, DNS and outbound access to Ubuntu/Corelight repositories. GDeploy must reach the guest over SSH TCP **22**. The sensor must reach Fleet Manager at its supplied pairing URL, normally HTTPS TCP **1443**.
4. Prepare a **monitoring port group** that receives the traffic you want to inspect through your tap, SPAN or virtual mirroring configuration. GDeploy connects the second VMXNET3 adapter to this port group with **no IP address, DHCP, IPv6 router advertisements or default route**. It does not change ESXi port-group security, switch mirroring or physical tap configuration. Configure promiscuous-mode handling where your monitoring design requires it.
5. Allow administrators to reach the sensor API over HTTPS TCP **443**. The sensor's Kubernetes CNI manages guest firewall rules; it must not compete with UFW or firewalld. GDeploy prepares this dedicated sensor guest accordingly and retains the vendor's default SSH access. Upstream network firewalls are managed separately.

The repository token for **Software Sensor** comes from **Corelight Customer Portal → Downloads → Software Sensor**. It is separate from the FleetManager repository token. Both the guest's Ubuntu mirrors and `pkgrepos.corelight.cloud` (including HTTPS download destinations returned by it) must be reachable.

## VM resources

The default is **4 vCPUs, 16 GiB RAM and a 600 GiB disk**. GDeploy accepts at least 4 vCPUs, 16 GiB RAM and a 550 GiB disk, leaving room for Ubuntu around the vendor's **500 GB free-space requirement**. Storage allocation and free guest space are checked separately. Use **SSD or better** storage and provide dedicated CPU/memory capacity; do not rely on CPU over-subscription, ballooning or memory overcommit.

The guest processor must expose **x86-64-v3 or newer** capabilities. An ESXi host's CPU model alone does not prove those features reach the VM, especially with compatibility masks. GDeploy checks guest CPU capabilities before installing the sensor. Machine-learning/anomaly features require at least **8 cores and 32 GB RAM**, and greater traffic volumes may need more resources. Vendor throughput examples are sizing guidance, not a performance guarantee for your host.

Use the supported Ubuntu 24.04 live-server ISO in **Setup → OS installation media**. The sensor role selects the minimal Ubuntu installation. Other selected application roles keep their existing OS installation behavior.

## Configure and deploy

1. Select **New deployment → Corelight Software Sensor** and continue to **Configure VMs**.
2. Enter its VM name, resources and datastore. Select the management port group and static address, or confirm that DHCP has a reservation. Select the second adapter's monitoring port group. Choose the datastore and port groups appropriate to your SSD backing and monitoring topology.
3. Continue to **Sensor configuration**. Provide the Software Sensor repository token, Fleet Manager community string, **sensor license key**, pairing URL and server SSL name. A FleetManager product identity `.pem` is not the sensor license key.
4. Enter the unique **pairing token** from the new Fleet sensor record. Common settings can be retained in **Setup → Software packages → Corelight Software Sensor**, but this token is supplied separately for each deployment. It is never reused as a Setup default.
5. Choose the IPv4 network permitted to use the sensor API on TCP 443. The default `0.0.0.0/0` allows any IPv4 source that can reach the VM; enter your administration subnet to restrict it.
6. Save and continue to review. Run preflight and deploy, or use the existing option to deploy automatically when all checks pass. Configuration and pairing inputs must be complete before preflight. If FleetManager is also selected, its own configuration step appears before the sensor step.

GDeploy encrypts saved repository/community/license secrets and the per-job pairing token. Ordinary settings/history responses contain saved-state flags and public configuration, not raw credentials. Blank secret fields retain existing common settings; clearing Setup removes defaults for future jobs without changing deployed sensors. Each queued job keeps its encrypted snapshot.

GDeploy prevents a pairing token already assigned to another job from being reused, including concurrent requests. A failed or stopped job may already have used its token at Fleet Manager, so create a fresh Fleet sensor record for a replacement. Token tracking stays in the GDeploy data volume and must be preserved with the encryption key when upgrading or restoring backups.

## What GDeploy installs

GDeploy creates two VMXNET3 adapters and reads their actual MAC addresses from ESXi before preparing the sensor ISO. The management and monitoring roles are matched by MAC, so Linux interface ordering cannot apply management DHCP or a static address to the monitoring adapter. The VM and installation media are recorded for the normal Stop, Hide and Delete & redeploy controls.

The initial VM has disk-only boot configured. Once the ISO is ready, GDeploy adds the installation drive and its boot entry together before powering on. The disk stays first: it falls through to the ISO while empty and boots the installed OS after reboot. If an older release fails at VM creation with `configSpec.bootOptions.bootOrder`, update to **v0.15.2 or later** and use the failed job's recovery flow. This error occurs before Ubuntu or the sensor installer starts.

After Ubuntu and SSH are ready, the guest installer validates resources and networking, configures the signed **sensor-stable** apt repository and installs **corelightctl** and **corelight-sensor**, with their dependencies. It uses the latest repository packages; the installed versions are recorded. Repository authentication is stored in a root-only apt file and remains available for deliberate future administration.

The installer prepares the host with `corelightctl sensor prepare`, initializes `/etc/corelight/corelightctl.yaml` and supplies the license, community string, management/monitoring interfaces, Fleet pairing values and API access rule. It runs `corelightctl sensor deploy -v`, then checks `corelightctl sensor status`. The sensitive configuration is root-readable and diagnostic output is redacted.

Completion requires healthy sensor services, including **sensor-core** and **connection-manager**. An unlicensed sensor or failed Fleet pairing is not reported as ready. The guide's specific Suricata warning about having no rules can be reported as a warning; unrelated failures still stop the job. Service checks do not prove mirrored packets are arriving or that your Fleet policy, log export, detection rules and retention are correct.

## After deployment and recovery

- Open the deployment's **Credentials** for the Ubuntu `gdeploy` SSH account and sensor access details. Confirm the sensor appears in Fleet Manager, apply the appropriate Fleet policy and configure export destinations/rules using Corelight's procedures.
- Verify traffic arrives on the monitoring interface and reaches the intended analysis/export pipeline. A second NIC connected to a normal port group does not create a traffic mirror by itself.
- Expand **Deployment logs** for installation and readiness failures. Useful guest commands are `sudo corelightctl sensor status`, `ip -br address` and `ip route`. Do not share `corelightctl.yaml`, repository auth files, license keys, pairing tokens or community strings in support logs.
- **Stop deployment** stops GDeploy's automation while preserving created VMs; work already running inside the guest may continue. Hiding a job preserves its VM, logs and credentials.
- **Delete & redeploy** permanently replaces the job's VMs/disks. For sensor jobs it requires a **fresh pairing token**, validated before cleanup; the previous token is never silently reused. It does not delete the old sensor record from Fleet Manager. Manage those Fleet records separately.
- Updating GDeploy does not upgrade or reconfigure existing sensors. Follow Corelight's supported backup and upgrade procedure for the sensor software, and keep the complete GDeploy data volume/encryption key and guest backups.

Automated tests cover the configuration flow, secret handling, VM/NIC construction and installer behavior. A full licensed deployment on your ESXi host, actual Fleet pairing and real mirrored traffic require the [lab acceptance checks](LAB_VALIDATION.md#corelight-software-sensor).
