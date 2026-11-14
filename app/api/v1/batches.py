"""Batch tracking: start, end, list, details and a report (PDF / Excel / CSV) per batch."""
import re

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.timeutil import utcnow
from app.deps import client_ip, get_company_db, get_scoped_device, scope_devices
from app.models.master import User
from app.models.tenant import Batch, Device
from app.services import audit, batches, perms, tablefile
from app.services.export import iter_file, local_text, tz_label

router = APIRouter(prefix="/batches", tags=["batches"])


class StartIn(BaseModel):
    device_id: int
    batch_no: str = Field(min_length=1, max_length=64)
    product: str | None = Field(default=None, max_length=120)


def _get(db, user, batch_id) -> tuple[Batch, Device]:
    b = db.get(Batch, batch_id)
    if not b:
        raise AppError(404, "BATCH_NOT_FOUND", "Batch not found.")
    return b, get_scoped_device(db, user, b.device_id)


@router.post("/start", status_code=201)
def start(body: StartIn, request: Request, user: User = Depends(perms.require("control", 1)), db: Session = Depends(get_company_db)):
    d = get_scoped_device(db, user, body.device_id)
    if db.scalar(select(Batch.id).where(Batch.device_id == d.id, Batch.ended_at.is_(None))):
        raise AppError(409, "BATCH_RUNNING", "A batch is already running on this device. End it first.")
    b = Batch(device_id=d.id, batch_no=body.batch_no.strip(), product=body.product, started_at=utcnow(), started_by=user.username)
    db.add(b)
    db.commit()
    audit.append(db, user.id, user.company_name, "batch.start", f"device:{d.id}/{b.batch_no}", client_ip(request))
    return batches.to_dict(db, b, d.device_name)


@router.post("/{batch_id}/end")
def end(batch_id: int, request: Request, user: User = Depends(perms.require("control", 1)), db: Session = Depends(get_company_db)):
    b, d = _get(db, user, batch_id)
    if b.ended_at:
        raise AppError(409, "BATCH_ENDED", "This batch has already ended.")
    batches.finish(db, b, user.username)
    audit.append(db, user.id, user.company_name, "batch.end", f"device:{d.id}/{b.batch_no}", client_ip(request))
    return batches.to_dict(db, b, d.device_name, full=True)


@router.get("")
def list_batches(device_id: int | None = None, limit: int = Query(50, ge=1, le=200), user: User = Depends(perms.require("monitor", 1)),
                 db: Session = Depends(get_company_db)):
    stmt = scope_devices(select(Batch, Device).join(Device, Device.id == Batch.device_id), user).order_by(Batch.id.desc()).limit(limit)
    if device_id:
        stmt = stmt.where(Batch.device_id == device_id)
    return [batches.to_dict(db, b, d.device_name) for b, d in db.execute(stmt).all()]


@router.get("/{batch_id}")
def details(batch_id: int, user: User = Depends(perms.require("monitor", 1)), db: Session = Depends(get_company_db)):
    b, d = _get(db, user, batch_id)
    return batches.to_dict(db, b, d.device_name, full=True)


@router.get("/{batch_id}/report")
def report(batch_id: int, request: Request, format: str = Query("pdf", pattern="^(csv|xlsx|pdf)$"), user: User = Depends(perms.require("reports", 1)),
           db: Session = Depends(get_company_db)):
    b, d = _get(db, user, batch_id)
    s = batches.to_dict(db, b, d.device_name, full=True)
    rows = [["Batch", b.batch_no, "", "", ""], ["Product", b.product or "", "", "", ""], ["Device", d.device_name, "", "", ""],
            [f"Started ({tz_label()})", local_text(b.started_at), "by", b.started_by, ""],
            ["Ended", local_text(b.ended_at) if b.ended_at else "running", "by", b.ended_by or "", ""], ["", "", "", "", ""],
            ["Sensor", "Min", "Average", "Max", "Unit"]]
    rows += [[x["metric"], x["min"], x["avg"], x["max"], x["unit"]] for x in s["summary"]["sensors"]]
    rows += [["", "", "", "", ""], ["Alarms during the batch", len(s["summary"]["alarms"]), "", "", ""]]
    rows += [[a["metric"], a["severity"], a["message"][:60], local_text(a["raised_at"]), ""] for a in s["summary"]["alarms"]]
    rows += [["", "", "", "", ""], ["Changes sent to the plant", len(s["summary"]["writes"]), "", "", ""]]
    rows += [[w["metric"], w["value"], w["state"], w["by"], w["reason"] or ""] for w in s["summary"]["writes"]]
    tmp, filename, media = tablefile.build(f"Batch {b.batch_no}", ["Item", "Value", "", "", ""], rows, format, "batch_" + re.sub(r"[^A-Za-z0-9_-]", "_", b.batch_no))
    audit.append(db, user.id, user.company_name, "batch.report", f"batch:{b.id}:{format}", client_ip(request))
    return StreamingResponse(iter_file(tmp), media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
