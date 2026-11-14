"""Stuck sensor warning. A sensor that used to move but has shown exactly the same value for N minutes while its device keeps
answering gets ONE advisory alarm ("Flow 2 unchanged for 47 min - check the sensor or its cable"). It clears when the value changes.

Only readings that CHANGED are stored, so the last stored time of a sensor is the last time its value changed.
N is the company setting 'stuck_sensor_minutes' (0 = off) or the sensor's own 'stuck_minutes'. On/off sensors (coils, bits) are never checked."""
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.timeutil import utcnow
from app.models.tenant import Alarm, Device, Notification
from app.services import alarms as alarm_svc
from app.services import csettings, lastseen, live

log = get_logger("stuck")
SUFFIX = " [stuck]"
ALIVE_SECONDS = 120          # the device must have answered within this time, otherwise it is simply offline
HISTORY_CHANGES = 5          # it must have changed at least this often in the last 24 hours (so a sensor that is always flat is not nagged)


def key(metric: str) -> str:
    return (metric[:64 - len(SUFFIX)] + SUFFIX)


def check_company(master, company: str, db: Session) -> None:
    default_min = csettings.get(company, "stuck_sensor_minutes")
    now = utcnow()
    seen = lastseen.get_all(company)
    devices = list(db.scalars(select(Device).where(Device.protocol != "Gateway")))
    for d in devices:
        last = seen.get(d.id)
        if not last or (now - last).total_seconds() > ALIVE_SECONDS:
            continue
        regs = [r for r in d.registers if r.data_type != "bool" and r.register_type not in ("coil", "discrete")]
        want = {r.metric_name: (r.stuck_minutes if r.stuck_minutes is not None else default_min) for r in regs}
        if not any(want.values()):
            continue
        rows = db.execute(text("""SELECT metric_name, max("timestamp"), count(*) FILTER (WHERE "timestamp" > :day)
                                  FROM telemetry WHERE device_id = :d AND "timestamp" > :week GROUP BY metric_name"""),
                          {"d": d.id, "day": now - timedelta(hours=24), "week": now - timedelta(days=7)}).all()
        info = {m: (ts, n) for m, ts, n in rows}
        maint, shelves = alarm_svc.mute_info(db, d)
        recipients = None
        for r in regs:
            minutes = want[r.metric_name]
            active = db.scalar(select(Alarm).where(Alarm.device_id == d.id, Alarm.metric_name == key(r.metric_name), Alarm.cleared_at.is_(None)))
            ts, n = info.get(r.metric_name, (None, 0))
            if not minutes or ts is None:
                if active and not minutes:
                    _clear(db, d, active, now, company)
                continue
            quiet_for = (now - ts).total_seconds() / 60
            if active:
                if ts > active.raised_at:     # the value changed after the alarm was raised: it is moving again
                    _clear(db, d, active, ts, company)
                continue
            if quiet_for >= minutes and n >= HISTORY_CHANGES:
                msg = f"{r.metric_name} has shown the same value for {int(quiet_for)} min while the device answers. Check the sensor and its cable."
                a = Alarm(device_id=d.id, metric_name=key(r.metric_name), severity="advisory", message=msg, raised_at=now)
                db.add(a)
                db.flush()
                muted = bool(maint or key(r.metric_name) in shelves)
                if not muted:
                    if recipients is None:
                        recipients = alarm_svc.recipient_ids(master, d)
                    for uid in recipients:
                        db.add(Notification(user_id=uid, alarm_id=a.id, channel="in_app", sent_at=utcnow()))
                db.commit()
                live.publish(company, {"type": "alarm", "event": "raised", "alarm": alarm_svc.alarm_dict(
                    a, d.device_name, alarm_svc.place_text(d.city, d.plant_name), **({"muted": "maintenance" if maint else "shelved"} if muted else {})),
                    "state": d.state, "region": d.region})
                log.info("stuck_sensor", device_id=d.id)


def _clear(db: Session, d: Device, a: Alarm, when, company: str) -> None:
    a.cleared_at = when
    db.commit()
    live.publish(company, {"type": "alarm", "event": "cleared", "alarm": alarm_svc.alarm_dict(a, d.device_name, alarm_svc.place_text(d.city, d.plant_name)),
                           "state": d.state, "region": d.region})
