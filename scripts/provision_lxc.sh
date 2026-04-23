#!/usr/bin/env bash
# Provision HermesOrch LXC on the Proxmox host at 192.168.10.22.
# Run from the Proxmox host as root.
#
# Assumes an Ubuntu 24.04 template is already downloaded. If not:
#   pveam update && pveam download local ubuntu-24.04-standard_24.04-2_amd64.tar.zst
set -euo pipefail

CTID="${CTID:-303}"
HOSTNAME="HermesOrch"
TEMPLATE="local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst"
STORAGE="${STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr0}"
CORES="${CORES:-4}"
MEMORY_MB="${MEMORY_MB:-8192}"
DISK_GB="${DISK_GB:-40}"
# DHCP avoids an IP-collision guess on an unknown LAN. Lease is captured below.
NET="name=eth0,bridge=${BRIDGE},ip=dhcp"

pct create "$CTID" "$TEMPLATE" \
  --hostname "$HOSTNAME" \
  --cores "$CORES" \
  --memory "$MEMORY_MB" \
  --rootfs "${STORAGE}:${DISK_GB}" \
  --net0 "$NET" \
  --features nesting=1 \
  --unprivileged 1 \
  --onboot 1 \
  --start 1

# Wait a moment for container to come up
sleep 5

pct exec "$CTID" -- bash -lc '
  apt-get update
  apt-get install -y python3.12 python3.12-venv python3-pip git curl build-essential ffmpeg
  curl -LsSf https://astral.sh/uv/install.sh | sh
  echo "Container ready. Ollama target: http://192.168.10.33:11434"
'

LEASED_IP=$(pct exec "$CTID" -- bash -lc "hostname -I | awk '{print \$1}'")
echo "HermesOrch LXC ${CTID} provisioned. Leased IP: ${LEASED_IP}"
