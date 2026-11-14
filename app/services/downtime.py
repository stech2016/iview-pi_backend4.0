"""Downtime log. A stop is opened automatically (the machine's run bit is 0, or the device stopped answering) and closed when it runs
again; the operator then picks a reason. Called once a minute from jobs.py (plantwatch).

The run bit is the device's first sensor with sensor_type 'status' (pump_run, compressor_run, burner_on ...).
Reasons are a fixed list here (REASONS) - change the list in this one place."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import as_utc, utcnow
from app.models.tenant import Device, DeviceRegister, DowntimeEvent, Telemetry
from app.services import lastseen

REASONS = ["Planned maintenance", "Breakdown", "No raw material", "Power failure", "Changeover", "Cleaning", "Operator break", "Quality check", "Other"]
ALIVE_SECONDS = 120


def run_metric(d: Device) -> str | None:
    regs = [r for r in d.registers if r.sensor_type == "status"]
    regs.sort(key=lambda r: 0 if any(w in r.metric_name.lower() for w in ("run", "on")) else 1)
    return regs[0].metric_name if regs else None


def _open(db: Session, d: Device) -> DowntimeEvent | None:
    return db.scalar(select(DowntimeEvent).where(DowntimeEvent.device_id == d.id, DowntimeEvent.ended_at.is_(None)).order_by(DowntimeEvent.id.desc()))


def check_company(company: str, db: Session) -> None:
    now, seen = utcnow(), lastseen.get_all(company)
    for d in db.scalars(select(Device).where(Device.protocol != "Gateway")):
        last = seen.get(d.id)
        if last is None:
            continue
        online = (now - last).total_seconds() <= ALIVE_SECONDS
        metric = run_metric(d)
        running = None
        if online and metric:
            v = db.scalar(select(Telemetry.value).where(Telemetry.device_id == d.id, Telemetry.metric_name == metric).order_by(Telemetry.timestamp.desc()).limit(1))
            running = None if v is None else v >= 0.5
        want = "offline" if not online else ("runbit" if running is False else None)
        ev = _open(db, d)
        if want and not ev:
            db.add(DowntimeEvent(device_id=d.id, started_at=now if want == "offline" else now, source=want))
        elif ev and (want is None or ev.source != want):
            ev.ended_at = now
            if want:
                db.add(DowntimeEvent(device_id=d.id, started_at=now, source=want))
    # FUTURE OUTBOUND CONNECTOR (a stop started or ended - for maintenance systems (CMMS)): add ONE call here, e.g. outbound.send("downtime.changed", payload), so an ERP / MES / webhook receives this.
    # Nothing is sent today. Keep it behind a company setting and a timeout so a slow receiver can never block the plant data.
    db.commit()


def minutes(ev: DowntimeEvent, until: datetime) -> float:
    return max(0.0, ((as_utc(ev.ended_at) if ev.ended_at else until) - as_utc(ev.started_at)).total_seconds() / 60)


def summary(db: Session, devices: list[Device], days: int) -> dict:
    """Availability % and the Pareto (reasons, biggest first) for the devices over the last N days."""
    now = utcnow()
    since = now - timedelta(days=days)
    ids = [d.id for d in devices]
    evs = list(db.scalars(select(DowntimeEvent).where(DowntimeEvent.device_id.in_(ids), DowntimeEvent.started_at >= since))) if ids else []
    total = sum(minutes(e, now) for e in evs)
    span = days * 1440 * max(len(ids), 1)
    pareto: dict[str, float] = {}
    for e in evs:
        pareto[e.reason or "Not entered yet"] = pareto.get(e.reason or "Not entered yet", 0) + minutes(e, now)
    return {"days": days, "events": len(evs), "downtime_minutes": round(total, 1),
            "availability_percent": round(max(0.0, 100 - total / span * 100), 2),
            "pareto": [{"reason": k, "minutes": round(v, 1)} for k, v in sorted(pareto.items(), key=lambda kv: -kv[1])]}
