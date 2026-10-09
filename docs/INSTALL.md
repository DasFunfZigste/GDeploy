# Install GDeploy on Ubuntu 24.04

This guide accompanies **GDeploy 0.15.2**. Use an Ubuntu Server **24.04 LTS, amd64/x86_64** host and a user that can run `sudo`. The commands install and update the current default branch of the public repository. GDeploy provisions VMs on standalone ESXi 8.0 Update 3.

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

Open **Setup** to configure the ESXi connection and OS installation media. The supported guest installer is Ubuntu Server 24.04 LTS amd64 live-server. Then choose **New deployment**.

## Update GDeploy

Run these commands from the **same clone** when you want to test the updated app:

```sh
cd ~/GDeploy
git pull --ff-only
sudo docker compose up -d --build --wait
```

This rebuilds the image and replaces the container while preserving the existing data volume, account, settings and deployment history. Keep your existing `.env` and `media/` directory. Refresh the browser afterward.

For logs, media/software setup, networking or backups, use the [operations reference](https://github.com/DasFunfZigste/GDeploy/blob/v0.15.2/docs/OPERATIONS.md). Release versions and changelogs are on [GitHub Releases](https://github.com/DasFunfZigste/GDeploy/releases).
