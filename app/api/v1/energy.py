"""Energy meter page, for ANY meter (not one brand): sensors are found by their role (sensor_type: voltage, current, power, pf,
frequency, energy), so whatever sensors the device has are shown and the rest are simply left out.
Documents (PDF / Excel / CSV): daily energy, monthly consumption, raw readings, alarm history."""
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.timeutil import utcnow
from app.deps import client_ip, get_company_db, get_scoped_device
from app.models.master import User
from app.models.tenant import Alarm, Device, DeviceRegister, Telemetry
from app.services import audit, perms, tablefile
from app.services.export import _offset_minutes, iter_file, local_text

router = APIRouter(prefix="/energy", tags=["energy"])
ROLES = ("voltage", "current", "power", "pf", "frequency", "energy")
MAX_RAW_ROWS = 50_000


def _roles(db: Session, d: Device) -> dict[str, list[DeviceRegister]]:
    out: dict[str, list[DeviceRegister]] = {}
    for r in db.scalars(select(DeviceRegister).where(DeviceRegister.device_id == d.id).order_by(DeviceRegister.id)):
        if r.sensor_type in ROLES:
            out.setdefault(r.sensor_type, []).append(r)
    return out


@router.get("/{device_id}/live")
def live(device_id: int, user: User = Depends(perms.require("monitor", 1)), db: Session = Depends(get_company_db)):
    d = get_scoped_device(db, user, device_id)
    roles, tiles = _roles(db, d), []
    for role, regs in roles.items():
        for r in regs:
            row = db.execute(select(Telemetry.value, Telemetry.timestamp).where(Telemetry.device_id == d.id, Telemetry.metric_name == r.metric_name)
                             .order_by(Telemetry.timestamp.desc()).limit(1)).first()
            tiles.append({"role": role, "metric": r.metric_name, "unit": r.unit or "", "value": row[0] if row else None,
                          "at": row[1].isoformat() if row else None, "low": r.low_limit, "high": r.high_limit})
    return {"device_id": d.id, "device_name": d.device_name, "roles_present": list(roles), "tiles": tiles}


def _energy_per(db: Session, d: Device, reg: DeviceRegister, unit: str, days: int) -> list[list]:
    """Cumulative energy counter -> consumption per day / month = last reading minus first reading of that period."""
    since = utcnow() - timedelta(days=days)
    # days / months start at the VIEWER's midnight (not UTC's): shift by the viewer's offset, cut, label with the shifted date
    p = func.date_trunc(unit, func.timezone("UTC", Telemetry.timestamp) + timedelta(minutes=_offset_minutes()))
    rows = db.execute(select(p, func.min(Telemetry.value), func.max(Telemetry.value))
                      .where(Telemetry.device_id == d.id, Telemetry.metric_name == reg.metric_name, Telemetry.timestamp >= since)
                      .group_by(p).order_by(p)).all()
    fmt = "%Y-%m-%d" if unit == "day" else "%Y-%m"
    return [[t.strftime(fmt), round(lo, 3), round(hi, 3), round(hi - lo, 3), reg.unit or ""] for t, lo, hi in rows]


@router.get("/{device_id}/report")
def report(device_id: int, request: Request, kind: str = Query(pattern="^(daily|monthly|raw|alarms)$"), format: str = Query("pdf", pattern="^(csv|xlsx|pdf)$"),
           days: int = Query(30, ge=1, le=366), user: User = Depends(perms.require("reports", 1)), db: Session = Depends(get_company_db)):
    d = get_scoped_device(db, user, device_id)
    roles = _roles(db, d)
    since = utcnow() - timedelta(days=days)
    if kind in ("daily", "monthly"):
        regs = roles.get("energy")
        if not regs:
            raise AppError(409, "NO_ENERGY_SENSOR", "This device has no energy (kWh) sensor, so this document cannot be made.")
        headers = ["Period", "Start reading", "End reading", "Consumed", "Unit"]
        rows = _energy_per(db, d, regs[0], "day" if kind == "daily" else "month", days if kind == "daily" else max(days, 366))
        title = "Daily energy" if kind == "daily" else "Monthly consumption"
    elif kind == "raw":
        metrics = [r.metric_name for rs in roles.values() for r in rs]
        if not metrics:
            raise AppError(409, "NO_METER_SENSORS", "This device has no meter sensors (voltage, current, power ...).")
        headers, title = ["Time", "Sensor", "Value"], "Raw readings"
        rows = [[local_text(t, "%Y-%m-%d %H:%M:%S"), m, v] for t, m, v in db.execute(
            select(Telemetry.timestamp, Telemetry.metric_name, Telemetry.value).where(Telemetry.device_id == d.id, Telemetry.metric_name.in_(metrics),
                                                                                    Telemetry.timestamp >= since).order_by(Telemetry.timestamp.desc()).limit(MAX_RAW_ROWS)).all()]
    else:
        headers, title = ["Raised", "Cleared", "Sensor", "Severity", "Message"], "Alarm history"
        rows = [[local_text(a.raised_at), local_text(a.cleared_at) if a.cleared_at else "active", a.metric_name, a.severity, a.message]
                for a in db.scalars(select(Alarm).where(Alarm.device_id == d.id, Alarm.raised_at >= since).order_by(Alarm.raised_at.desc()).limit(5000))]
    tmp, filename, media = tablefile.build(f"{title} - {d.device_name}", headers, rows, format, f"energy_{kind}_{d.id}")
    audit.append(db, user.id, user.company_name, "energy.report", f"device:{d.id}/{kind}:{format}", client_ip(request))
    return StreamingResponse(iter_file(tmp), media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
