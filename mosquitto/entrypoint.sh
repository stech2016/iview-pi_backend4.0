#!/bin/sh
# Starts the broker: static logins, Dynamic Security plugin (customer gateways), and - when the server's https certificate exists -
# the TLS door on port 8883 that customers' gateways use. Re-starts the broker (the container restarts itself) when the certificate is renewed.
# Environment: MQTT_PASSWORD, MQTT_GATEWAY_PASSWORD, MQTT_ADMIN_PASSWORD, DOMAIN. Certificate: Caddy's volume mounted at /caddy (read only).
DATA=/mosquitto/data
DYN=$DATA/dynamic-security.json
RUN=/mosquitto/run
CERTS=/mosquitto/certs
mkdir -p "$RUN" "$CERTS"
chown mosquitto:mosquitto "$DATA" 2>/dev/null

# ---- 1. the static logins (rebuilt at every start from .env)
rm -f /mosquitto/passwd
mosquitto_passwd -b -c /mosquitto/passwd stech_backend "$MQTT_PASSWORD" || exit 1
mosquitto_passwd -b /mosquitto/passwd stech_gateway "$MQTT_GATEWAY_PASSWORD" || exit 1
chown mosquitto:mosquitto /mosquitto/passwd && chmod 600 /mosquitto/passwd

# ---- 2. Dynamic Security plugin (the customer gateways' logins). Created once, kept in the data volume.
PLUGIN=$(ls /usr/lib/mosquitto_dynamic_security.so /usr/lib/*/mosquitto_dynamic_security.so /usr/local/lib/mosquitto_dynamic_security.so 2>/dev/null | head -n 1)
if [ -z "$PLUGIN" ]; then
  echo "FATAL: the Dynamic Security plugin (mosquitto_dynamic_security.so) is not in this broker image. Use the image eclipse-mosquitto:2." >&2
  exit 1
fi
if [ -z "$MQTT_ADMIN_PASSWORD" ]; then
  echo "FATAL: MQTT_ADMIN_PASSWORD is empty. Add it to .env (scripts/upgrade_to_v2.sh does that for you)." >&2
  exit 1
fi
if [ ! -s "$DYN" ]; then
  mosquitto_ctrl dynsec init "$DYN" stech_admin "$MQTT_ADMIN_PASSWORD" || exit 1
  echo "dynamic security: created $DYN"
fi
chown mosquitto:mosquitto "$DYN" && chmod 600 "$DYN"

# ---- 3. certificate of the server name (Caddy fetched it for https) -> TLS door 8883
cert_file() { find /caddy/caddy/certificates -type f -name "$DOMAIN.crt" 2>/dev/null | head -n 1; }
CRT=$(cert_file)
CUR=""
TLS=0
if [ -n "$DOMAIN" ] && [ -n "$CRT" ] && [ -s "${CRT%.crt}.key" ]; then
  cp "$CRT" "$CERTS/server.crt" && cp "${CRT%.crt}.key" "$CERTS/server.key" \
    && chown mosquitto:mosquitto "$CERTS/server.crt" "$CERTS/server.key" && chmod 640 "$CERTS/server.crt" "$CERTS/server.key" && TLS=1
  CUR=$(md5sum "$CRT" | cut -d' ' -f1)
fi

# ---- 4. the final settings file: plugin lines first (global), the product's settings, then the TLS listener
{
  echo "plugin $PLUGIN"
  echo "plugin_opt_config_file $DYN"
  cat /mosquitto/config/mosquitto.conf
  if [ "$TLS" = 1 ]; then
    echo ""
    echo "listener 8883"
    echo "certfile $CERTS/server.crt"
    echo "keyfile $CERTS/server.key"
    echo "tls_version tlsv1.2"
  fi
} > "$RUN/mosquitto.conf"
[ "$TLS" = 1 ] && echo "TLS door 8883: ON (certificate of $DOMAIN)" || echo "TLS door 8883: OFF until the https certificate of '$DOMAIN' exists (checked every minute)"

mosquitto -c "$RUN/mosquitto.conf" &
MPID=$!
trap 'kill -TERM $MPID 2>/dev/null' TERM INT

# ---- 5. watcher: a certificate that appeared (first start) or was renewed -> restart the broker once (clients reconnect by themselves)
(
  while sleep 60; do
    NOW=$(cert_file)
    [ -n "$NOW" ] || continue
    H=$(md5sum "$NOW" 2>/dev/null | cut -d' ' -f1)
    if [ -n "$H" ] && [ "$H" != "$CUR" ]; then
      echo "the https certificate changed: restarting the broker to use it"
      kill -TERM $MPID 2>/dev/null
      exit 0
    fi
  done
) &

wait $MPID
exit 1
