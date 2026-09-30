# First deployment acceptance

Use an isolated datastore/network with disposable VMs. This checklist verifies the parts that unit tests cannot exercise without ESXi and installation media.

1. Supply and verify Ubuntu 24.04 amd64 live-server media. Confirm the ESXi account/license supports API writes and complete the certificate-trust checks below.
2. Deploy an **OS only** VM using DHCP. Observe EFI falling back from the empty disk to the installation CD. Confirm there are no keyboard prompts, Ubuntu reboots to disk, VMware Tools reports its IP, cloud-init finishes and the deployment's ISO is detached/removed.
3. Reveal credentials, sign in through SSH as `gdeploy`, verify sudo works, and check the recorded SSH host key. Restart GDeploy and confirm history and credentials remain available.
4. Repeat using a reserved static IPv4 address. Confirm address/prefix, default route and DNS in the guest.
5. Deploy Elasticsearch and Kibana as separate VMs. Confirm the same package version on both, Elasticsearch HTTPS authentication, Kibana HTTPS login and available status. Verify Kibana uses its service account token and CA validation. Restart both guests and confirm the connection recovers.
6. Deploy Splunk using your supported Ubuntu-compatible Enterprise x86_64 package. Accept its license explicitly. Confirm HTTPS web sign-in using the shown admin password and that the Splunkd systemd service survives reboot.
7. Deliberately select insufficient resources or unavailable media and verify preflight blocks creation with a useful explanation.
8. Interrupt an OS installation by restarting GDeploy. Confirm it is marked interrupted rather than silently retried. Run delete & redeploy with typed confirmation; confirm only that deployment's VMs/disks/media disappear and the replacement receives new credentials.
9. Remove cleanup permission and repeat recovery. Confirm cleanup failure prevents replacement. Restore permission and confirm retry succeeds.
10. Verify unauthenticated API requests are rejected, credential reveals are audited, and passwords do not appear in ordinary history, logs, browser storage or Git.

Record your exact ESXi build, Ubuntu ISO checksum, Splunk version, Elastic version and any host-specific findings before expanding use.

## Setup and OS installation media

1. Open **Setup → OS installation media** and test all three sources: **ESXi datastore**, browser upload, and GDeploy server media. For the ESXi source, complete the datastore checks below. For each source, supply the publisher's verified SHA-256 checksum. A wrong checksum must leave the previous selection unchanged.
2. Recreate the app container with the same data volume. Confirm the saved ISO, uploaded/imported copies, administrator account and ESXi trust remain available.
3. Queue a deployment with one ISO, then change the Setup selection. Confirm the queued deployment keeps its original source. Replacing or deleting that source must stop the deployment before VM creation.
4. Interrupt a browser upload and restart the container during another upload. Confirm incomplete upload files are removed and previously saved media remains available. Use a real multi-GiB installer to check transfer progress, disk capacity and any reverse proxy upload limits.

## ESXi datastore media checks

Use disposable source files for rename, replacement and permission tests. Record the original ISO's SHA-256 before starting; the application must preserve it through import, deployment and cleanup.

1. Save and test the standalone ESXi connection, then choose **ESXi datastore** in the media tab. Browse the datastore root and nested folders, including names containing spaces, and select a supported ISO. Confirm only the intended host's accessible storage is listed and that the saved host and datastore/path are clearly identified.
2. Import with the publisher's verified SHA-256. Confirm a retained local copy appears under `/data/media`, the saved selection is ready, and its bytes match the expected checksum. Verify the original datastore ISO still exists with the same checksum. Deploy an OS-only VM from that selection and confirm remastering and guest installation complete; only the deployment-specific ISO should be removed afterwards.
3. Restart/recreate GDeploy using the same volume. Confirm the imported copy and origin details remain available. Rename or remove the disposable original after a completed import; selecting the retained copy and deploying from it must still work without another source download.
4. Select an ISO in the browser, then rename/delete it on ESXi before importing. Confirm a useful missing-file error, no newly saved selection, and no retained partial local copy. Repeat by replacing the ISO before or during copying; a mismatched publisher checksum or interrupted transfer must fail without replacing the previous media choice.
5. Revoke `Datastore.Browse`, then test browsing. Restore it, revoke file download access, and test an import from an already loaded listing. Confirm each failure is displayed clearly and preserves prior media. Restore permissions and verify retry succeeds without changing the source file.
6. Repeat datastore browse and import with an approved certificate, ordinary trusted-CA verification, removed approval, a replaced certificate and invalid validity dates. Fresh connections must follow the certificate rules below; they must not bypass TLS checks to complete an import.
7. Browse host A, then change the saved ESXi connection to host B before importing the selected ISO. Confirm the stale selection is blocked and browsing must be refreshed. Verify it cannot silently fetch the same path from host B.
8. Exercise a real large ISO, an import exceeding 16 GiB, insufficient GDeploy-volume space and a connection interruption. Confirm clear failures preserve the previous choice and original datastore file. Restart during another import; confirm incomplete local copies are cleaned up while registered media is kept. Record transfer time and free-space requirements separately from guest-installation time.

## ESXi certificate-trust checks

Use a disposable ESXi lab endpoint for certificate replacement and invalid-date tests.

1. Enter the ESXi hostname/IP in **Setup → ESXi connection** and retrieve its certificate. Verify the displayed subject, issuer, SHA-256 fingerprint, validity dates and DNS/IP names against the host certificate through an independently trusted management session. Retrieving alone must not approve it.
2. Approve the verified certificate, save credentials and test inventory. Exercise an ISO download through the datastore browser and an ISO upload during deployment, confirming API and both transfer directions work with the approved certificate without CA-file or environment edits. If using an IP absent from the SAN names, confirm explicit approval works for that exact endpoint.
3. Restart/recreate GDeploy with the same data volume and verify that the approval remains. Confirm another hostname/IP alias does not inherit the approval automatically.
4. Replace the lab host certificate with a different currently valid certificate. Confirm new API and datastore download/upload connections block until the replacement is retrieved, compared with the trusted source and explicitly approved. An already-open connection may finish; use fresh connections for this check.
5. Test expired and not-yet-valid lab certificates and verify neither can establish approved connections.
6. Remove the endpoint's approval and confirm normal system/private CA and hostname verification resumes. A CA-trusted, hostname-matching certificate should connect; a self-signed certificate outside that trust store should fail until explicitly approved again.
7. Confirm authenticated administrative access is required for retrieval/trust/removal and that the certificate workflow does not expose ESXi passwords in its responses or logs.
