"""Batch tracking: start / end a batch on a device; on end the min / avg / max of every sensor, the alarms and the writes made
during the batch are stored as one JSON summary. Only readings that changed are stored, so min / avg / max are over stored points."""
import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timeutil import as_utc, utcnow
from app.models.tenant import Alarm, Batch, Command, DeviceRegister, Telemetry


def summarise(db: Session, b: Batch, until: datetime) -> dict:
    start = as_utc(b.started_at)
    regs = {r.metric_name: r for r in db.scalars(select(DeviceRegister).where(DeviceRegister.device_id == b.device_id))}
    rows = db.execute(select(Telemetry.metric_name, func.min(Telemetry.value), func.avg(Telemetry.value), func.max(Telemetry.value), func.count())
                      .where(Telemetry.device_id == b.device_id, Telemetry.timestamp >= start, Telemetry.timestamp <= until)
                      .group_by(Telemetry.metric_name)).all()
    sensors = [{"metric": m, "unit": (regs[m].unit if m in regs else None) or "", "min": round(lo, 3), "avg": round(av, 3), "max": round(hi, 3), "points": n}
               for m, lo, av, hi, n in sorted(rows)]
    alarms = [{"metric": a.metric_name, "severity": a.severity, "message": a.message, "raised_at": a.raised_at.isoformat()}
              for a in db.scalars(select(Alarm).where(Alarm.device_id == b.device_id, Alarm.raised_at >= start, Alarm.raised_at <= until).order_by(Alarm.raised_at))]
    writes = [{"metric": c.metric_name, "value": c.value, "state": c.state, "by": c.requested_by, "reason": c.reason, "at": c.created_at.isoformat()}
              for c in db.scalars(select(Command).where(Command.device_id == b.device_id, Command.created_at >= start, Command.created_at <= until).order_by(Command.id))]
    return {"sensors": sensors, "alarms": alarms, "writes": writes}


def finish(db: Session, b: Batch, user: str) -> None:
    b.ended_at, b.ended_by = utcnow(), user
    # FUTURE OUTBOUND CONNECTOR (batch ended - ideal for MES / ERP production reports): add ONE call here, e.g. outbound.send("batch.ended", payload), so an ERP / MES / webhook receives this.
    # Nothing is sent today. Keep it behind a company setting and a timeout so a slow receiver can never block the plant data.
    b.summary = json.dumps(summarise(db, b, b.ended_at))
    db.commit()


def to_dict(db: Session, b: Batch, name: str, full: bool = False) -> dict:
    out = {"id": b.id, "device_id": b.device_id, "device_name": name, "batch_no": b.batch_no, "product": b.product,
           "started_at": b.started_at.isoformat(), "ended_at": b.ended_at.isoformat() if b.ended_at else None,
           "started_by": b.started_by, "ended_by": b.ended_by, "running": b.ended_at is None}
    if full:
        out["summary"] = json.loads(b.summary) if b.summary else summarise(db, b, utcnow())
    return out
