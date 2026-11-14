# STECH iVIEW cloud backend - version 1.1 - file guide

Start here. This page says **which file does what**, so nobody has to search. Setup steps are in `README.md`.

## Where is `.env`?
In **this folder** (next to `docker-compose.yml`). `python3 setup.py` creates it once. Edit it with `nano .env`, then `podman compose up -d`.
Never change `SECRET_KEY`, `FERNET_KEY`, `AUDIT_HMAC_KEY` after people use the app.

New lines in v1.1 (all optional, all have safe defaults):

| Line | Meaning |
|---|---|
| `GATEWAY_OFFLINE_SECONDS=4` | plant script counted offline after this many seconds without a heartbeat (default 4) |
| `REMEMBER_DAYS=30` | "Keep me signed in" lasts this many days |
| `IVIEW_IMAGE=ghcr.io/<owner>/<repo>:latest` | image the server pulls for updates |
| `SMS_PROVIDER=twilio`, `SMS_ACCOUNT`, `SMS_TOKEN`, `SMS_FROM` | SMS and voice add-on, client's own Twilio account. Empty = off |
| `PLANT_UTC_OFFSET_MINUTES=330` | plant local time for reports and the nightly clean-up (330 = IST) |
| `PURGE_HOUR=2` | local hour of the nightly clean-up |

## Use the web version of the app
The browser only talks to this server if its address is allowed. In `.env` set `CORS_ORIGINS` to every address the web app is opened from, separated by commas, for example `CORS_ORIGINS=https://iview.mycompany.com,http://localhost:8080` (the second one is only for testing on your own PC). Then restart: `docker compose up -d backend` (or `podman compose up -d backend`).

## Run it with Podman (instead of Docker)
Same files. Use `podman compose up -d --build` where this guide says `docker compose`. Image names in `docker-compose.yml` are full names, so Podman never asks "which registry".

## Updates from the internet
1. Push the code to your Git repository (main branch). `.github/workflows/build.yml` runs the tests, builds the image and publishes it.
2. On the server, put `IVIEW_IMAGE=ghcr.io/<owner>/<repo>:latest` in `.env`, then run `sh scripts/update.sh` (or run it every night with cron, line inside the script).
Your data is never touched by an update.

## File map

### Top folder
| File | What it is for |
|---|---|
| `.env` | all settings (made by `setup.py`) |
| `setup.py` | asks 3 questions, makes `.env` |
| `docker-compose.yml` | the whole server: database, valkey, mosquitto, backend, ingest, caddy |
| `Dockerfile` | how the backend image is built |
| `Caddyfile` | https door in front of everything |
| `mosquitto/` | MQTT broker settings (`mosquitto.conf`, `acl`) |
| `alembic/versions/` | database upgrades, run automatically on start (`0007` alert rules, `0008` keep-until-deleted) |
| `deploy/` | OPTIONAL: `docker-compose.monitor.yml` (Uptime Kuma), `docker-compose.security.yml` (CrowdSec), `caddy-waf/` (Coraza firewall) |
| `plant_gateway/` | the plant script that sits at the plant |
| `tests/` | `python -m pytest tests -q` (needs PostgreSQL and Redis on localhost) |
| `.github/workflows/build.yml` | automatic test + build + publish |

### `scripts/`
| File | What it is for |
|---|---|
| `update.sh` | pull the newest image and restart |
| `set_quota.py` | Siemas sets a client's GB: `python scripts/set_quota.py <company> <gb>` |
| `test_mail.py` | checks the e-mail setup |
| `rotate_secrets.py`, `security_check.py`, `migrate_all_tenants.py`, `fake_plc.py`, `backup.sh` | as before |

### `app/api/v1/` - the web addresses the app calls (one file per area)
| File | Area |
|---|---|
| `auth.py` | owner sign-up with terms and e-mail activation, login (2-step code, keep signed in, forced password change), password reset |
| `account.py` | 2-step login on/off, own profile (e-mail, phone, password) |
| `users.py` | the Super admin (and 'Manage users' holders, inside their own area) create, change, switch off / on people; job titles and permissions |
| `control.py` | write to the plant: send a value / on-off command and follow it (sent, delivered, written, confirmed) |
| `company_settings.py` | the Settings screen: every adjustable value of a company |
| `plant.py` | plant health panel, alarm response report, plant map, shift handover (+ PDF) |
| `share.py` | read-only share links (public live page) and QR labels |
| `setup.py` | industry templates, CSV tag import, connection test |
| `devices.py`, `changes.py` | devices, sensors; an operator's edit becomes a change request (Regional admin decides, Super admin is told and can alert / review) |
| `alarms.py`, `alert_rules.py` | alarms (comments, shelving); the user's own alert rules |
| `oncall.py` | on-call roster per State + Region |
| `retention.py` | used / free space, keep-for time, region shares, free up space |
| `reports.py` | ERP export (CSV / Excel / PDF), scheduled e-mail reports |
| `tiles.py` | dashboard tile per State + Region |
| `storage.py` | which database the company uses |
| `gateway.py`, `remote_access.py` | plant PCs, remote address |
| `audit.py`, `health.py`, `ws.py`, `router.py` | audit log, health + `/app-info`, live stream, list of all routers |

### `app/services/` - the work behind the addresses (one job per file)
| File | Job |
|---|---|
| `alarms.py` | decides when an alarm starts / ends and who is told (maintenance mode and shelving: recorded, nobody told) |
| `commands.py` | a write command: sign, send, acknowledgements, expiry watchdog |
| `csettings.py` | the list of company settings (one line each) |
| `perms.py` | job titles and the None / Read / Read and Write level per area |
| `comm.py`, `stuck.py` | silent-device alarm; stuck-sensor warning |
| `plantsummary.py` | plant health numbers, alarm report, handover snapshot |
| `alertrules.py` | evaluates the user's alert rules |
| `oncall.py` | tells roster step 1, then step 2... if nobody acknowledges |
| `notify.py` | one call that tells people: phone push + SMS / voice |
| `channels/` | `twilio_sms.py`; add another provider = copy `twilio_sms.py` + one line in `__init__.py` |
| `push.py` | phone notifications (Firebase) |
| `mail.py` | e-mail (Brevo / SMTP), with attachment |
| `twofactor.py` | 2-step login codes |
| `retention.py` | storage numbers, free-up, nightly clean-up (the export-before file is built in `api/v1/retention.py`) |
| `erp.py`, `tablefile.py`, `export.py` | report rows; CSV / Excel / PDF builder; device and dashboard exports |
| `jobs.py` | every minute: scheduled reports, nightly clean-up |
| `gwcontrol.py`, `gateway.py`, `status.py`, `lastseen.py` | plant script control, online / offline rules (offline watch checks every 1 s) |
| `ingest.py`, `live.py`, `sync.py`, `audit.py`, `lockout.py`, `resetcode.py` | as before |

### Other `app/` files
| File | Job |
|---|---|
| `main.py` | starts the API; small database upgrades on start |
| `core/config.py` | every `.env` setting and its default |
| `core/security.py`, `crypto.py` | tokens, passwords, encryption |
| `models/master.py`, `models/tenant.py` | database tables (company-wide / per company) |
| `schemas/` | what each request and answer looks like |
| `deps.py` | who may see what (roles, State + Region) |
| `workers/mqtt_ingest.py` | the background worker: readings in, plant-script watch, timed jobs |
| `workers/poller.py` | reads Modbus / OPC-UA devices; escalation of unacknowledged alarms |

## What v1.1 changed (small, separate files)
Everything is read-only toward the PLC.

## v1.1.4 (3 Nov 2026)
- Country -> State / province -> Region -> City (all countries, free ISO 3166 data via pycountry). New: `GET /auth/countries`, `GET /auth/states?country=`.
- Parent company: new role `parent_admin` (own login, no plant data). Sub-company Super admins may pick a Parent at sign-up and wait for its approval. `GET /parent/overview`, `GET/POST /parent/registrations`.
- Sign-up checks every field (phone is optional). `GET /auth/parents`.
- Dashboard: `GET /devices/sparks` (two main readings + last 15 minutes), `PUT /devices/{id}/main-sensors`, `GET /gateway/summary`.
- Database: master `users.country`, `users.parent_company` (added automatically at start); tenant migration 0009 (`devices.country`, `device_registers.is_main`).

## v1.1.5 final (5 Nov 2026)
- Tenant migration 0011: alarm rule kinds (level, deadband, rate, no-data), downtime events, batches, screens, command reason/pulse/approval.
- New API: `/downtime*`, `/batches*`, `/energy/{id}/live|report`, `/screens/process/{device}`, `/screens/hmi`, `/commands/{id}/approve|reject`.
- Writes now need a reason and the user's password (or 2-step code). Optional second-person approval. Pulse commands release themselves after 1 s.
- Any capitalisation is accepted for protocol, region, state, country, register type, data type, severity, byte/word order, parity; the app shows the standard names.
- Parent company removed.
- Web app: Caddy serves `./web` (copy `build/web` there). `/api`, `/reset-password`, `/activate` go to the backend.
- Animations: defaults in `app/data/animations.json`; changes by users are stored per device in the `screens` table.
- Outbound connectors / webhooks: comments `FUTURE OUTBOUND CONNECTOR` in ingest.py, alarms.py, commands.py, batches.py, downtime.py, screens.py.
- Tests: `python -m pytest tests -q` (82 pass).

## Signed automatic update of the plant script (v1.1.5)

Plant PCs update their own `plant_gateway.py`. Nobody downloads it again.

**One-time setup (on your own PC, not on the server)**
1. `pip install cryptography` then `python scripts/gen_signing_key.py` - prints a private key.
2. GitHub repo -> Settings -> Secrets and variables -> Actions -> New repository secret. Name `PLANT_SCRIPT_SIGNING_KEY`, value = that key. Keep a copy in your password manager. It is never put on the server.
3. Push to `main`. The build signs the script (`scripts/sign_release.py`) and bakes the signature and the public key into the image. Without the secret the build still works but no updates are offered.
4. Download the script from the app **one last time** for each plant PC (scripts older than 3.3 cannot verify signatures). From then on updates are automatic.

**Shipping a new script version:** change `VERSION` in `plant_gateway/plant_gateway.py` (higher number), push, run `scripts/update.sh` on the server. Each plant PC checks every 10 minutes.

**Safety:** only a higher version; Ed25519 signature + SHA-256 must match; the file must compile and pass a start-up self-test; the new version must reach the cloud within 2 minutes or the old one is restored and that version is never retried on that PC; every install or rollback is written to the audit log (`gateway.script_updated`, `gateway.script_update_rolled_back`). Switch per plant PC on the Gateway page (`POST /gateway/{id}/auto-update`) or `"auto_update": false` in link.json.

## Gateway as a ready-made program (nothing to install on the plant PC)
The Gateway page downloads a zip with a ready-made program (Python and all libraries inside): `STECH-Gateway.exe` (Windows), `stech-gateway` (Linux, macOS). The customer unzips and double-clicks; no Python, no pip.
- Built by the `bundle` jobs in `.github/workflows/build.yml` (PyInstaller, `plant_gateway/launcher.py`), smoke-tested in the pipeline, copied into the image under `plant_gateway/bin/<system>/`.
- The program keeps `plant_gateway.py` (next to it) running, restarts it after a crash or a signed self-update, and tests a new script with `program <script> --selftest` before the swap.
- The signed self-update replaces `plant_gateway.py`. The PROGRAM updates itself too: when `LAUNCHER_VERSION` in `launcher.py` is raised, the pipeline signs the new program (per system, in `release.json`), each plant script downloads it, checks SHA-256 + signature, tests it (`new-program plant_gateway.py --selftest`), and its supervisor swaps it in (old one kept as `.old`). The new program must connect to the cloud within 2 minutes, otherwise the old one is put back and that version is never tried again on that PC (audit: `gateway.program_updated` / `gateway.program_update_rolled_back`). A script that needs new libraries declares `NEEDS_LAUNCHER = 2`; it installs after the program update (next check). Nobody downloads again.
- Tested here end to end with real Linux programs (offer, download, verify, swap, rollback). The Windows swap (renaming the running .exe) is the one step not yet tried on a real Windows PC.
- A build without the programs still works for Windows (old `START.bat`, needs Python); Linux/macOS answer "not in this server build yet".
- Windows may show "Windows protected your PC" for an unsigned program: More info, Run anyway. A code-signing certificate (paid) removes the warning. macOS: `xattr -d com.apple.quarantine stech-gateway` once (an Apple developer account removes it).
