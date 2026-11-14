# STECH iVIEW 1.1.5 - configuration guide for the backend team

Nothing in the plant logic is fixed. There are three places to change things, each a line or two:

1. **Per company, from the app (Settings screen, Super admin) or the API** - no restart, active within 5 seconds.
2. **Server `.env` file** - defaults for every company, and the switches of the server. Change the line, then `docker compose up -d backend ingest`.
3. **Plant PC `link.json`** - only for the plant script (poll speed). Normally changed from the app, see below.

## 1. Company settings (screen "Settings", Super admin only)

Change from the app, or with one call:  `PUT /api/v1/settings  {"values": {"gateway_offline_seconds": 15}}`
(send `null` to go back to the default).  Read them with `GET /api/v1/settings`. Every change is written to the audit log.
To add a new setting: add ONE line to `REGISTRY` in `app/services/csettings.py` and read it with `csettings.get(company, "key")`.
The screen, the limits check and this table follow that line.

| Key | Meaning | Default | Allowed | Notes |
|---|---|---|---|---|
| `write_enabled` | Allow writing to the plant | off | on / off | Master switch (write lock). OFF = nobody can send a value or command to a PLC, even with the Control right. |
| `command_expiry_seconds` | A command is cancelled if not delivered within (s) | 10 | 3 - 120 | A command that is not delivered in time is never run later. |
| `gateway_offline_seconds` | Plant PC / device 'Offline' alarm after (s) | 10 | 3 - 3600 | No heartbeat or no answer for this long raises the Offline alarm. Critical plants: 5 to 10 s. Remote or cellular sites: 30 to 60 s. |
| `device_stale_seconds` | A value turns grey ('stale') after (s) | 5 | 2 - 600 | Keep this below the offline time. Write buttons lock while data is stale. |
| `plant_poll_seconds` | Plant PC reads every device every (s) | 1.0 | 0.25 - 60.0 | Faster = fresher values on the screen but more load on the PLCs and serial cables. 1 s is normal; 0.5 s for fast processes. |
| `screen_refresh_seconds` | App screens refresh the live values every (s) | 1.0 | 0.5 - 10.0 | How fresh the numbers on the phone / PC dashboard are. Total delay = plant PC read time + about 0.6 s + this; 1 s keeps the whole path under 3 s. Higher saves mobile data. |
| `stuck_sensor_minutes` | Stuck sensor warning after (min) | 30 | 0 - 10080 | A sensor whose value never changes for this long while its device answers gets an advisory alarm. 0 = off. |
| `shelve_max_minutes` | Longest an alarm may be shelved (min) | 480 | 5 - 10080 | Shelving always ends by itself. |
| `maintenance_max_hours` | Longest maintenance mode (h) | 24 | 1 - 720 | Maintenance mode always ends by itself. |
| `alarms_per_hour_target` | Alarms per hour: target | 12 | 1 - 1000 | Shown green below this on the plant health panel (EEMUA 191 gives 12). |
| `alarms_per_hour_limit` | Alarms per hour: action limit | 18 | 1 - 1000 | Shown red above this (EEMUA 191 gives 18). |
| `stale_alarm_hours` | An alarm is 'stale' if open and unacknowledged for (h) | 24 | 1 - 720 | Stale alarms are counted on the plant health panel. |
| `spark_minutes` | Line on each tile shows the last (min) | 15 | 1 - 240 | Time window of the small line on every dashboard tile. |
| `trend_default` | Trend opens with | 8h | 1h, 8h, 24h, 7d | The range a trend chart opens with. |
| `theme_default` | Colour theme for new users | system | system, light, dark | Each person can still choose their own. |
| `share_link_max_days` | Longest a share link works (days) | 30 | 1 - 365 | A read-only link always ends by itself. |
| `write_confirm_tolerance_pct` | Write counts as confirmed if the PLC value is within (%) | 0.1 | 0.0 - 10.0 | Compared with the value asked for, after the PLC was read back. |

Guidance for the two timing settings: the **Offline** alarm (`gateway_offline_seconds`) is a debounce, not a physical constant. Critical plants with a
wired network: 5 to 10 s. Remote / cellular / satellite sites: 30 to 60 s. The grey **stale** time (`device_stale_seconds`) must stay below it
(the API refuses otherwise).

## 2. Server `.env`

| Line | What it does | Default |
|---|---|---|
| `EMAIL_ACTIVATION=true` | the company owner must confirm the e-mail with a 6-digit code before the first login. **Needs the mail settings below.** `false` = demo mode, sign-in works at once | `false` |
| `MAIL_SERVER` `MAIL_PORT` `MAIL_USERNAME` `MAIL_PASSWORD` `MAIL_FROM` (or `BREVO_API_KEY`) | the SMTP account for activation, one-time passwords, reset links, escalation and scheduled reports | empty |
| `ACTIVATION_CODE_MINUTES` | life of the activation code | 30 |
| `TEMP_PASSWORD_E_MAIL=false` | do not e-mail the one-time password; the Super admin hands it over personally | `true` |
| `TERMS_FILE=/app/terms.txt` | your own terms text shown at sign-up (mount the file). Change `TERMS_VERSION` when it changes | short default text |
| `PUBLIC_API_URL` | the public address of the server (share links, QR codes, plant script link) | empty |
| `GATEWAY_OFFLINE_SECONDS`, `DEVICE_STALE_SECONDS` | default Offline / grey times for companies that did not change them | 10 / 5 |
| `COMMAND_EXPIRY_SECONDS` | default life of a write command | 10 |
| `PLANT_POLL_SECONDS` | default read interval of the plant PCs | 1 |
| `STUCK_SENSOR_MINUTES`, `SHELVE_MAX_MINUTES`, `MAINTENANCE_MAX_HOURS` | defaults of the same-named company settings | 30 / 480 / 24 |
| `ALARMS_PER_HOUR_TARGET`, `ALARMS_PER_HOUR_LIMIT`, `STALE_ALARM_HOURS` | defaults for the plant health panel | 12 / 18 / 24 |
| `TREND_DEFAULT`, `SHARE_LINK_MAX_DAYS`, `WRITE_CONFIRM_TOLERANCE_PCT` | defaults | 8h / 30 / 0.1 |

The complete list with comments is `app/core/config.py`. A per-company value (section 1) always wins over the `.env` default.

## 3. Plant PC (`link.json`, next to `plant_gateway.py`)

| Key | What | Default |
|---|---|---|
| `poll_seconds` | how often every device is read (0.25 to 60). The company setting `plant_poll_seconds` is sent to the script with the device list and wins, so normally leave this alone | 1 |

Fast screen updates need: poll 0.5 to 1 s (setting above), heartbeat 1 s (fixed in the script), the live feed (WebSocket) instead of polling in the app, and a plant PC with a
stable network. Values travel plant PC -> broker -> server -> app without waiting for the database copy.

## 4. Writing to the plant - how to switch it on

1. Super admin: Settings -> **Allow writing to the plant** = on (it is OFF for every company by default; this is the company write lock).
2. Admin: open a sensor -> **Allow writing** and set the lowest / highest value (required for numbers; on / off points need none). Modbus coils and holding registers (also one bit of a register) and OPC-UA nodes can be written.
3. People need **Control = Read and Write** (job titles Admin, Engineer, Supervisor, Senior operator, Operator have it by default; Guest does not).
4. The app asks for confirmation, then follows the command: sent -> delivered -> written -> confirmed. A command nobody received in `command_expiry_seconds` is cancelled and **never run later**.
5. Safety: the PLC keeps its interlocks. iVIEW checks permission, lock, limits, plant PC online and fresh data before sending; the plant PC checks signature, expiry, writable flag and limits again; the value is read back and compared. Every step is in the audit log.

## 5. People and permissions

* Only the company owner (Super admin) signs up. The Super admin creates everybody else: `POST /api/v1/users` -> one-time password (shown once, also e-mailed) -> the person must choose a new password at the first login.
* Job title gives the starting levels; the Super admin can change any of the six areas (Monitor, Alarms, Configuration, Control, Reports and sharing, Manage users) between None / Read / Read and Write, at creation and later (`PUT /api/v1/users/{id}`).
* **Manage users** = Read and Write on that area. Such a person creates, changes, switches off and re-activates people only inside **his own Country / State / Region**, never an Admin, and never gives more than he has. Only the Super admin can give Manage users.
* Where to change the starting levels of a job title: `TITLES` in `app/services/perms.py` (one line per title).

## 6. Industry templates and CSV import

* Templates: `app/data/industry_templates.json` - copy a block, rename, change sensors. They appear in the app at once (Water / STP, Power meter, HVAC, Compressed air, Boiler).
* CSV: `GET /api/v1/setup/import/template.csv` for the columns; `POST /api/v1/setup/import?dry_run=true` checks, `dry_run=false` creates. Nothing is created if any row is wrong.
* Connection test: `GET /api/v1/setup/test-connection?device_ids=...` shows per device whether the plant PC reaches it and which sensors deliver values.

## 7. Upgrade from 1.1.4

Start the new image; the master tables get their new columns by themselves and each company's database is upgraded by `alembic/versions/0010_write_alarms_maintenance.py` at start. Existing users keep working (they get the rights of their old role until the Super admin changes them). Plant PCs: download the new script (3.2) from the app; old scripts keep reading but cannot write.

## New in .env (only this one line is new in 1.1.5)
`CORS_ORIGINS=` - only needed when the web app is on a different address than the API. Your existing BREVO_API_KEY, FCM_SERVICE_ACCOUNT_B64, MAIL_FROM and EMAIL_ACTIVATION stay as they are.
Web files go in the folder `web` next to docker-compose.yml.
