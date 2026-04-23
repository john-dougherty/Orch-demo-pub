#!/usr/bin/env bash
# Install ngrok inside the HermesOrch LXC and register its authtoken.
# Run on the container (192.168.10.232) as root. Usage:
#   NGROK_AUTHTOKEN=xxx bash install_ngrok.sh
set -euo pipefail

if [[ -z "${NGROK_AUTHTOKEN:-}" ]]; then
  echo "NGROK_AUTHTOKEN env var is required" >&2
  exit 1
fi

if ! command -v ngrok >/dev/null 2>&1; then
  curl -fsSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
    | tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
  echo "deb https://ngrok-agent.s3.amazonaws.com buster main" \
    > /etc/apt/sources.list.d/ngrok.list
  apt-get update -qq
  apt-get install -y ngrok
fi

ngrok config add-authtoken "$NGROK_AUTHTOKEN"
echo "ngrok installed: $(ngrok --version)"
