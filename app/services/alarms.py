"""Threshold evaluation -> ISA-18.2 style alarm lifecycle + notifications."""
from datetime import datetime, timedelta

from sqlalchemy import select, or_, and_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.timeutil import utcnow
from app.models.master import AlarmSettings, User
from app.models.tenant import Alarm, AlarmShelf, Device, Notification
from app.services import alertrules, live, notify, push, sync

log = get_logger("alarms")


def place_text(city: str | None, plant_name: str | None) -> str | None:
    """"Coimbatore - Plant 2 Boiler House" (either part may be missing)."""
    return " - ".join(p for p in (city, plant_name) if p) or None


def mute_info(db: Session, device: Device) -> tuple[str | None, dict[str, AlarmShelf]]:
    """('maintenance' | None, {metric -> its current shelf}) of one device. Muted alarms are kept in the list but nobody is told."""
    now = utcnow()
    maint = "maintenance" if device.maintenance_until and device.maintenance_until > now else None
    shelves = {sh.metric_name: sh for sh in db.scalars(select(AlarmShelf).where(AlarmShelf.device_id == device.id, AlarmShelf.until > now))}
    return maint, shelves


def muted(db: Session, device: Device, metric: str) -> bool:
    maint, shelves = mute_info(db, device)
    return bool(maint or metric in shelves)


def alarm_dict(alarm: Alarm, device_name: str | None = None, place: str | None = None, **extra) -> dict:
    """`extra` carries what the list adds: muted ('shelved' / 'maintenance'), shelved_until / shelved_by / shelve_reason, comment_count."""
    return {**{
        "id": alarm.id, "device_id": alarm.device_id, "device_name": device_name, "place": place,
        "metric_name": alarm.metric_name, "severity": alarm.severity, "message": alarm.message,
        "raised_at": alarm.raised_at.isoformat(), "cleared_at": alarm.cleared_at.isoformat() if alarm.cleared_at else None,
        "acknowledged_by": alarm.acknowledged_by,
        "acknowledged_at": alarm.acknowledged_at.isoformat() if alarm.acknowledged_at else None,
    }, **extra}


DEFAULT_ESCALATION = {"critical": 2, "major": 5, "minor": 0, "advisory": 0}   # only critical and major alarms are escalated


def escalation_minutes(master: Session, company: str) -> dict[str, int]:
    """Minutes an unacknowledged alarm of each priority may wait before it is escalated (0 = never). Editable by the admin."""
    row = master.scalar(select(AlarmSettings).where(AlarmSettings.company_name == company))
    if not row:
        return dict(DEFAULT_ESCALATION)
    return {"critical": row.critical, "major": row.major, "minor": 0, "advisory": 0}


def recipient_ids(master: Session, device: Device) -> list[int]:
    stmt = select(User.id).where(
        User.company_name == device.company_name, User.status == "approved",
        or_(User.role == "admin", and_(User.role.in_(("operator", "regional_admin")), User.state == device.state, User.region == device.region)),
    )
    return list(master.scalars(stmt))


def record_late(db: Session, device: Device, rows: list[dict], now: datetime) -> int:
    """Readings that arrive LATE (older than LATE_READING_SECONDS: the gateway was cut off and is catching up).
    A breach inside them is over (or unknown) by now, so nobody is paged and no live alarm is opened or closed by them.
    Each breach period is written once into the alarm list as an already-closed alarm, "received late", so nothing is lost.
    Whether the sensor is still outside its limit is decided by the newest fresh reading through the normal path.
    Returns how many entries were written."""
    cutoff = now - timedelta(seconds=settings.LATE_READING_SECONDS)
    regs = {r.metric_name: r for r in device.registers}
    by_metric: dict[str, list[tuple[datetime, float]]] = {}
    for r in rows:
        if r["timestamp"] < cutoff and r["metric_name"] in regs:
            by_metric.setdefault(r["metric_name"], []).append((r["timestamp"], r["value"]))
    made: list[Alarm] = []
    for metric, pts in by_metric.items():
        reg = regs[metric]
        if reg.low_limit is None and reg.high_limit is None:
            continue
        pts.sort()
        unit = f" {reg.unit}" if reg.unit else ""
        start = last_bad = text = None
        worst = None
        def close(end: datetime) -> None:
            if start is None:
                return
            exists = db.scalar(select(Alarm.id).where(Alarm.device_id == device.id, Alarm.metric_name == metric, Alarm.raised_at == start))
            if exists:
                return                                   # a resend of the same batch: written already
            a = Alarm(device_id=device.id, metric_name=metric, severity=reg.alarm_severity, raised_at=start, cleared_at=end,
                      message=f"{text} - received late after a connection loss ({start:%Y-%m-%d %H:%M}-{end:%H:%M} UTC)")
            db.add(a)
            made.append(a)
        for ts, value in pts:
            bad = None
            if reg.high_limit is not None and value > reg.high_limit:
                bad = f"{metric} high: {value:g}{unit} exceeds limit {reg.high_limit:g}{unit}"
            elif reg.low_limit is not None and value < reg.low_limit:
                bad = f"{metric} low: {value:g}{unit} below limit {reg.low_limit:g}{unit}"
            if bad and start is None:
                start, text = ts, bad
            elif bad:
                last_bad = ts
            elif start is not None:
                close(ts)
                start = None
            if bad:
                last_bad = ts
        if start is not None:
            close(last_bad)                              # still outside the limit at the last late reading: closed there, "at least until"
    if not made:
        return 0
    db.commit()
    sync.mirror(db, "alarms", made)
    for a in made:
        log.info("alarm_recorded_late", device_id=device.id, alarm_id=a.id)
    return len(made)


def evaluate(db: Session, master: Session | None, device: Device, latest: dict[str, tuple[float, datetime]], pre: dict | None = None) -> None:
    """`latest` maps metric -> (value, timestamp) for the newest reading of each metric.
    `pre` (a batch of many devices) brings this device's open alarms and alert rules already read in ONE query for the whole batch."""
    regs = {r.metric_name: r for r in device.registers}
    raised, changed, updated, notifs = [], [], [], []
    recipients: list[int] | None = None
    mute: list = []                            # v1.1.5: in maintenance or shelved -> the alarm is recorded, nobody is told

    def quiet(metric: str) -> bool:
        if not mute:                           # looked up only when an alarm is actually raised (most readings raise nothing)
            mute.extend(mute_info(db, device))
        return bool(mute[0] or metric in mute[1])

    # ONE query for every open alarm of this device (was one query per sensor with a limit, on every reading)
    open_alarms = pre["open"] if pre is not None else {a.metric_name: a for a in db.scalars(select(Alarm).where(Alarm.device_id == device.id, Alarm.cleared_at.is_(None)))}

    for metric, (value, ts) in latest.items():
        reg = regs.get(metric)
        if not reg or (reg.low_limit is None and reg.high_limit is None):
            continue
        unit = f" {reg.unit}" if reg.unit else ""
        breach = None
        if reg.high_limit is not None and value > reg.high_limit:
            breach = f"{metric} high: {value:g}{unit} exceeds limit {reg.high_limit:g}{unit}"
        elif reg.low_limit is not None and value < reg.low_limit:
            breach = f"{metric} low: {value:g}{unit} below limit {reg.low_limit:g}{unit}"
        active = open_alarms.get(metric)

        if breach and not active:
            alarm = Alarm(device_id=device.id, metric_name=metric, severity=reg.alarm_severity,
                          message=breach, raised_at=ts)
            db.add(alarm)
            db.flush()
            if recipients is None:
                recipients = recipient_ids(master, device) if master is not None else []
            if not quiet(metric):
                for uid in recipients:
                    n = Notification(user_id=uid, alarm_id=alarm.id, channel="in_app", sent_at=utcnow())
                    db.add(n)
                    notifs.append(n)
            # FUTURE OUTBOUND CONNECTOR (alarm raised): add ONE call here, e.g. outbound.send("alarm.raised", payload), so an ERP / MES / webhook receives this.
            # Nothing is sent today. Keep it behind a company setting and a timeout so a slow receiver can never block the plant data.
            raised.append(alarm)
        elif not breach and active:
            active.cleared_at = ts
            changed.append(active)
        elif breach and active and active.severity != reg.alarm_severity:
            active.severity = reg.alarm_severity  # the user changed the sensor's alarm priority
            updated.append(active)

    rule_raised, rule_cleared = alertrules.check(db, device, latest, open_alarms, pre["rules"] if pre is not None else None)   # v1.1: the user's own alert rules
    if rule_raised and recipients is None:
        recipients = recipient_ids(master, device) if master is not None else []
    for alarm in rule_raised:
        if quiet(alarm.metric_name or ""):
            continue
        for uid in recipients or []:
            n = Notification(user_id=uid, alarm_id=alarm.id, channel="in_app", sent_at=utcnow())
            db.add(n)
            notifs.append(n)
    raised, changed = raised + rule_raised, changed + rule_cleared

    if not (raised or changed or updated):
        return
    db.commit()
    sync.mirror(db, "alarms", raised + changed + updated)
    sync.mirror(db, "notifications", notifs)
    for a in raised:
        log.info("alarm_raised", device_id=device.id, severity=a.severity, alarm_id=a.id)
        text = (f"{place_text(device.city, device.plant_name)}: " if place_text(device.city, device.plant_name) else "") + a.message
        if quiet(a.metric_name or ""):
            live.publish(device.company_name, {"type": "alarm", "event": "raised", "alarm": alarm_dict(
                a, device.device_name, place_text(device.city, device.plant_name), muted="maintenance" if mute[0] else "shelved"),
                "state": device.state, "region": device.region})
            continue
        if master is not None:
            notify.tell(master, device.company_name, recipients or [], push.alarm_title(a.severity, device.device_name), text,
                        {"alarm_id": a.id, "severity": a.severity, "device_id": device.id}, a.severity)
        else:
            push.notify(device.company_name, recipients or [], push.alarm_title(a.severity, device.device_name), text,
                        {"alarm_id": a.id, "severity": a.severity, "device_id": device.id})
        live.publish(device.company_name, {"type": "alarm", "event": "raised", "alarm": alarm_dict(a, device.device_name, place_text(device.city, device.plant_name)),
                                           "state": device.state, "region": device.region})
    for a in updated:
        live.publish(device.company_name, {"type": "alarm", "event": "updated", "alarm": alarm_dict(a, device.device_name, place_text(device.city, device.plant_name)),
                                           "state": device.state, "region": device.region})
    for a in changed:
        log.info("alarm_cleared", device_id=device.id, alarm_id=a.id)
        live.publish(device.company_name, {"type": "alarm", "event": "cleared", "alarm": alarm_dict(a, device.device_name, place_text(device.city, device.plant_name)),
                                           "state": device.state, "region": device.region})
