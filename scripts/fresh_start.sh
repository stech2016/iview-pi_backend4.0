#!/bin/sh
# DELETES ALL DATA: every user, company, device and reading. Keeps the server, the .env file and the database volume itself.
# Use only for a fresh start.   Run:  sh scripts/fresh_start.sh YES
set -e
cd "$(dirname "$0")/.."
[ "$1" = "YES" ] || { echo "This deletes ALL data. To go on run:  sh scripts/fresh_start.sh YES"; exit 1; }
if command -v podman >/dev/null 2>&1; then ENGINE=podman; elif command -v docker >/dev/null 2>&1; then ENGINE=docker; else echo "Install Podman or Docker first"; exit 1; fi
DB=$($ENGINE ps --format '{{.Names}}' | grep -E 'db-1$|_db_1$' | head -n 1)
[ -n "$DB" ] || { echo "The database container is not running"; exit 1; }
for c in $($ENGINE ps --format '{{.Names}}' | grep -E 'backend-1$|ingest-1$|_backend_1$|_ingest_1$'); do $ENGINE stop "$c"; done
$ENGINE exec "$DB" sh -c 'for d in $(psql -U "$POSTGRES_USER" -d postgres -Atc "SELECT datname FROM pg_database WHERE datname LIKE '"'"'stech_%'"'"'"); do echo "dropping $d"; psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE \"$d\" WITH (FORCE)"; done'
VK=$($ENGINE ps --format '{{.Names}}' | grep -E 'valkey-1$|_valkey_1$' | head -n 1)
[ -z "$VK" ] || $ENGINE exec "$VK" valkey-cli FLUSHALL
echo "All data deleted. Now run:  sh scripts/update.sh"
