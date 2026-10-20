# Install GDeploy on Ubuntu 24.04

This guide accompanies **GDeploy 0.18.0**. Use an Ubuntu Server **24.04 LTS, amd64/x86_64** host and a user that can run `sudo`. The commands install and update the current default branch of the public repository. GDeploy provisions VMs on standalone ESXi 8.0 Update 3 or Proxmox VE 9.2.

## Install Docker and Git

If Git and Docker with Compose are already working, skip this step.

```sh
sudo apt update
sudo apt install -y git docker.io docker-compose-v2 docker-buildx
sudo systemctl enable --now docker
```

## Download and start GDeploy

```sh
cd ~
git clone https://github.com/DasFunfZigste/GDeploy.git
cd GDeploy
sudo docker compose up -d --build --wait
```

This builds the Docker image locally and starts the container. No Docker registry login, GitHub credential linking or initial `.env` file is required.

## Open the app

Browse to **`http://YOUR_SERVER_IP:8000`**, replacing `YOUR_SERVER_IP` with the Ubuntu server's LAN address. The container is exposed to the LAN by default.

On a fresh installation, sign in with **username `admin` and password `admin`**. Change these credentials when prompted, then sign in again with your new account.

Open **Setup → Virtualization hosts** to add an ESXi or Proxmox host, retrieve/approve its certificate if needed, and save an optional default network. Then save installation media for each OS you want to use: **Ubuntu Server 24.04** needs an amd64 live-server ISO; **AlmaLinux 10** needs the standard x86_64 full DVD ISO. Both saved selections remain available. Then open **New deployment** and choose your **Target virtualization host** and Ubuntu or Alma in **Operating system** before continuing. Alma needs x86-64-v3 CPU features; its Corelight roles are experimental. See the [Alma setup and testing guide](https://github.com/DasFunfZigste/GDeploy/blob/v0.18.0/docs/ALMALINUX.md) for details.

For Proxmox API tokens, ISO storage and bridges, see the [Proxmox setup guide](https://github.com/DasFunfZigste/GDeploy/blob/v0.18.0/docs/PROXMOX.md).

## Update GDeploy

Finish or resolve active deployments, then run these commands from the **same clone**:

```sh
cd ~/GDeploy
git pull --ff-only
sudo docker compose up -d --build --wait
```

This rebuilds the image and replaces the container while preserving the existing data volume, account, settings and deployment history. Keep your existing `.env` and `media/` directory. Refresh the browser afterward.

For logs, media/software setup, networking or backups, use the [operations reference](https://github.com/DasFunfZigste/GDeploy/blob/v0.18.0/docs/OPERATIONS.md). Release versions and changelogs are on [GitHub Releases](https://github.com/DasFunfZigste/GDeploy/releases).
