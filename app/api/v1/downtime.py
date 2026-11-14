"""Downtime log: list, reason entry, availability % and Pareto, export. Reasons are chosen from downtime.REASONS."""
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.timeutil import utcnow
from app.deps import client_ip, get_company_db, get_scoped_device, scope_devices
from app.models.master import User
from app.models.tenant import Device, DowntimeEvent
from app.services import audit, downtime, perms, tablefile
from app.services.export import iter_file, local_text, tz_label

router = APIRouter(prefix="/downtime", tags=["downtime"])


class ReasonIn(BaseModel):
    reason: str = Field(min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=200)


def _out(e: DowntimeEvent, name: str, now) -> dict:
    return {"id": e.id, "device_id": e.device_id, "device_name": name, "started_at": e.started_at.isoformat(),
            "ended_at": e.ended_at.isoformat() if e.ended_at else None, "minutes": round(downtime.minutes(e, now), 1),
            "source": e.source, "reason": e.reason, "note": e.note, "set_by": e.set_by}


def _rows(db, user, device_id, days):
    stmt = scope_devices(select(DowntimeEvent, Device).join(Device, Device.id == DowntimeEvent.device_id), user)
    if device_id:
        stmt = stmt.where(DowntimeEvent.device_id == device_id)
    return db.execute(stmt.where(DowntimeEvent.started_at >= utcnow() - timedelta(days=days)).order_by(DowntimeEvent.started_at.desc()).limit(2000)).all()


@router.get("/reasons")
def reasons(user: User = Depends(perms.require("monitor", 1))):
    return downtime.REASONS


@router.get("")
def list_events(device_id: int | None = None, days: int = Query(7, ge=1, le=365), user: User = Depends(perms.require("monitor", 1)),
                db: Session = Depends(get_company_db)):
    now = utcnow()
    return [_out(e, d.device_name, now) for e, d in _rows(db, user, device_id, days)]


@router.get("/summary")
def get_summary(device_id: int | None = None, days: int = Query(7, ge=1, le=365), user: User = Depends(perms.require("monitor", 1)),
                db: Session = Depends(get_company_db)):
    stmt = scope_devices(select(Device).where(Device.protocol != "Gateway"), user)
    if device_id:
        stmt = stmt.where(Device.id == device_id)
    return downtime.summary(db, list(db.scalars(stmt)), days)


@router.put("/{event_id}")
def set_reason(event_id: int, body: ReasonIn, request: Request, user: User = Depends(perms.require("alarms", 1)), db: Session = Depends(get_company_db)):
    e = db.get(DowntimeEvent, event_id)
    if not e:
        raise AppError(404, "DOWNTIME_NOT_FOUND", "Stop record not found.")
    d = get_scoped_device(db, user, e.device_id)
    if body.reason not in downtime.REASONS:
        raise AppError(422, "DOWNTIME_REASON", "Choose a reason from the list.")
    e.reason, e.note, e.set_by = body.reason, body.note, user.username
    db.commit()
    audit.append(db, user.id, user.company_name, "downtime.reason", f"device:{d.id}/{body.reason}", client_ip(request))
    return _out(e, d.device_name, utcnow())


@router.get("/export")
def export(request: Request, format: str = Query(pattern="^(csv|xlsx|pdf)$"), device_id: int | None = None, days: int = Query(30, ge=1, le=365),
           user: User = Depends(perms.require("reports", 1)), db: Session = Depends(get_company_db)):
    now = utcnow()
    rows = [[d.device_name, local_text(e.started_at), local_text(e.ended_at) if e.ended_at else "running",
             round(downtime.minutes(e, now), 1), e.source, e.reason or "", e.note or "", e.set_by or ""] for e, d in _rows(db, user, device_id, days)]
    tmp, filename, media = tablefile.build("Downtime log", [f"Device", f"Stopped ({tz_label()})", f"Started again ({tz_label()})", "Minutes", "Detected by", "Reason", "Note", "Entered by"], rows, format, "downtime")
    audit.append(db, user.id, user.company_name, "downtime.export", format, client_ip(request))
    return StreamingResponse(iter_file(tmp), media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
