# First deployment acceptance

Use an isolated datastore/network with disposable VMs. This checklist verifies the parts that unit tests cannot exercise without ESXi and installation media.

1. Supply and verify Ubuntu 24.04 amd64 live-server media. Confirm the ESXi account/license supports API writes and the app trusts its certificate.
2. Deploy a plain Ubuntu VM using DHCP. Observe EFI falling back from the empty disk to the installation CD. Confirm there are no keyboard prompts, Ubuntu reboots to disk, VMware Tools reports its IP, cloud-init finishes and the ISO is detached/removed.
3. Reveal credentials, sign in through SSH as `gdeploy`, verify sudo works, and check the recorded SSH host key. Restart GDeploy and confirm history and credentials remain available.
4. Repeat using a reserved static IPv4 address. Confirm address/prefix, default route and DNS in the guest.
5. Deploy Elasticsearch and Kibana as separate VMs. Confirm the same package version on both, Elasticsearch HTTPS authentication, Kibana HTTPS login and available status. Verify Kibana uses its service account token and CA validation. Restart both guests and confirm the connection recovers.
6. Deploy Splunk using your supported Ubuntu-compatible Enterprise x86_64 package. Accept its license explicitly. Confirm HTTPS web sign-in using the shown admin password and that the Splunkd systemd service survives reboot.
7. Deliberately select insufficient resources or unavailable media and verify preflight blocks creation with a useful explanation.
8. Interrupt an OS installation by restarting GDeploy. Confirm it is marked interrupted rather than silently retried. Run delete & redeploy with typed confirmation; confirm only that deployment's VMs/disks/media disappear and the replacement receives new credentials.
9. Remove cleanup permission and repeat recovery. Confirm cleanup failure prevents replacement. Restore permission and confirm retry succeeds.
10. Verify unauthenticated API requests are rejected, credential reveals are audited, and passwords do not appear in ordinary history, logs, browser storage or Git.

Record your exact ESXi build, Ubuntu ISO checksum, Splunk version, Elastic version and any host-specific findings before expanding use.
