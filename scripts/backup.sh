#!/usr/bin/env bash
# Disaster-recovery snapshots of the master registry + every tenant DB.
# Cron example (daily 02:30):  30 2 * * *  /opt/stech/backend/scripts/backup.sh >> /var/log/stech_backup.log 2>&1
set -euo pipefail
: "${PGHOST:=localhost}" "${PGPORT:=5432}" "${PGUSER:=postgres}" "${MASTER_DB:=stech_master}"
: "${BACKUP_DIR:=/var/backups/stech}" "${RETENTION_DAYS:=14}"
export PGPASSWORD="${PGPASSWORD:?set PGPASSWORD}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP_DIR/$STAMP"; umask 077

dump() {  # $1 = db name
  local out="$BACKUP_DIR/$STAMP/$1.dump"
  pg_dump -Fc -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$1" > "$out"
  if [ -n "${GPG_RECIPIENT:-}" ]; then gpg --yes -r "$GPG_RECIPIENT" -e "$out" && rm -f "$out"; fi
  echo "[$(date -u +%FT%TZ)] backed up $1"
}

dump "$MASTER_DB"
psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$MASTER_DB" -Atc \
  "SELECT db_name FROM company_storage WHERE db_name IS NOT NULL AND mode <> 'cloud'" | while read -r db; do
  [ -n "$db" ] && dump "$db"
done
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENTION_DAYS" -exec rm -rf {} +
echo "[$(date -u +%FT%TZ)] backup complete: $BACKUP_DIR/$STAMP"
