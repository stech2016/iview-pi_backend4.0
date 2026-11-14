"""Cloud <-> plant script control (one state per gateway: 'main' or one State + Region script).

  * notify_config(company)      tells the plant scripts "settings changed" (instant reload) over MQTT
  * install_config_hook()       calls notify_config automatically whenever a device / sensor is saved in the app
  * handle_status(...)          a plant script said online/offline (Last Will) or sent its heartbeat
  * watchdog()                  heartbeat missing for the company's "offline" time (Settings, default 10 s) -> that gateway is offline
  * gateway offline             one CRITICAL alarm for the people of that gateway's region, cleared when it is back
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta

import paho.mqtt.client as mqtt
import redis
from sqlalchemy import event, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.timeutil import utcnow
from app.db.session import MasterSession, tenant_session
from app.models.master import CompanyStorage, Gateway, User
from app.models.tenant import Alarm, Device, DeviceRegister, Notification
from app.services import gateway as gw
from app.services import comm, commands, csettings, live, push, status
from app.services.alarms import alarm_dict, place_text

log = get_logger("gwcontrol")

GATEWAY_METRIC = "gateway"
GATEWAY_DEVICE = "Plant Gateway"


# ------------------------------------------------------------------ instant settings push
_client: mqtt.Client | None = None
_client_lock = threading.Lock()


def _publisher() -> mqtt.Client:
    global _client
    with _client_lock:
        if _client is None:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"stech-api-{os.getpid()}")
            if settings.MQTT_USERNAME:
                c.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
            c.reconnect_delay_set(1, 10)
            c.connect_async(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=30)
            c.loop_start()
            _client = c
        return _client


def publish_message(topic: str, body: str) -> bool:
    """One MQTT message from the API (QoS 1, not retained). False when the broker connection is down (nothing was queued)."""
    try:
        client = _publisher()
        for _ in range(20):          # the first call of a process waits up to 2 s for the connection
            if client.is_connected():
                break
            time.sleep(0.1)
        if not client.is_connected():
            return False
        return client.publish(topic, body, qos=1).rc == mqtt.MQTT_ERR_SUCCESS
    except Exception as exc:
        log.warning("publish_failed", error=type(exc).__name__)
        return False


def notify_config(company: str) -> None:
    """One ping reaches every gateway of the company: 'all' for the new scripts, 'main' for the ones downloaded earlier.
    A script that gets the ping reloads only if ITS devices really changed."""
    try:
        body = json.dumps({"changed": utcnow().isoformat()})
        for topic in (f"stech/{company}/gw/all/config", f"stech/{company}/gw/main/config"):
            _publisher().publish(topic, body, qos=1)
    except Exception as exc:
        log.warning("config_push_failed", error=type(exc).__name__)


def install_config_hook() -> None:
    """Whenever a device or sensor is saved/changed/deleted in a company database, push the new settings to the plant script."""
    def before_flush(session: Session, ctx, instances):
        for obj in list(session.new) + list(session.dirty) + list(session.deleted):
            if isinstance(obj, (Device, DeviceRegister)) and getattr(obj, "protocol", "") != "Gateway":
                session.info["gw_dirty"] = True

    def after_commit(session: Session):
        if session.info.pop("gw_dirty", False) and session.info.get("company"):
            notify_config(session.info["company"])

    event.listen(Session, "before_flush", before_flush)
    event.listen(Session, "after_commit", after_commit)
    event.listen(Session, "after_soft_rollback", lambda s, prev: s.info.pop("gw_dirty", None))


# ------------------------------------------------------------------ live details from the script (version, devices answering)
_info_client: redis.Redis | None = None


def _info_redis() -> redis.Redis:
    global _info_client
    if _info_client is None:
        _info_client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    return _info_client


def _info_key(company: str, gid: str) -> str:
    return f"gwinfo:{company}:{gid}"


def store_info(company: str, gid: str, info: dict) -> None:
    try:
        _info_redis().setex(_info_key(company, gid), 60, json.dumps(info))
    except Exception as exc:
        log.warning("gateway_info_store_failed", error=type(exc).__name__)


def load_info(company: str, gid: str) -> dict:
    try:
        raw = _info_redis().get(_info_key(company, gid))
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _clean_info(body: dict) -> dict:
    """Only small, known fields are kept (the message is signed, but never trust its size)."""
    raw = body.get("info")
    if not isinstance(raw, dict):
        return {}
    out = {}
    if isinstance(raw.get("ver"), str):
        out["ver"] = raw["ver"][:16]
    for key in ("ok", "total", "backlog"):
        if isinstance(raw.get(key), int) and 0 <= raw[key] < 10**9:
            out[key] = raw[key]
    return out


# ------------------------------------------------------------------ status handling (runs in the mqtt_ingest service)
def _row(master: Session, company: str, gid: str) -> Gateway | None:
    row = master.scalar(select(Gateway).where(Gateway.company_name == company, Gateway.gateway_id == gid))
    if not row and gid == gw.MAIN:  # the original company-wide script: its row appears with its first message
        row = Gateway(company_name=company, gateway_id=gid, state="never")
        master.add(row)
        master.flush()
    return row


def is_online(row: Gateway | None) -> bool:
    return bool(row and row.state == "online" and row.last_seen
                and utcnow() - row.last_seen < timedelta(seconds=csettings.get(row.company_name, "gateway_offline_seconds")))


def handle_status(company: str, gid: str, kind: str, payload: bytes) -> None:
    """kind = 'status' (retained online/offline + Last Will) or 'hb' (heartbeat every 5 s). Unsigned/forged messages are ignored."""
    try:
        body = json.loads(payload.decode())
        state, ts, sig = str(body["state"]), str(body["ts"]), str(body["sig"])
    except (ValueError, KeyError, TypeError):
        return
    topic = f"stech/{company}/gw/{gid}/{kind}"
    if state not in ("online", "offline") or not gw.verify_text(company, gid, topic, ts, state, sig):
        log.warning("gateway_status_rejected", company=company, gateway=gid)
        return
    if state == "online":
        info = _clean_info(body)
        if info:
            store_info(company, gid, info)
        if kind == "hb":   # per-device health (script 3.1): signed, and ignored when old (a recorded message must not be replayed)
            health = None
            dev_text, dev_sig = body.get("dev"), body.get("dsig")
            if isinstance(dev_text, str) and isinstance(dev_sig, str) and gw.verify_dev(company, gid, topic, ts, dev_text, dev_sig):
                try:
                    fresh = abs(utcnow().timestamp() - datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()) <= 30
                except ValueError:
                    fresh = False
                health = status.parse_health(dev_text) if fresh else None
            status.store_heartbeat(company, gid, health)
            comm.update(company, health)    # a device silent for the offline time raises its alarm; answering again clears it
        mark_online(company, gid)
    elif kind == "status":
        status.clear_heartbeat(company, gid)
        mark_offline(company, gid, "stopped")


def mark_online(company: str, gid: str) -> None:
    with MasterSession() as master:
        row = _row(master, company, gid)
        if not row:
            return
        was = row.state
        row.last_seen = utcnow()
        if was != "online":
            row.state, row.changed_at = "online", utcnow()
        master.commit()
        site = (row.site_state, row.site_region)
    if was != "online":
        log.info("gateway_online", company=company, gateway=gid)
        _publish_state(company, gid, True, site)
        if was == "offline":
            _clear_alarm(company, gid)


def mark_offline(company: str, gid: str, why: str) -> None:
    with MasterSession() as master:
        row = _row(master, company, gid)
        if not row or row.state != "online":
            master.commit()
            return
        row.state, row.changed_at = "offline", utcnow()
        master.commit()
        site, name = (row.site_state, row.site_region), row.name
    log.warning("gateway_offline", company=company, gateway=gid, why=why)
    _publish_state(company, gid, False, site)
    _raise_alarm(company, gid, name, site, why)


def _publish_state(company: str, gid: str, online: bool, site: tuple) -> None:
    live.publish(company, {"type": "gateway", "online": online, "gateway_id": gid,
                           "state": site[0] or "ALL", "region": site[1] or "ALL"})


def watchdog() -> None:
    """Heartbeat missing -> offline. Also sends unacknowledged-alarm escalation once a minute."""
    from app.workers.poller import Poller  # reuse the escalation rule the admin already configures
    esc, last_esc = Poller(), 0.0
    while True:
        try:
            now = utcnow()
            with MasterSession() as master:
                online = list(master.scalars(select(Gateway).where(Gateway.state == "online")))
                stale = [(g.company_name, g.gateway_id) for g in online if g.last_seen is None or
                         now - g.last_seen >= timedelta(seconds=csettings.get(g.company_name, "gateway_offline_seconds"))]
            for company, gid in stale:
                mark_offline(company, gid, "no heartbeat")
            commands.expire_due()
            if time.monotonic() - last_esc >= 60:
                last_esc = time.monotonic()
                esc.escalate_unacknowledged()
        except Exception as exc:
            log.warning("watchdog_failed", error=type(exc).__name__)
        time.sleep(1)


# ------------------------------------------------------------------ the alarm for the people of the gateway's region
def _pseudo_name(gid: str) -> str:
    return GATEWAY_DEVICE if gid == gw.MAIN else f"{GATEWAY_DEVICE} - {gid}"


def _pseudo_device(db: Session, company: str, gid: str, site: tuple) -> Device:
    name = _pseudo_name(gid)
    dev = db.scalar(select(Device).where(Device.protocol == "Gateway", Device.device_name == name))
    if not dev:
        dev = Device(device_name=name, protocol="Gateway", ip_address="cloud", port=0,
                     company_name=company, state=site[0] or "ALL", region=site[1] or "ALL")
        db.add(dev)
        db.flush()
    return dev


def _raise_alarm(company: str, gid: str, name: str | None, site: tuple, why: str) -> None:
    try:
        with MasterSession() as master:
            if not master.scalar(select(CompanyStorage.id).where(CompanyStorage.company_name == company, CompanyStorage.db_name.is_not(None))):
                return
            who = select(User.id).where(User.company_name == company, User.status == "approved")
            if site[0]:  # a regional gateway: the Super admin and the people of that State + Region
                who = who.where(or_(User.role == "admin", (User.state == site[0]) & (User.region == site[1])))
            users = list(master.scalars(who))
            with tenant_session(master, company) as db:
                dev = _pseudo_device(db, company, gid, site)
                if db.scalar(select(Alarm.id).where(Alarm.device_id == dev.id, Alarm.cleared_at.is_(None))):
                    return
                label = f" '{name}'" if name and gid != gw.MAIN else ""
                msg = (f"The plant gateway script{label} has stopped. Live data from its plant is not reaching the cloud. "
                       "Check that the plant PC is on, has internet, and the script is running.")
                alarm = Alarm(device_id=dev.id, metric_name=GATEWAY_METRIC, severity="critical", message=msg, raised_at=utcnow())
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
        log.warning("gateway_alarm_failed", company=company, gateway=gid, error=type(exc).__name__)


def _clear_alarm(company: str, gid: str) -> None:
    try:
        with MasterSession() as master:
            if not master.scalar(select(CompanyStorage.id).where(CompanyStorage.company_name == company, CompanyStorage.db_name.is_not(None))):
                return
            with tenant_session(master, company) as db:
                dev = db.scalar(select(Device).where(Device.protocol == "Gateway", Device.device_name == _pseudo_name(gid)))
                if not dev:
                    return
                for a in db.scalars(select(Alarm).where(Alarm.device_id == dev.id, Alarm.cleared_at.is_(None))):
                    a.cleared_at = utcnow()
                    db.commit()
                    live.publish(company, {"type": "alarm", "event": "cleared", "alarm": alarm_dict(a, dev.device_name, place_text(dev.city, dev.plant_name)),
                                           "state": dev.state, "region": dev.region})
    except Exception as exc:
        log.warning("gateway_alarm_clear_failed", company=company, gateway=gid, error=type(exc).__name__)
