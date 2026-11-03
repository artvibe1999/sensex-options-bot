#!/usr/bin/env bash
# One-time preparation of a VM for SENSEX PRO (the existing sxbot VM already has all of this).
# Confirms the target first; installs Docker only if missing; creates data/ and logs/ owned by the container user.
#   bash scripts/bootstrap_vm.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "Host: $(hostname)  User: $(whoami)  Dir: $DIR"
read -r -p "Prepare THIS machine for SENSEX PRO? [y/N] " ok
[ "${ok:-}" = "y" ] || { echo "aborted"; exit 1; }
command -v docker >/dev/null || { curl -fsSL https://get.docker.com | sudo sh; }
sudo timedatectl set-ntp true || true
mkdir -p "$DIR/data" "$DIR/logs"
sudo chown -R 10001:10001 "$DIR/data" "$DIR/logs"
[ -f "$DIR/.env" ] || { cp "$DIR/.env.example" "$DIR/.env"; chmod 600 "$DIR/.env"; echo "Fill in $DIR/.env (chmod 600)"; }
echo "Next: fill .env, then: sudo docker compose up -d --build   (starts in PAPER mode)"
