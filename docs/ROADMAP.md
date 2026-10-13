# Planned additions

The sidebar's **Preview** section lists future work. These entries are reminders and are not active features or deployment choices.

## VM Manager

A single screen for viewing and connecting to VMs on the configured hypervisor, starting with GDeploy's standalone ESXi integration.

Planned scope:

- List the hypervisor's VMs, including machines created outside GDeploy, with their names and power states.
- Show overall host performance and per-VM CPU, memory, storage and network information at a glance, using available hypervisor metrics.
- Display guest IP addresses and each NIC's MAC address and connected port group. Identify unavailable guest information clearly; IP reporting depends on the hypervisor and guest tools.
- Open an embedded SSH terminal for Linux VM administration from the same screen. GDeploy must be able to reach the guest's SSH service.
- Use GDeploy's encrypted saved guest credentials or automation SSH key for associated deployments. When suitable credentials are unavailable, prompt the administrator for an SSH username and password or private key. Any credentials retained by GDeploy must receive the same encryption and access controls as existing guest credentials.

The first connection method is SSH. Full hypervisor consoles and VM lifecycle controls can be considered separately when this feature is designed.

## Alma Linux

Add Alma Linux as a future operating-system deployment option.

## Proxmox

Add Proxmox as a future hypervisor integration.
