"""A device that stops answering its plant PC.

The plant PC tells the cloud every second which devices answered their last read. The first time a device did NOT answer is
remembered here (Valkey). While it stays silent:
  - shorter than 'stale' seconds   the app keeps the last value, nothing changes
  - from 'stale' seconds           the value turns grey ("stale 7 s") and the write buttons lock
  - from 'offline' seconds         the device is Offline and ONE alarm is raised for the people of its State + Region
Both times are company settings (Settings screen). When the device answers again the alarm clears by itself.
"""
import time

import redis
from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger
from app.core.timeutil import utcnow
from app.db.session import MasterSession, tenant_session
from app.models.tenant import Alarm, Device, Notification
from app.services import alarms as alarm_svc
from app.services import csettings, live, notify, push

log = get_logger("comm")
METRIC = "comm"
_client: redis.Redis | None = None


def _r() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    return _client


def _fail_key(company: str) -> str:
    return f"commfail:{company}"


def _alarm_key(company: str) -> str:
    return f"commalarm:{company}"


def failing_since(company: str) -> dict[int, float]:
    """device id -> the time (epoch seconds) it first stopped answering."""
    try:
        return {int(k): float(v) for k, v in _r().hgetall(_fail_key(company)).items()}
    except Exception as exc:
        log.warning("commfail_read_failed", error=type(exc).__name__)
        return {}


def update(company: str, health: dict[str, bool] | None) -> None:
    """Called for every heartbeat that carries the per-device list. Cheap: Valkey only, the database only when an alarm changes."""
    if not health:
        return
    now = time.time()
    offline_s = csettings.get(company, "gateway_offline_seconds")
    try:
        pipe = _r().pipeline()
        for dev, ok in health.items():
            if ok:
                pipe.hdel(_fail_key(company), dev)
            else:
                pipe.hsetnx(_fail_key(company), dev, now)
        pipe.execute()
        since = failing_since(company)
        alarmed = {int(x) for x in _r().smembers(_alarm_key(company))}
    except Exception as exc:
        log.warning("comm_update_failed", error=type(exc).__name__)
        return
    for dev, ok in health.items():
        if not dev.isdigit():
            continue
        did = int(dev)
        if ok and did in alarmed:
            _clear(company, did)
        elif not ok and did not in alarmed and now - since.get(did, now) >= offline_s:
            _raise(company, did)


def _recipients(master, device: Device) -> list[int]:
    return alarm_svc.recipient_ids(master, device)


def _raise(company: str, device_id: int) -> None:
    try:
        with MasterSession() as master, tenant_session(master, company) as db:
            dev = db.get(Device, device_id)
            if not dev:
                return
            _r().sadd(_alarm_key(company), device_id)
            if alarm_svc.muted(db, dev, METRIC):
                return    # in maintenance (or shelved): remembered, but nobody is told
            if db.scalar(select(Alarm.id).where(Alarm.device_id == dev.id, Alarm.metric_name == METRIC, Alarm.cleared_at.is_(None))):
                return
            msg = f"'{dev.device_name}' is not answering the plant PC. Check its power, cable and network."
            alarm = Alarm(device_id=dev.id, metric_name=METRIC, severity="major", message=msg, raised_at=utcnow())
            db.add(alarm)
            db.flush()
            users = _recipients(master, dev)
            for uid in users:
                db.add(Notification(user_id=uid, alarm_id=alarm.id, channel="in_app", sent_at=utcnow()))
            db.commit()
            live.publish(company, {"type": "alarm", "event": "raised", "alarm": alarm_svc.alarm_dict(alarm, dev.device_name, alarm_svc.place_text(dev.city, dev.plant_name)),
                                   "state": dev.state, "region": dev.region})
            notify.tell(master, company, users, push.alarm_title("major", dev.device_name), msg,
                        {"alarm_id": alarm.id, "severity": "major", "device_id": dev.id}, "major")
    except Exception as exc:
        log.warning("comm_alarm_failed", company=company, error=type(exc).__name__)


def _clear(company: str, device_id: int) -> None:
    try:
        _r().srem(_alarm_key(company), device_id)
        with MasterSession() as master, tenant_session(master, company) as db:
            dev = db.get(Device, device_id)
            if not dev:
                return
            for a in db.scalars(select(Alarm).where(Alarm.device_id == dev.id, Alarm.metric_name == METRIC, Alarm.cleared_at.is_(None))):
                a.cleared_at = utcnow()
                db.commit()
                live.publish(company, {"type": "alarm", "event": "cleared", "alarm": alarm_svc.alarm_dict(a, dev.device_name, alarm_svc.place_text(dev.city, dev.plant_name)),
                                       "state": dev.state, "region": dev.region})
    except Exception as exc:
        log.warning("comm_clear_failed", company=company, error=type(exc).__name__)
