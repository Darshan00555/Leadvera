#!/usr/bin/env bash
# One-time setup of a fresh Oracle Cloud (Always Free) Ubuntu server for Leadvera.
#   bash oracle-setup.sh
# Installs Docker, opens HTTP/HTTPS in Oracle's built-in firewall, adds swap, sets India time.
set -euo pipefail

sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl iptables-persistent netfilter-persistent
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER"

# Oracle's Ubuntu image only lets SSH in; Caddy needs 80 (certificate check) and 443 (the portal).
for port in 80 443; do
  sudo iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null ||
    sudo iptables -I INPUT 1 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
done
sudo netfilter-persistent save

# 2 GB swap so the image build never runs out of memory.
if ! sudo swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi
sudo timedatectl set-timezone Asia/Kolkata || true

echo
echo "Server ready. Type 'exit', connect again with ssh (so docker works without sudo), then continue the guide."
