#!/usr/bin/env bash
# One-shot VM bootstrap: swap, Docker, and unpack the app. Idempotent-ish.
set -euo pipefail

echo "::: [1/4] Adding 2G swap (build headroom)"
if ! sudo swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "::: [2/4] Installing prerequisites + Docker"
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
  curl -fsSL https://download.docker.com/linux/debian/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
fi
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update -y
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER" || true

echo "::: [3/4] Unpacking application"
mkdir -p "$HOME/rag-console"
tar -xzf "$HOME/deploy.tar.gz" -C "$HOME/rag-console"

echo "::: [4/4] Versions"
sudo docker --version
sudo docker compose version

echo "BOOTSTRAP_DONE"
