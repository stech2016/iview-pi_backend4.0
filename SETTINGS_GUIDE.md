# STECH iVIEW backend - what you can change, and where

Almost everything is a setting in the `.env` file on the server (folder `/home/stechiview/backend`). Change the line, then restart:
`docker compose up -d backend ingest` (or run `sh scripts/update.sh`). No code edit and no new image is needed for these.

| What | Setting in `.env` | Default |
|---|---|---|
| Server's own public address (reset links, plant script) | `REMOTE_ADDRESS` | empty |
| Allowed web addresses (web app) | `CORS_ORIGINS` (comma separated) | localhost:3000 |
| Plant PC / device "Offline" alarm after (seconds) | `GATEWAY_OFFLINE_SECONDS` | 10 |
| Value turns grey ("stale") after (seconds) | `DEVICE_STALE_SECONDS` | 5 |
| Minutes shown in the small line on each tile | `SPARK_MINUTES` | 15 |
| Login lasts (minutes) / "keep me signed in" (days) | `ACCESS_TOKEN_EXPIRE_MINUTES` / `REMEMBER_DAYS` | 30 / 30 |
| Plant time zone for reports (minutes from UTC, 330 = IST) | `PLANT_UTC_OFFSET_MINUTES` | 330 |
| Hour of the nightly clean-up | `PURGE_HOUR` | 2 |
| PLC read interval / timeout (seconds) | `POLL_INTERVAL_SECONDS` / `POLL_TIMEOUT_SECONDS` | 10 / 3 |
| Oldest app version still accepted | `MIN_APP_VERSION` | 1.0.0 |
| E-mail | `MAIL_*`, `BREVO_API_KEY`, `MAIL_FROM` | empty |
| Phone notifications | `FCM_SERVICE_ACCOUNT_B64` | empty |
| SMS / voice | `SMS_*` | empty |
| E-mail activation for the owner sign-up | `EMAIL_ACTIVATION` | false |
| Database host / SSL | `PG_HOST`, `PG_PORT`, `PG_SSLMODE` | local |
| Secret keys (never share) | `SECRET_KEY`, `FERNET_KEY`, `AUDIT_HMAC_KEY` | required |

Full list with comments: `app/core/config.py`. **Per-company settings and the full 1.1.5 guide: `README_CONFIG.md`.**

## Fixed in code (one file each, needs a new image)
| What | File |
|---|---|
| Roles, regions, State list for India | `app/core/constants.py` |
| Countries and States for the rest of the world | automatic (pycountry), `app/core/geo.py` |
| Plant PCs allowed per State + Region | `MAX_PER_REGION` in `app/api/v1/gateway.py` |
| Points drawn in each tile line | `SPARK_POINTS` in `app/api/v1/devices.py` |
| Password rules | `app/schemas/auth.py` |

## Adding a new feature later
New screen data = one new file in `app/api/v1/` + one name added in `app/api/v1/router.py`. Database changes = one new file in `alembic/versions/` (next number, 0011...). Tests: `python -m pytest tests -q`.

| `write_step_up` (Company settings) | ask password / 2-step code for every write; default on |
| `write_approval` (Company settings) | a second person must approve each write; default off |
