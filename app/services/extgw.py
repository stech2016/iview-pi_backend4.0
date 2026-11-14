"""V2.0 - customer-owned gateways (any brand) talk to the cloud broker with plain MQTT: JSON or Sparkplug B.

    gateway  --(TLS, port 8883, own login)-->  broker  -->  this module  -->  Detected list  /  readings of the chosen points
    cloud    --(write command)-->  broker  -->  gateway (the customer mapped the command topic to the PLC address)

Rules of the road
  * The broker lets a gateway touch only  iview/<plant id>/#  and  spBv1.0/<plant id>/#  (see brokeradmin.py), so the plant id in the
    topic is trusted: no signature is needed (a customer gateway cannot sign).
  * Nothing a gateway sends is stored or shown until a Super admin / Regional admin ticks it in the Detected list. Ticked points become a
    normal device with sensors (protocol 'MQTT'): tiles, alarms, reports, trends work exactly as before.
  * A sensor of such a device is tied to its gateway by   topic = 'ext:<plant id>:<device name used by the gateway>'   and
    json_field = '<tag name used by the gateway>'.
  * A write is only a published message. The cloud calls it CONFIRMED when the next reading of that point shows the new value.
"""
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import redis
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.timeutil import utcnow
from app.db.session import MasterSession, tenant_session
from app.models.master import PlantLink, User, CompanyStorage
from app.models.tenant import Alarm, Command, DetectedTag, Device, DeviceRegister, Notification
from app.services import live, push, sparkplug as sp
from app.services.alarms import alarm_dict, place_text
from app.services.ingest import ingest_readings

log = get_logger("extgw")

EXT_PREFIX = "ext:"                 # Command.gateway_id and Device.plant_pc of a customer gateway: 'ext:<plant id>'
MAX_TAGS_PER_MESSAGE = 500
MAX_DETECTED_PER_PLANT = 2000
MAX_TAG_LEN = 64
MAX_DEVICE_LEN = 120
DETECTED_REFRESH_SECONDS = 30
LINK_CACHE_SECONDS = 10
RESERVED_TOPIC_WORDS = {"status", "cmd", "cmdack", "hb"}
DATA_WORDS = {"data", "telemetry", "tags", "values", "state", "readings"}
TS_KEYS = ("ts", "timestamp", "time", "t")
DEVICE_KEYS = ("device", "deviceName", "device_name", "deviceId", "device_id", "asset", "node")
CONTAINER_KEYS = ("tags", "data", "values", "metrics", "points", "readings", "d", "payload")
NAME_KEYS = ("name", "n", "tag", "id", "key", "metric", "label", "point")
VALUE_KEYS = ("value", "v", "val", "data", "reading", "pv")
SKIP_KEYS = set(TS_KEYS) | set(DEVICE_KEYS) | {"quality", "q", "status", "seq", "uid", "unit", "units", "description"}
PLANT_RE = re.compile(r"^pl[0-9a-f]{10}$")


# ================================================================================================ small helpers
def is_ext_gid(gid: str | None) -> bool:
    return bool(gid) and str(gid).startswith(EXT_PREFIX)


def plant_of_gid(gid: str) -> str:
    return gid[len(EXT_PREFIX):]


def reg_topic(plant_id: str, ext_device: str) -> str:
    return f"{EXT_PREFIX}{plant_id}:{ext_device}"


def split_reg_topic(topic: str | None) -> tuple[str, str] | None:
    """'ext:<plant>:<device>' -> (plant, device); anything else -> None."""
    if not topic or not topic.startswith(EXT_PREFIX):
        return None
    plant, _, device = topic[len(EXT_PREFIX):].partition(":")
    return (plant, device) if PLANT_RE.match(plant) else None


def is_ext_register(reg: DeviceRegister | None) -> bool:
    return bool(reg and split_reg_topic(reg.topic) and reg.json_field)


def _clean_name(text_: object, limit: int) -> str | None:
    if not isinstance(text_, str):
        return None
    t = "".join(ch for ch in text_ if ch.isprintable()).strip()
    return t[:limit] if t and len(t) <= limit else None      # too long -> skipped (cutting would make two tags look the same)


def metric_name(text_: object) -> str | None:
    """The sensor name in the app: letters, numbers, dot, dash, underscore (the app's rule for every sensor). Spaces and other signs become '_'."""
    if not isinstance(text_, str):
        return None
    t = re.sub(r"[^A-Za-z0-9_.\-]+", "_", text_.strip()).strip("_")
    return t[:64] or None


def _number(v: object) -> float | None:
    """A reading as a number: numbers, booleans (1 / 0), numeric text and the words true / false. Everything else is not a reading."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        t = v.strip().lower()
        if t in ("true", "false"):
            return 1.0 if t == "true" else 0.0
        try:
            f = float(t)
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def _when(v: object) -> datetime | None:
    """ISO text, or epoch seconds / milliseconds -> UTC time. Anything else (or a time far in the future) -> None (the arrival time is used)."""
    try:
        if isinstance(v, bool) or v is None:
            return None
        if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\d+(\.\d+)?", v.strip())):
            f = float(v)
            f = f / 1000.0 if f > 1e11 else f
            ts = datetime.fromtimestamp(f, tz=timezone.utc)
        elif isinstance(v, str):
            ts = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            ts = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    if ts > utcnow() + timedelta(minutes=5) or ts.year < 2000:
        return None
    return ts


@dataclass
class Reading:
    device: str
    tag: str
    value: float
    ts: datetime | None
    kind: str = "number"      # number | bool


def _kind(v: object) -> str:
    return "bool" if isinstance(v, bool) or (isinstance(v, str) and v.strip().lower() in ("true", "false")) else "number"


# ================================================================================================ JSON layouts (auto-detected)
def _walk(obj: dict, prefix: str, depth: int, out: list, ts_default):
    for k, v in obj.items():
        if not isinstance(k, str):
            continue
        if depth == 0 and k in SKIP_KEYS:
            continue
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            if depth < 2:
                _walk(v, name + ".", depth + 1, out, ts_default)
        elif isinstance(v, list):
            continue
        else:
            n = _number(v)
            if n is not None:
                out.append((name, n, ts_default, _kind(v)))


def _from_list(items: list, ts_default) -> list:
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = next((it[k] for k in NAME_KEYS if isinstance(it.get(k), str)), None)
        raw = next((it[k] for k in VALUE_KEYS if k in it and not isinstance(it[k], (dict, list))), None)
        n = _number(raw)
        if name is None or n is None:
            continue
        out.append((name, n, _when(next((it[k] for k in TS_KEYS if k in it), None)) or ts_default, _kind(raw)))
    return out


def parse_json(rest: list[str], body: object, default_device: str) -> list[Reading]:
    """rest = the topic below  iview/<plant id>/ .  Handles: a bare value (topic = device/tag), flat {tag: value}, a list of
    {name, value}, a wrapper {device, ts, tags|data|values: ...}, and nested objects (names joined with '.')."""
    words = list(rest)
    if words and words[-1].lower() in DATA_WORDS:
        words = words[:-1]
    hint = "/".join(words)
    found: list[tuple] = []
    device = hint
    if not isinstance(body, (dict, list)):
        n = _number(body)
        if n is None or not words:
            return []
        dev, tag = ("/".join(words[:-1]), words[-1]) if len(words) > 1 else ("", words[0])
        found, device = [(tag, n, None, _kind(body))], dev
    elif isinstance(body, list):
        found = _from_list(body, None)
    else:
        ts_default = _when(next((body[k] for k in TS_KEYS if k in body), None))
        if not device:
            device = next((body[k] for k in DEVICE_KEYS if isinstance(body.get(k), str) and body[k].strip()), "")
        box = next((body[k] for k in CONTAINER_KEYS if isinstance(body.get(k), (dict, list))), None)
        if isinstance(box, list):
            found = _from_list(box, ts_default)
        elif isinstance(box, dict):
            _walk(box, "", 1, found, ts_default)       # (a wrapper key is not part of the tag name; its own keys are not skipped)
        else:
            key = "value" if "value" in body else "v" if "v" in body else None
            if key and words and all(k == key or k in SKIP_KEYS for k in body):      # {"value": 5, "ts": ..} on topic .../device/tag
                n = _number(body[key])
                if n is not None:
                    dev, tag = ("/".join(words[:-1]), words[-1]) if len(words) > 1 else ("", words[0])
                    found, device = [(tag, n, ts_default, _kind(body[key]))], dev
            else:
                _walk(body, "", 0, found, ts_default)
    out, seen = [], set()
    dev_name = _clean_name(device, MAX_DEVICE_LEN) or default_device
    for tag, n, ts, kind in found[:MAX_TAGS_PER_MESSAGE]:
        tag = _clean_name(tag, MAX_TAG_LEN)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(Reading(dev_name, tag, n, ts, kind))
    return out


# ================================================================================================ Sparkplug B (alias memory in Valkey)
_redis_c: redis.Redis | None = None
_ALIAS_MEM: dict[str, dict[int, str]] = {}
_REBIRTH_SENT: dict[tuple, float] = {}


def _r() -> redis.Redis:
    global _redis_c
    if _redis_c is None:
        _redis_c = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    return _redis_c


def _alias_key(plant: str, edge: str, device: str) -> str:
    return f"spalias:{plant}:{edge}:{device}"


def _alias_save(plant: str, edge: str, device: str, names: dict[int, str]) -> None:
    key = _alias_key(plant, edge, device)
    _ALIAS_MEM[key] = names
    if len(_ALIAS_MEM) > 5000:
        _ALIAS_MEM.clear()
        _ALIAS_MEM[key] = names
    try:
        pipe = _r().pipeline()
        pipe.delete(key)
        if names:
            pipe.hset(key, mapping={str(a): n for a, n in names.items()})
            pipe.expire(key, 90 * 86400)
        pipe.execute()
    except Exception as exc:
        log.warning("alias_store_failed", error=type(exc).__name__)


def _alias_load(plant: str, edge: str, device: str) -> dict[int, str]:
    key = _alias_key(plant, edge, device)
    hit = _ALIAS_MEM.get(key)
    if hit is not None:
        return hit
    try:
        raw = _r().hgetall(key)
        names = {int(a.decode()): n.decode() for a, n in raw.items()}
    except Exception:
        names = {}
    if names:
        _ALIAS_MEM[key] = names
    return names


def _control_metric(name: str) -> bool:
    return name.startswith(("Node Control/", "Device Control/", "Properties/")) or name in ("bdSeq",)


def parse_sparkplug(plant_id: str, topic: sp.Topic, payload: bytes) -> tuple[list[Reading], bool]:
    """Returns (readings, needs_rebirth). NBIRTH / DBIRTH teach the alias numbers; NDATA / DDATA may use them instead of names."""
    pl = sp.decode(payload)
    device = topic.device or topic.edge
    names: dict[int, str] = {}
    birth = topic.kind in ("NBIRTH", "DBIRTH")
    if birth:
        for m in pl.metrics:
            if m.name is not None and m.alias is not None:
                names[m.alias] = m.name
        _alias_save(plant_id, topic.edge, device, names)
    else:
        names = _alias_load(plant_id, topic.edge, device)
    out, rebirth, seen = [], False, set()
    base_ts = datetime.fromtimestamp(pl.timestamp / 1000.0, tz=timezone.utc) if pl.timestamp and pl.timestamp > 0 else None
    for m in pl.metrics:
        name = m.name if m.name is not None else names.get(m.alias) if m.alias is not None else None
        if name is None:
            rebirth = rebirth or m.alias is not None
            continue
        name = _clean_name(name, MAX_TAG_LEN)
        n = m.as_number()
        if not name or n is None or m.historical or _control_metric(name) or name in seen:
            continue
        seen.add(name)
        ts = None
        for cand in (m.timestamp, pl.timestamp):
            if cand and cand > 0:
                try:
                    t = datetime.fromtimestamp(cand / 1000.0, tz=timezone.utc)
                except (OverflowError, OSError, ValueError):
                    continue
                if utcnow() - timedelta(days=3650) < t <= utcnow() + timedelta(minutes=5):
                    ts = t
                    break
        out.append(Reading(_clean_name(device, MAX_DEVICE_LEN) or topic.edge, name, n, ts or base_ts,
                           "bool" if m.datatype == sp.BOOLEAN else "number"))
        if len(out) >= MAX_TAGS_PER_MESSAGE:
            break
    return out, rebirth


# ================================================================================================ links (cached)
_LINKS: dict[str, tuple[float, dict | None]] = {}
_LINK_LOCK = threading.Lock()


def link_dict(row: PlantLink) -> dict:
    return {"company": row.company_name, "plant_id": row.plant_id, "name": row.name, "state": row.site_state, "region": row.site_region,
            "enabled": row.enabled, "cmd_format": row.cmd_format, "last_format": row.last_format, "sp_edge": row.sp_edge}


def get_link(plant_id: str) -> dict | None:
    if not PLANT_RE.match(plant_id or ""):
        return None
    now = time.monotonic()
    with _LINK_LOCK:
        hit = _LINKS.get(plant_id)
        if hit and hit[0] > now:
            return hit[1]
    with MasterSession() as master:
        row = master.scalar(select(PlantLink).where(PlantLink.plant_id == plant_id))
        val = link_dict(row) if row else None
    with _LINK_LOCK:
        if len(_LINKS) > 5000:
            _LINKS.clear()
        _LINKS[plant_id] = (now + LINK_CACHE_SECONDS, val)
    return val


def forget_link(plant_id: str) -> None:
    with _LINK_LOCK:
        _LINKS.pop(plant_id, None)


def offline_after(company: str) -> int:
    return max(30, settings.PLANT_LINK_OFFLINE_SECONDS)


def is_online(row: PlantLink | None) -> bool:
    return bool(row and row.enabled and row.state == "online" and row.last_seen
                and utcnow() - row.last_seen < timedelta(seconds=offline_after(row.company_name)))


_TOUCHED: dict[str, float] = {}


def touch(plant_id: str, link: dict, fmt: str | None = None, edge: str | None = None) -> None:
    """The gateway sent something: online. The database row is written at most every 5 s (or when something changed)."""
    now = time.monotonic()
    changed = (fmt and fmt != link.get("last_format")) or (edge and edge != link.get("sp_edge"))
    if not changed and now - _TOUCHED.get(plant_id, 0) < 5:
        return
    _TOUCHED[plant_id] = now
    was = None
    with MasterSession() as master:
        row = master.scalar(select(PlantLink).where(PlantLink.plant_id == plant_id))
        if not row:
            return
        was = row.state
        row.last_seen = utcnow()
        if fmt:
            row.last_format = fmt
        if edge:
            row.sp_edge = edge[:64]
        if was != "online":
            row.state, row.changed_at = "online", utcnow()
        master.commit()
        site, name = (row.site_state, row.site_region), row.name
    if changed:
        forget_link(plant_id)
    if was != "online":
        log.info("plant_link_online", plant=plant_id)
        _announce(link["company"], plant_id, True, site)
        if was == "offline":
            _clear_alarm(link["company"], plant_id, name)


def mark_offline(plant_id: str, why: str) -> None:
    with MasterSession() as master:
        row = master.scalar(select(PlantLink).where(PlantLink.plant_id == plant_id))
        if not row or row.state != "online":
            return
        row.state, row.changed_at = "offline", utcnow()
        master.commit()
        company, site, name = row.company_name, (row.site_state, row.site_region), row.name
    _TOUCHED.pop(plant_id, None)
    log.warning("plant_link_offline", plant=plant_id, why=why)
    _announce(company, plant_id, False, site)
    _raise_alarm(company, plant_id, name, site, why)


def _announce(company: str, plant_id: str, online: bool, site: tuple) -> None:
    live.publish(company, {"type": "gateway", "online": online, "gateway_id": EXT_PREFIX + plant_id, "state": site[0] or "ALL", "region": site[1] or "ALL"})


def watchdog() -> None:
    """Nothing heard for the offline time -> Offline + one critical alarm for the people of that State + Region. Runs forever (own thread)."""
    while True:
        try:
            limit = utcnow() - timedelta(seconds=offline_after(""))
            with MasterSession() as master:
                stale = [r.plant_id for r in master.scalars(select(PlantLink).where(PlantLink.state == "online", PlantLink.enabled.is_(True)))
                         if r.last_seen is None or r.last_seen < limit]
            for pid in stale:
                mark_offline(pid, "no data")
        except Exception as exc:
            log.warning("plant_watchdog_failed", error=type(exc).__name__)
        time.sleep(5)


# ------------------------------------------------------------------ the alarm for the people of the gateway's region
def _pseudo_name(name: str) -> str:
    return f"Plant Gateway - {name}"[:120]


def _raise_alarm(company: str, plant_id: str, name: str, site: tuple, why: str) -> None:
    try:
        with MasterSession() as master:
            if not master.scalar(select(CompanyStorage.id).where(CompanyStorage.company_name == company, CompanyStorage.db_name.is_not(None))):
                return
            who = select(User.id).where(User.company_name == company, User.status == "approved",
                                        or_(User.role == "admin", (User.state == site[0]) & (User.region == site[1])))
            users = list(master.scalars(who))
            with tenant_session(master, company) as db:
                pname = _pseudo_name(name)
                dev = db.scalar(select(Device).where(Device.protocol == "Gateway", Device.device_name == pname))
                if not dev:
                    dev = Device(device_name=pname, protocol="Gateway", ip_address="cloud", port=0, company_name=company,
                                 state=site[0] or "ALL", region=site[1] or "ALL")
                    db.add(dev)
                    db.flush()
                if db.scalar(select(Alarm.id).where(Alarm.device_id == dev.id, Alarm.cleared_at.is_(None))):
                    return
                msg = (f"The customer gateway '{name}' sent nothing for a while. Live data from this plant is not reaching the cloud. "
                       "Check the gateway's power, its internet and its MQTT settings.")
                alarm = Alarm(device_id=dev.id, metric_name="gateway", severity="critical", message=msg, raised_at=utcnow())
                db.add(alarm)
                db.flush()
                for uid in users:
                    db.add(Notification(user_id=uid, alarm_id=alarm.id, channel="in_app", sent_at=utcnow()))
                db.commit()
                live.publish(company, {"type": "alarm", "event": "raised", "alarm": alarm_dict(alarm, dev.device_name, place_text(dev.city, dev.plant_name)),
                                       "state": site[0] or "ALL", "region": site[1] or "ALL"})
                push.notify(company, users, push.alarm_title("critical", dev.device_name), msg,
                            {"alarm_id": alarm.id, "severity": "critical", "device_id": dev.id})
    except Exception as exc:
        log.warning("plant_alarm_failed", plant=plant_id, error=type(exc).__name__)


def _clear_alarm(company: str, plant_id: str, name: str) -> None:
    try:
        with MasterSession() as master, tenant_session(master, company) as db:
            dev = db.scalar(select(Device).where(Device.protocol == "Gateway", Device.device_name == _pseudo_name(name)))
            if not dev:
                return
            for a in db.scalars(select(Alarm).where(Alarm.device_id == dev.id, Alarm.cleared_at.is_(None))):
                a.cleared_at = utcnow()
                db.commit()
                live.publish(company, {"type": "alarm", "event": "cleared", "alarm": alarm_dict(a, dev.device_name, place_text(dev.city, dev.plant_name)),
                                       "state": dev.state, "region": dev.region})
    except Exception as exc:
        log.warning("plant_alarm_clear_failed", plant=plant_id, error=type(exc).__name__)


# ================================================================================================ message intake
def parse_message(topic: str, payload: bytes) -> tuple[str, str, list[Reading], dict] | None:
    """-> (plant id, format, readings, extra) or None when the message is not one of ours / not understood.
    extra: {'status': True|False} for a status message, {'rebirth': (edge)} when a Sparkplug alias is unknown, {'death': True}, {'edge': id}."""
    parts = topic.split("/")
    if len(parts) >= 2 and parts[0] == "iview":
        plant = parts[1]
        if not PLANT_RE.match(plant):
            return None
        rest = parts[2:]
        if rest and rest[0] in ("cmd", "cmdack"):
            return None              # our own commands (the backend hears itself) and the gateway's optional answers
        if rest and rest[0] in ("status", "hb") and len(rest) == 1:
            return plant, "json", [], {"status": _status_of(payload)}
        body: object
        raw = payload.decode("utf-8", "replace").strip()
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw
        return plant, "json", parse_json(rest, body, "Gateway"), {}
    if len(parts) >= 4 and parts[0] == sp.NAMESPACE:
        t = sp.parse_topic(topic)
        if not t or not PLANT_RE.match(t.group):
            return None
        if t.kind in ("NCMD", "DCMD", "STATE"):
            return None
        if t.kind == "NDEATH":
            return t.group, "sparkplug", [], {"death": True, "edge": t.edge}
        if t.kind == "DDEATH":
            return t.group, "sparkplug", [], {"edge": t.edge}
        try:
            readings, rebirth = parse_sparkplug(t.group, t, payload)
        except sp.SparkplugError:
            return None
        return t.group, "sparkplug", readings, {"edge": t.edge, "rebirth": t.edge if rebirth else None}
    return None


def _status_of(payload: bytes) -> bool | None:
    raw = payload.decode("utf-8", "replace").strip()
    try:
        body = json.loads(raw)
    except ValueError:
        body = raw
    if isinstance(body, dict):
        body = next((body[k] for k in ("status", "state", "online") if k in body), None)
    if isinstance(body, bool):
        return body
    if isinstance(body, (int, float)):
        return bool(body)
    if isinstance(body, str):
        t = body.strip().lower()
        if t in ("online", "on", "1", "true", "up", "connected"):
            return True
        if t in ("offline", "off", "0", "false", "down", "disconnected", "dead"):
            return False
    return None


def request_rebirth(plant_id: str, edge: str) -> None:
    """Standard Sparkplug: NCMD with 'Node Control/Rebirth' = true makes the edge node publish its birth again (at most once a minute per node)."""
    key = (plant_id, edge)
    if time.monotonic() - _REBIRTH_SENT.get(key, 0) < 60:
        return
    _REBIRTH_SENT[key] = time.monotonic()
    from app.services import gwcontrol
    body = sp.encode([sp.Metric("Node Control/Rebirth", datatype=sp.BOOLEAN, value=True)], seq=0)
    gwcontrol.publish_message(sp.topic_for(plant_id, "NCMD", edge), body)


def process(items: list[tuple[str, bytes]]) -> None:
    """items = (topic, payload) in arrival order. Everything is parsed here, then stored per gateway."""
    per_plant: dict[str, list[Reading]] = {}
    fmt_of: dict[str, str] = {}
    edge_of: dict[str, str] = {}
    for topic, payload in items:
        try:
            parsed = parse_message(topic, payload)
        except Exception as exc:        # one strange message must never stop the others
            log.warning("ext_message_failed", error=type(exc).__name__)
            continue
        if not parsed:
            continue
        plant, fmt, readings, extra = parsed
        link = get_link(plant)
        if not link or not link["enabled"]:
            continue                    # (the broker login is disabled too; this covers a message already queued)
        if "status" in extra:
            if extra["status"] is False:
                mark_offline(plant, "gateway said offline")
            elif extra["status"] is True:
                touch(plant, link, fmt)
            continue
        if extra.get("death"):
            mark_offline(plant, "Sparkplug NDEATH")
            continue
        if extra.get("rebirth"):
            request_rebirth(plant, extra["rebirth"])
        touch(plant, link, fmt, extra.get("edge") if fmt == "sparkplug" else None)
        fmt_of[plant], edge_of[plant] = fmt, extra.get("edge") or ""
        if readings:
            per_plant.setdefault(plant, []).extend(readings)
    for plant, readings in per_plant.items():
        link = get_link(plant)
        if link:
            _store(link, readings)


_DET_SEEN: dict[tuple, float] = {}


def _store(link: dict, readings: list[Reading]) -> None:
    company, plant = link["company"], link["plant_id"]
    pause = 1.0
    for attempt in range(6):
        try:
            _store_once(company, plant, link, readings)
            return
        except AppError as exc:
            log.warning("ext_rejected", code=exc.code)
            return
        except Exception as exc:
            from sqlalchemy.exc import OperationalError
            if isinstance(exc, OperationalError) and attempt < 5:      # the database is busy / restarting: keep the readings, try again
                time.sleep(pause)
                pause = min(pause * 2, 10)
                continue
            log.warning("ext_store_failed", plant=plant, error=type(exc).__name__, detail=str(exc)[:160])
            return


def _store_once(company: str, plant: str, link: dict, readings: list[Reading]) -> None:
    by_dev: dict[str, list[Reading]] = {}
    for r in readings:
        by_dev.setdefault(r.device, []).append(r)
    with MasterSession() as master, tenant_session(master, company) as db:
        topics = [reg_topic(plant, d) for d in by_dev]
        regs: dict[tuple[str, str], DeviceRegister] = {}
        for reg in db.scalars(select(DeviceRegister).where(DeviceRegister.topic.in_(topics))):
            regs[(reg.topic, reg.json_field)] = reg
        devices = {d.id: d for d in db.scalars(select(Device).where(Device.id.in_({r.device_id for r in regs.values()}), Device.company_name == company))} if regs else {}
        per_device: dict[int, list] = {}
        unknown: list[Reading] = []
        for dev_name, rows in by_dev.items():
            topic = reg_topic(plant, dev_name)
            for r in rows:
                reg = regs.get((topic, r.tag))
                device = devices.get(reg.device_id) if reg else None
                if device:
                    per_device.setdefault(device.id, []).append((reg.metric_name, r.value, r.ts))
                else:
                    unknown.append(r)
        if unknown:
            _list_detected(db, plant, unknown)
        for device_id, rows in per_device.items():
            device = devices[device_id]
            try:
                ingest_readings(db, master, device, rows)
            except AppError as exc:
                log.warning("ext_rejected", code=exc.code)
                db.rollback()
                continue
            _confirm_commands(db, company, device, rows)


def _list_detected(db: Session, plant: str, readings: list[Reading]) -> None:
    """Unchosen points go to the Detected list (name + one sample value; nothing else is kept)."""
    now_m = time.monotonic()
    fresh = [r for r in readings if now_m - _DET_SEEN.get((plant, r.device, r.tag), 0) >= DETECTED_REFRESH_SECONDS]
    if not fresh:
        return
    have = db.scalar(select(func.count(DetectedTag.id)).where(DetectedTag.plant_id == plant)) or 0
    room = max(0, MAX_DETECTED_PER_PLANT - have)
    now = utcnow()
    known = {(d, t) for d, t in db.execute(select(DetectedTag.ext_device, DetectedTag.tag).where(DetectedTag.plant_id == plant))} if have else set()
    added = 0
    for r in fresh:
        is_new = (r.device, r.tag) not in known
        if is_new:
            if added >= room:
                continue
            added += 1
        stmt = pg_insert(DetectedTag).values(plant_id=plant, ext_device=r.device[:MAX_DEVICE_LEN], tag=r.tag, sample=f"{r.value:g}"[:40],
                                             kind=r.kind, count=1, first_seen=now, last_seen=now, dismissed=False)
        db.execute(stmt.on_conflict_do_update(constraint="uq_detected_tag", set_={
            "sample": stmt.excluded.sample, "last_seen": now, "kind": stmt.excluded.kind, "count": DetectedTag.count + 1}))
        _DET_SEEN[(plant, r.device, r.tag)] = now_m
    db.commit()
    if len(_DET_SEEN) > 50000:
        _DET_SEEN.clear()
    if added < sum(1 for r in fresh if (r.device, r.tag) not in known):
        log.warning("detected_list_full", plant=plant, limit=MAX_DETECTED_PER_PLANT)


# ================================================================================================ writing
def _seg(text_: str) -> str:
    return text_.replace("/", ".").replace("+", "_").replace("#", "_")


def command_topic_json(plant_id: str, ext_device: str, tag: str) -> str:
    return f"iview/{plant_id}/cmd/{_seg(ext_device)}/{_seg(tag)}"


def publish_command(company: str, cmd: Command, device: Device, reg: DeviceRegister) -> None:
    """Sends the write as an ordinary MQTT message to the gateway's command topic. Nothing is signed (the customer's gateway cannot check it);
    the broker login of the plant is what keeps other plants from sending or hearing it."""
    from app.services import gwcontrol
    where = split_reg_topic(reg.topic)
    if not where or not reg.json_field:
        raise AppError(409, "NOT_A_GATEWAY_POINT", "This sensor does not belong to a customer gateway.")
    plant, ext_device = where
    link = get_link(plant)
    if not link or link["company"] != company or not link["enabled"]:
        raise AppError(409, "GATEWAY_NOT_FOUND", "The gateway of this device was not found or is switched off.")
    fmt = link["cmd_format"] if link["cmd_format"] in ("json", "sparkplug") else (link["last_format"] or "json")
    tag, raw = reg.json_field, cmd.raw_value
    if fmt == "sparkplug":
        edge = link["sp_edge"]
        if not edge:
            raise AppError(409, "GATEWAY_NO_EDGE", "The gateway has not sent a Sparkplug message yet, so the cloud does not know its node name. Wait for its first message.")
        metric = sp.Metric(tag, datatype=sp.datatype_for_register(reg.data_type), value=bool(raw) if reg.data_type == "bool" else raw)
        body = sp.encode([metric], seq=0)
        if ext_device == edge:
            topic = sp.topic_for(plant, "NCMD", edge)
        else:
            topic = sp.topic_for(plant, "DCMD", edge, ext_device)
    else:
        exp = cmd.expires_at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        ts = utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")
        body = json.dumps({"uid": cmd.uid, "device": ext_device, "tag": tag, "value": raw, "expires": exp, "ts": ts}, separators=(",", ":"))
        topic = command_topic_json(plant, ext_device, tag)
    if not gwcontrol.publish_message(topic, body):
        raise AppError(503, "COMMAND_CHANNEL_DOWN", "The command channel to the plants is not connected right now. Nothing was sent. Try again in a moment.")


def _confirm_commands(db: Session, company: str, device: Device, rows: list[tuple]) -> None:
    """A write to a customer gateway is confirmed when the next reading of that point shows the new value."""
    pending = list(db.scalars(select(Command).where(Command.device_id == device.id, Command.state.in_(("sent", "delivered")),
                                                    Command.gateway_id.like(EXT_PREFIX + "%"))))
    if not pending:
        return
    from app.services import commands, csettings
    latest = {m: v for m, v, _ in sorted(rows, key=lambda x: x[2] or utcnow())}
    tol = csettings.get(company, "write_confirm_tolerance_pct") / 100.0
    for c in pending:
        back = latest.get(c.metric_name)
        if back is None:
            continue
        if abs(back - c.value) <= max(1e-9, tol * abs(c.value), 1e-6):
            commands._finish(db, company, c, "confirmed", None, back)
            if c.pulse and c.value == 1:
                threading.Timer(commands.PULSE_SECONDS, commands.release_pulse, (company, c.uid)).start()


# ================================================================================================ Detected list -> devices (used by the API)
def accept(db: Session, link: PlantLink, items: list[dict], by: str) -> dict:
    """items: [{id, name?, unit?, device_name?, sensor_type?}]. Creates the device (or adds sensors to it) for every ticked point."""
    ids = [int(i["id"]) for i in items if str(i.get("id", "")).isdigit()]
    rows = {r.id: r for r in db.scalars(select(DetectedTag).where(DetectedTag.id.in_(ids), DetectedTag.plant_id == link.plant_id))}
    made_devices: dict[str, Device] = {}
    first_values: dict[str, list] = {}
    added, skipped = 0, []
    for it in items:
        row = rows.get(int(it["id"])) if str(it.get("id", "")).isdigit() else None
        if not row:
            skipped.append({"id": it.get("id"), "why": "not in the Detected list"})
            continue
        dev_name = (_clean_name(it.get("device_name"), MAX_DEVICE_LEN) if it.get("device_name") else None) or row.ext_device
        metric = metric_name(it.get("name")) or metric_name(row.tag) or f"tag{row.id}"
        key = dev_name
        device = made_devices.get(key)
        if device is None:
            device = db.scalar(select(Device).where(Device.company_name == link.company_name, Device.device_name == dev_name,
                                                    Device.ip_address == f"plant:{link.plant_id}"))
            if device is None:
                device = Device(device_name=dev_name, protocol="MQTT", ip_address=f"plant:{link.plant_id}", port=settings.MQTT_TLS_PORT, company_name=link.company_name,
                                state=link.site_state, region=link.site_region, plant_name=link.name[:120])
                db.add(device)
                db.flush()
            made_devices[key] = device
        if any(r.metric_name == metric for r in device.registers):
            skipped.append({"id": row.id, "why": f"'{metric}' already exists on {dev_name}"})
            continue
        db.add(DeviceRegister(device_id=device.id, metric_name=metric, unit=(str(it["unit"])[:16] if it.get("unit") else None),
                              sensor_type=(str(it["sensor_type"])[:32] if it.get("sensor_type") else None), data_type="bool" if row.kind == "bool" else "float32",
                              topic=reg_topic(link.plant_id, row.ext_device), json_field=row.tag, alarm_severity="major"))
        db.flush()
        db.expire(device, ["registers"])
        if row.sample is not None and utcnow() - row.last_seen < timedelta(hours=1):    # a gateway that reports only changes may stay quiet: show the last sample at once
            try:
                first_values.setdefault(dev_name, []).append((metric, float(row.sample), row.last_seen))
            except ValueError:
                pass
        db.delete(row)
        added += 1
    db.commit()
    for dev_name, vals in first_values.items():
        try:
            ingest_readings(db, None, made_devices[dev_name], vals)
        except Exception as exc:
            db.rollback()
            log.warning("first_value_failed", error=type(exc).__name__)
    return {"sensors_added": added, "devices": [d.device_name for d in made_devices.values()], "skipped": skipped}
