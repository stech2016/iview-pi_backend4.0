#!/bin/sh
# Update this server from the internet: pull the newest image the build pipeline published, restart only what changed.
# Works with Podman or Docker (whichever is installed; Podman is used first).
# Run by hand, or every night from cron:   0 3 * * *  cd /opt/stech-iview && sh scripts/update.sh >> update.log 2>&1
# Needs IVIEW_IMAGE=ghcr.io/<owner>/<repo>:latest in .env. Your data (database volume) is never touched.
set -e
cd "$(dirname "$0")/.."
IMAGE=$(grep '^IVIEW_IMAGE=' .env | cut -d= -f2-)
[ -n "$IMAGE" ] || { echo "Add IVIEW_IMAGE=ghcr.io/<owner>/<repo>:latest to .env first"; exit 1; }
if command -v podman >/dev/null 2>&1; then ENGINE=podman; elif command -v docker >/dev/null 2>&1; then ENGINE=docker; else echo "Install Podman or Docker first"; exit 1; fi
$ENGINE pull "$IMAGE"
# Broker rules AND broker settings ship inside the image. If they differ from the server copy, replace them and restart the broker,
# so a new feature (remote write, bigger reading queue ...) never needs a hand edit on the server. The password file is not touched.
CHANGED=0
for F in acl mosquitto.conf entrypoint.sh; do
  if $ENGINE run --rm --entrypoint cat "$IMAGE" /app/mosquitto/$F > mosquitto/$F.new 2>/dev/null && [ -s mosquitto/$F.new ]; then
    if ! cmp -s mosquitto/$F.new mosquitto/$F; then
      [ -f mosquitto/$F ] && cp mosquitto/$F mosquitto/$F.bak
      mv mosquitto/$F.new mosquitto/$F
      CHANGED=1
      echo "broker file $F updated"
    else rm -f mosquitto/$F.new; fi
  else rm -f mosquitto/$F.new; echo "WARNING: could not read mosquitto/$F from the image - the broker file was NOT refreshed. Tell the developer."; fi
done
[ "$CHANGED" = 1 ] && $ENGINE compose restart mosquitto
# The WEB version of the app (the Flutter web build kept in the image under /app/web) is copied next to Caddy, so a machine QR label
# and any browser open the app. The files are replaced INSIDE the folder (Caddy keeps that folder open), and only when the image really holds a build.
if $ENGINE run --rm --entrypoint sh "$IMAGE" -c 'test -f /app/web/main.dart.js' >/dev/null 2>&1; then
  rm -rf web.new && mkdir -p web.new web
  if $ENGINE run --rm --entrypoint tar "$IMAGE" -C /app/web -cf - . | tar -xf - -C web.new && [ -s web.new/index.html ]; then
    find web -mindepth 1 -delete && cp -a web.new/. web/ && echo "web version of the app updated"
  else echo "WARNING: could not copy the web version of the app from the image. Tell the developer."; fi
  rm -rf web.new
fi
$ENGINE compose up -d backend ingest
$ENGINE image prune -f
echo "updated $(date) ($ENGINE)"
