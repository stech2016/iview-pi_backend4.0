#!/bin/sh
# One-time upgrade of a running v1.1.x server to V2.0 (customers' own gateways). Safe to run twice. Your data is never touched.
# Run from the server folder (the one with docker-compose.yml and .env):   sh scripts/upgrade_to_v2.sh
set -e
cd "$(dirname "$0")/.."
if command -v podman >/dev/null 2>&1; then ENGINE=podman; elif command -v docker >/dev/null 2>&1; then ENGINE=docker; else echo "Install Podman or Docker first"; exit 1; fi
[ -f .env ] || { echo "No .env here. Run this from the server folder."; exit 1; }
[ -f mosquitto/entrypoint.sh ] || { echo "mosquitto/entrypoint.sh is missing. Unpack the V2.0 backend over this folder first (keep your .env)."; exit 1; }

cp .env ".env.before-v2.$(date +%Y%m%d-%H%M%S)"
DOMAIN=$(grep '^DOMAIN=' .env | cut -d= -f2-)
add() { grep -q "^$1=" .env || { echo "$1=$2" >> .env; echo "added $1 to .env"; }; }
add MQTT_ADMIN_USERNAME stech_admin
add MQTT_ADMIN_PASSWORD "$(head -c 24 /dev/urandom | base64 | tr -d '/+=\n' | head -c 32)"
add MQTT_TLS_PORT 8883
add MQTT_TLS_HOST "$DOMAIN"
chmod 600 .env

echo "Starting the new version (the database and your data stay as they are) ..."
$ENGINE compose up -d --build
echo
echo "Done. Two things are still needed ON THE INTERNET SIDE (nothing on this server can do them):"
echo "  1. Open TCP port 8883 in the server's firewall / cloud network security group (Azure: Networking > Add inbound rule, port 8883, TCP)."
echo "  2. Check the broker:   $ENGINE compose logs mosquitto | tail -20"
echo "       must say  'TLS door 8883: ON'   (if it says OFF, the https certificate does not exist yet: open https://$DOMAIN once and wait a minute)"
echo "Then add a gateway in the app (Gateway page > Add gateway) and read the manual."
