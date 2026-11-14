# STECH iVIEW – cloud backend

Runs 24/7 on any cloud server (Azure, AWS, Google, DigitalOcean, Hetzner...). Needs only **Docker** and a **domain name**.

## Start (4 steps)
1. Copy this folder to the server and open it in a terminal.
2. `python3 setup.py`  – answer 3 questions (domain, database, e-mail). It creates `.env` with all secret keys.
3. Point your domain to the server's public IP. Open ports **80** and **443**, and **8883** (TLS MQTT) if customers connect their own gateways (V2.0).
4. `docker compose up -d --build`

Check: open `https://<your-domain>/api/v1/healthz` → `{"status":"ok"}`.
Then in the app, type `https://<your-domain>` on the first screen.

## What you may change later (everything is in `.env`)
| Change | Lines in `.env` |
|---|---|
| Move to another cloud / managed database | `PG_HOST`, `PG_ADMIN_USER`, `PG_ADMIN_PASSWORD`, `PG_SSLMODE=require` (and remove `COMPOSE_PROFILES=localdb`) |
| New domain | `DOMAIN`, `PUBLIC_API_URL`, `FRONTEND_URL`, `CORS_ORIGINS`, `MQTT_PUBLIC_HOST` |
| E-mail for forgot-password | `BREVO_API_KEY`, `MAIL_FROM` (see **E-mail with Brevo** below) |
| Phone notifications | `FCM_SERVICE_ACCOUNT_B64` (see **Phone notifications** below) |

After editing `.env`: `docker compose up -d`.  **Never change** `SECRET_KEY`, `FERNET_KEY`, `AUDIT_HMAC_KEY` after people use the app.

## E-mail with Brevo (forgot password)
Only the Brevo API key is needed (free plan: 300 e-mails a day). The mail goes to the e-mail address the person registered with.
1. Make a free account at brevo.com. Menu (your name, top right) **SMTP & API → API keys → Generate a new API key**. Copy it (it starts with `xkeysib-`).
2. If Brevo shows **Settings → Security → Authorised IPs** as active, add the server's public IP there (or click **Deactivate blocking**). Otherwise Brevo refuses the server.
3. On the server: `nano .env`, add the line `BREVO_API_KEY=xkeysib-...`, save (Ctrl+O, Enter, Ctrl+X).
4. `sudo docker compose up -d --force-recreate backend ingest`
5. Test: `sudo docker compose exec backend python scripts/test_mail.py you@example.com` - it says SENT, or says exactly what is wrong.
`MAIL_FROM` is optional: without it the cloud uses the sender of your Brevo account (the e-mail you signed up with). Set it only to use another verified sender.
The e-mail has a **Reset my password** button (opens a page of your own server in the phone's or PC's browser) and, as a backup, a 6-digit code for the app (Forgot password → I have a code). Both work 15 minutes and only once.

## Phone notifications (pop-up on the phone, even when the app is closed)
Free, through Google Firebase Cloud Messaging. The in-app alarm banner keeps working without it.
1. console.firebase.google.com → **Add project** (no Analytics needed) → **Project settings → Service accounts → Generate new private key**. A `.json` file downloads.
2. Copy it to the server and make one line: `base64 -w0 firebase-key.json` and put the result into `.env` as `FCM_SERVICE_ACCOUNT_B64=...`
3. `sudo docker compose up -d --force-recreate backend ingest`
4. Build the app with the same project's `google-services.json` (see the app's `README`/`android_setup`). Delete `firebase-key.json` from the server afterwards; it is a secret.
Each critical/major/minor/advisory alarm is pushed to the Super admin and to the people of that State + Region. A critical alarm for a stopped plant PC and a "not acknowledged in time" reminder are pushed too.

## What runs (all open source)
FastAPI (API) · PostgreSQL · Mosquitto (MQTT) · Valkey (instant push) · Caddy (automatic https) · `ingest` (plant readings → database, plant-script watchdog).

## Security in short
- Open ports: 80, 443 and (V2.0, customer gateways) 8883 with TLS and a login per plant. Database, Valkey and the plain MQTT port 1883 are never reachable from the internet.
- Everything is encrypted (https; plant scripts use MQTT inside https).
- Passwords are bcrypt-hashed, logins use short-lived tokens, every company has its own database.
- Every plant reading is signed with the key of its own gateway (each plant script has its own key); unsigned readings, readings with a cancelled key, and readings for devices outside the gateway's State + Region are thrown away.
- The plant script download link works 10 minutes and only for a signed-in admin.

## Useful commands
- Logs: `docker compose logs -f backend ingest`
- Update after a new version: copy the new files, then `docker compose up -d --build`
- Backup: `docker compose exec db pg_dumpall -U postgres > backup.sql` (bundled database)

## Plant side
In the app (Super admin or Regional admin) → Gateway page → **Add plant PC** (a State + Region may have several; each device names the plant PC that reads it) → **Download** → copy to the plant PC → double-click `START.bat`.
A device turns grey (**stale**) after 5 seconds without data and shows **Offline** after 10 seconds (both are per-company settings in Company settings; defaults 5 s and 10 s). The plant script sends a heartbeat with the state of every device each second.
Optional: right-click `AUTOSTART.bat` → Run as administrator (it must say DONE). It then starts with the PC and runs hidden; check `gateway.log`.
After updating from an older version, restart the broker once so it reads the new access rules: `docker compose up -d --force-recreate mosquitto`.

Modbus options the plant script reads (read-only, it never writes to a PLC): TCP, RTU over TCP, UDP, TLS, and serial RTU / ASCII (RS-485, RS-422, RS-232 through a USB-serial adapter). Several slaves can share one serial cable. Per device you can set slave ID, byte order, word order, address base (0 or 1), timeout and retries; sensors can be 16/32/64-bit integers, floats, or a single bit of a status word. OPC-UA is read too. If the internet or the PLC drops, readings are kept and sent when it returns; a wrong PC clock is corrected using the cloud's time.
After updating, the database upgrades itself when the API starts. Plant scripts downloaded before this version keep working but only read Modbus TCP and OPC-UA; download the script again from the app to get all options.

## Roles
- **Super admin** (the company creator): whole company, all plant PCs, storage, audit log, alarm escalation ("not acknowledged in time", critical and major only).
- **Regional admin**: one State + Region. Approves that region's sign-ups and change requests, manages that region's devices and its plant PCs.
- **Operator** / **Guest**: as before, limited to their own State + Region.
