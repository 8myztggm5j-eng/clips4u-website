#!/usr/bin/env bash
# Run from repository root: bash oracle/configure.sh YOUR_PUBLIC_IP
set -euo pipefail

if [[ "${1:-}" == "" ]]; then
  echo "Usage: bash oracle/configure.sh YOUR_ORACLE_PUBLIC_IPV4"
  exit 2
fi
IP="$1"
if ! [[ "$IP" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
  echo "Expected a public IPv4 address (example: 203.0.113.10)."
  exit 2
fi
IFS='.' read -r a b c d <<< "$IP"
for octet in "$a" "$b" "$c" "$d"; do
  if (( 10#$octet > 255 )); then
    echo "Invalid IPv4 address"
    exit 2
  fi
done
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if ! command -v openssl >/dev/null 2>&1; then
  echo "Install openssl first: sudo apt-get install -y openssl"
  exit 2
fi
if ! sudo docker compose version >/dev/null 2>&1; then
  echo "Install Docker Engine and the Docker Compose v2 plugin first."
  exit 2
fi

ENV_PATH="$ROOT/oracle/.env"
DOMAIN="clips4u.${IP//./-}.sslip.io"
if [[ ! -e "$ENV_PATH" ]]; then
  # Keep API authentication secret on the server. NEVER commit oracle/.env.
  KEY="$(openssl rand -hex 32)"
  umask 077
  printf 'CLIPS4U_DOMAIN=%s\nCLIPS4U_IMPORT_KEY=%s\n' "$DOMAIN" "$KEY" > "$ENV_PATH"
  unset KEY
  echo "Created private oracle/.env"
else
  echo "Reusing oracle/.env (will not overwrite your secret)."
  DOMAIN="$(sed -n 's/^CLIPS4U_DOMAIN=//p' "$ENV_PATH" | head -n1)"
fi

sudo docker compose --env-file "$ENV_PATH" -f "$ROOT/oracle/compose.yaml" up -d --build
echo
echo "Your new HTTPS backend: https://$DOMAIN"
echo "Test when DNS and certificates are ready: https://$DOMAIN/health"
echo "Your private access key remains in oracle/.env. Don't paste it in chat or GitHub."
echo "Enter the new URL and access key in Clips4U's existing import fields."
