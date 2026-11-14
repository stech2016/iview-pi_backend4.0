"""Settings the Super admin can change for his company (Settings screen), every one with a server default.

One line per setting below: key -> (type, default, minimum, maximum, label, unit, help). To add a setting, add one line here;
the Settings screen, the limits check and the README table (README_CONFIG.md) pick it up from this list.
The server default of each comes from .env (see core/config.py) - a company that never changed it uses that value.
Values are cached for 5 seconds inside each process, so a change reaches every process within 5 seconds, no restart.
"""
import json
import threading
import time

from sqlalchemy import select

from app.core.config import settings as env
from app.core.errors import AppError

# key: (type, default, min, max, label, unit, help)    type: int | float | bool | choice (min = list of choices)
REGISTRY: dict[str, tuple] = {
    "write_enabled": ("bool", False, None, None, "Allow writing to the plant", "",
                      "Master switch (write lock). OFF = nobody can send a value or command to a PLC, even with the Control right."),
    "write_step_up": ("bool", True, None, None, "Ask for the password (or authenticator code) before every write", "",
                      "Recommended ON. The person must type their password, or the 6-digit code if 2-step login is on, for each change."),
    "write_approval": ("bool", False, None, None, "A second person must approve every write", "",
                       "OFF by default. ON = a change waits as 'Awaiting approval' until another user with Control right approves it."),
    "command_expiry_seconds": ("int", env.COMMAND_EXPIRY_SECONDS, 3, 120, "A command is cancelled if not delivered within", "s",
                               "A command that is not delivered in time is never run later."),
    "gateway_offline_seconds": ("int", env.GATEWAY_OFFLINE_SECONDS, 3, 3600, "Plant PC / device 'Offline' alarm after", "s",
                                "No heartbeat or no answer for this long raises the Offline alarm. Critical plants: 5 to 10 s. Remote or cellular sites: 30 to 60 s."),
    "device_stale_seconds": ("int", env.DEVICE_STALE_SECONDS, 2, 600, "A value turns grey ('stale') after", "s",
                             "Keep this below the offline time. Write buttons lock while data is stale."),
    "plant_poll_seconds": ("float", env.PLANT_POLL_SECONDS, 0.25, 60.0, "Plant PC reads every device every", "s",
                           "Faster = fresher values on the screen but more load on the PLCs and serial cables. 1 s is normal; 0.5 s for fast processes."),
    "screen_refresh_seconds": ("float", 1.0, 0.5, 10.0, "App screens refresh the live values every", "s",
                               "How fresh the numbers on the phone / PC dashboard are. Total delay from the plant = plant PC read time + about 0.6 s + this. "
                               "1 s keeps the whole path under 3 s (the promise). Higher saves mobile data, lower is faster."),
    "stuck_sensor_minutes": ("int", env.STUCK_SENSOR_MINUTES, 0, 10080, "Stuck sensor warning after", "min",
                             "A sensor whose value never changes for this long while its device answers gets an advisory alarm. 0 = off."),
    "shelve_max_minutes": ("int", env.SHELVE_MAX_MINUTES, 5, 10080, "Longest an alarm may be shelved", "min", "Shelving always ends by itself."),
    "maintenance_max_hours": ("int", env.MAINTENANCE_MAX_HOURS, 1, 720, "Longest maintenance mode", "h", "Maintenance mode always ends by itself."),
    "alarms_per_hour_target": ("int", env.ALARMS_PER_HOUR_TARGET, 1, 1000, "Alarms per hour: target", "", "Shown green below this on the plant health panel (EEMUA 191 gives 12)."),
    "alarms_per_hour_limit": ("int", env.ALARMS_PER_HOUR_LIMIT, 1, 1000, "Alarms per hour: action limit", "", "Shown red above this (EEMUA 191 gives 18)."),
    "stale_alarm_hours": ("int", env.STALE_ALARM_HOURS, 1, 720, "An alarm is 'stale' if open and unacknowledged for", "h", "Stale alarms are counted on the plant health panel."),
    "spark_minutes": ("int", env.SPARK_MINUTES, 1, 240, "Line on each tile shows the last", "min", "Time window of the small line on every dashboard tile."),
    "trend_default": ("choice", env.TREND_DEFAULT, ["1h", "8h", "24h", "7d"], None, "Trend opens with", "", "The range a trend chart opens with."),
    "theme_default": ("choice", "system", ["system", "light", "dark"], None, "Colour theme for new users", "", "Each person can still choose their own."),
    "share_link_max_days": ("int", env.SHARE_LINK_MAX_DAYS, 1, 365, "Longest a share link works", "days", "A read-only link always ends by itself."),
    "write_confirm_tolerance_pct": ("float", env.WRITE_CONFIRM_TOLERANCE_PCT, 0.0, 10.0, "Write counts as confirmed if the PLC value is within", "%",
                                    "Compared with the value asked for, after the PLC was read back."),
}

_CACHE: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()
TTL = 5.0


def _load(company: str) -> dict:
    from app.db.session import MasterSession
    from app.models.master import CompanySettings
    with MasterSession() as m:
        row = m.scalar(select(CompanySettings).where(CompanySettings.company_name == company))
        try:
            return json.loads(row.values) if row else {}
        except ValueError:
            return {}


def overrides(company: str) -> dict:
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(company)
        if hit and hit[0] > now:
            return hit[1]
    data = _load(company)
    with _LOCK:
        if len(_CACHE) > 1000:
            _CACHE.clear()
        _CACHE[company] = (now + TTL, data)
    return data


def invalidate(company: str) -> None:
    with _LOCK:
        _CACHE.pop(company, None)


def get(company: str, key: str):
    """The company's value of a setting (its own, else the server default)."""
    default = REGISTRY[key][1]
    value = overrides(company).get(key, default)
    return value


def clean(key: str, value):
    """The value checked against the limits of its setting; raises a plain AppError when it does not fit."""
    if key not in REGISTRY:
        raise AppError(422, "UNKNOWN_SETTING", f"There is no setting called '{key}'.")
    kind, _default, lo, hi, label, unit, _help = REGISTRY[key]
    bad = AppError(422, "SETTING_OUT_OF_RANGE", f"'{label}' must be " + (f"one of {', '.join(lo)}." if kind == "choice" else
                   "on or off." if kind == "bool" else f"between {lo} and {hi}{(' ' + unit) if unit else ''}."))
    if kind == "bool":
        if not isinstance(value, bool):
            raise bad
        return value
    if kind == "choice":
        if value not in lo:
            raise bad
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise bad
    value = int(value) if kind == "int" else float(value)
    if not (lo <= value <= hi):
        raise bad
    return value


def as_list(company: str) -> list[dict]:
    mine = overrides(company)
    out = []
    for key, (kind, default, lo, hi, label, unit, help_) in REGISTRY.items():
        out.append({"key": key, "type": kind, "label": label, "unit": unit, "help": help_, "default": default,
                    "value": mine.get(key, default), "changed": key in mine,
                    "min": lo if kind in ("int", "float") else None, "max": hi,
                    "choices": lo if kind == "choice" else None})
    return out
