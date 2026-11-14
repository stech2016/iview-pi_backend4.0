"""Real-time online / offline state of every device (one rule for the dashboard, the device list and the detail page).

The plant script sends a signed heartbeat every second that lists, for each device it reads, whether that device
answered its last read. The ingest worker keeps the newest list in Valkey (shared by every backend process).

  online         the plant PC is reporting AND the device answered
  stale          the data is late (no heartbeat, or the device silent, for 'stale' seconds): the app shows the last value grey with its age
  offline        the device does NOT answer for 'offline' seconds (the connection to the PLC is really down)
  plant_offline  the plant PC itself has not reported for 'offline' seconds (switched off, no internet, script stopped) - the device may be fine
  waiting        no plant PC has ever connected for this device yet
The two times are company settings (device_stale_seconds, gateway_offline_seconds).

Devices that are not read by a plant PC (MQTT) are online while they keep publishing (they publish at their own pace).
Older plant scripts (3.0) send no per-device list: their devices count as online while data keeps arriving (<= 15 s).
"""
import json
import time

import redis

from app.core.config import settings
from app.core.logging import get_logger
from app.core.timeutil import utcnow
from app.services import comm, csettings
from app.services import gateway as gw

log = get_logger("status")
OLD_SCRIPT_DATA_SECONDS = 15      # script 3.0 repeats an unchanged value every 10 s
OTHER_DATA_SECONDS = 300          # MQTT devices: unknown publishing rate, as before
KEEP_SECONDS = 20
_client: redis.Redis | None = None


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    return _client


def _key(company: str, gid: str) -> str:
    return f"gwhb:{company}:{gid}"


def parse_health(text: str) -> dict[str, bool] | None:
    """'12:1,13:0' -> {'12': True, '13': False}. Anything malformed -> None (the list is ignored)."""
    out: dict[str, bool] = {}
    try:
        if len(text) > 20000:
            return None
        for part in filter(None, text.split(",")):
            dev, flag = part.split(":")
            if not dev.isdigit() or flag not in ("0", "1"):
                return None
            out[dev] = flag == "1"
    except ValueError:
        return None
    return out


def store_heartbeat(company: str, gid: str, dev: dict[str, bool] | None) -> None:
    try:
        _redis().setex(_key(company, gid), KEEP_SECONDS, json.dumps({"t": time.time(), "dev": dev}))
    except Exception as exc:
        log.warning("heartbeat_store_failed", error=type(exc).__name__)


def clear_heartbeat(company: str, gid: str) -> None:
    try:
        _redis().delete(_key(company, gid))
    except Exception as exc:
        log.warning("heartbeat_clear_failed", error=type(exc).__name__)


def _load(company: str, gids: set[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not gids:
        return out
    ordered = sorted(gids)
    try:
        for gid, raw in zip(ordered, _redis().mget([_key(company, g) for g in ordered])):
            if raw:
                out[gid] = json.loads(raw)
    except Exception as exc:
        log.warning("heartbeat_read_failed", error=type(exc).__name__)
    return out


def extgw_states(company: str, devices: list) -> dict[int, str]:
    """device id -> online | plant_offline | waiting, for the devices that come from a customer gateway (sensor topic 'ext:<plant>:<device>')."""
    from app.services import extgw
    plant_of: dict[int, str] = {}
    for d in devices:
        if d.protocol != "MQTT":
            continue
        for r in d.registers:
            where = extgw.split_reg_topic(r.topic)
            if where:
                plant_of[d.id] = where[0]
                break
    if not plant_of:
        return {}
    from sqlalchemy import select
    from app.db.session import MasterSession
    from app.models.master import PlantLink
    with MasterSession() as master:
        rows = {r.plant_id: r for r in master.scalars(select(PlantLink).where(PlantLink.plant_id.in_(set(plant_of.values())), PlantLink.company_name == company))}
    out = {}
    for did, plant in plant_of.items():
        row = rows.get(plant)
        out[did] = "online" if extgw.is_online(row) else ("plant_offline" if row is not None and row.last_seen else "waiting")
    return out


def device_states(company: str, devices: list, last_seen: dict, infos: dict, gw_rows: dict) -> dict[int, str]:
    """device id -> online | offline | plant_offline | waiting."""
    now, now_dt = time.time(), utcnow()
    stale_s = csettings.get(company, "device_stale_seconds")
    offline_s = max(csettings.get(company, "gateway_offline_seconds"), stale_s)
    failing = comm.failing_since(company)
    plant_devices = [d for d in devices if d.protocol in gw.GATEWAY_PROTOCOLS]
    gid_of = {d.id: gw.responsible_gid(infos, d.state, d.region, d.plant_pc) for d in plant_devices}
    beats = _load(company, set(gid_of.values()))
    out: dict[int, str] = {}
    ext_states = extgw_states(company, devices)
    for d in devices:
        seen = last_seen.get(d.id)
        age = (now_dt - seen).total_seconds() if seen else None
        if d.id in ext_states:     # V2.0: read by a customer gateway: it reports only what changes, so "online" = its gateway is connected
            out[d.id] = ext_states[d.id] if ext_states[d.id] != "online" or age is not None else "waiting"
            continue
        if d.id not in gid_of:
            out[d.id] = "online" if age is not None and age <= OTHER_DATA_SECONDS else "offline"
            continue
        gid = gid_of[d.id]
        rec = beats.get(gid)
        if rec is None:   # no heartbeat for a while (or never)
            row = gw_rows.get(gid)
            out[d.id] = "plant_offline" if row is not None and row.last_seen else "waiting"
            continue
        beat_age = now - float(rec.get("t", 0))
        health = rec.get("dev")
        if health is not None:   # plant script 3.1
            if beat_age > offline_s:
                out[d.id] = "plant_offline"
            elif beat_age > stale_s:
                out[d.id] = "stale"
            else:
                flag = health.get(str(d.id))
                if flag is None:   # not read yet since the last reload
                    out[d.id] = "online" if age is not None and age <= OLD_SCRIPT_DATA_SECONDS else "offline"
                elif flag:
                    out[d.id] = "online"
                else:              # the device is silent: grey first, Offline after the offline time
                    out[d.id] = "offline" if now - failing.get(d.id, now) >= offline_s else "stale"
        else:                    # plant script 3.0
            if beat_age > offline_s:
                out[d.id] = "plant_offline"
            else:
                out[d.id] = "online" if age is not None and age <= OLD_SCRIPT_DATA_SECONDS else "offline"
    return out
