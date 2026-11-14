"""ERP export (CSV / Excel / PDF) and scheduled reports by e-mail."""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.session import get_master_db
from app.deps import ADMIN_ROLES, client_ip, get_company_db, require_roles, scope_devices
from app.models.master import ReportSchedule, User
from app.models.tenant import Device
from app.services import audit, erp, perms, tablefile
from app.services.export import _offset_minutes, iter_file, plant_tz

router = APIRouter(prefix="/reports", tags=["reports"])
MAX_DAYS = 92


@router.get("/erp")
def erp_export(request: Request, format: str = Query(pattern="^(csv|xlsx|pdf)$"), date_from: datetime = Query(), date_to: datetime = Query(),
               user: User = Depends(perms.require("reports", 1)), db: Session = Depends(get_company_db)):
    """Daily minimum / average / maximum / last value of every sensor the caller may see, for ERP or Excel."""
    if date_from.tzinfo is None:
        date_from = date_from.replace(tzinfo=timezone.utc)
    if date_to.tzinfo is None:
        date_to = date_to.replace(tzinfo=timezone.utc)
    if date_from >= date_to:
        raise AppError(422, "INVALID_DATE_RANGE", "The start date must be earlier than the end date.")
    if date_to - date_from > timedelta(days=MAX_DAYS):
        raise AppError(413, "EXPORT_TOO_LARGE", f"Choose at most {MAX_DAYS} days at a time.")
    devices = list(db.scalars(scope_devices(select(Device).where(Device.protocol != "Gateway").order_by(Device.device_name), user)))
    rows = erp.rows(db, devices, date_from, date_to, _offset_minutes())
    name = f"{user.company_name}_erp_{datetime.now(plant_tz()):%Y%m%d_%H%M}"
    tmp, filename, media = tablefile.build(erp.title(user.company_name), erp.HEADERS, rows, format, name, group=8)
    audit.append(db, user.id, user.company_name, "report.erp_export", f"erp:{format}", client_ip(request))
    return StreamingResponse(iter_file(tmp), media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["dashboard", "erp"]
    fmt: Literal["csv", "xlsx", "pdf"] = "xlsx"
    frequency: Literal["daily", "weekly"] = "daily"
    hour: int = Field(default=8, ge=0, le=23)          # plant local time
    weekday: int = Field(default=0, ge=0, le=6)        # 0 = Monday (weekly only)
    recipients: str = Field(min_length=3, max_length=500)   # e-mail addresses separated by commas


def _out(s: ReportSchedule) -> dict:
    return {"id": s.id, "name": s.name, "kind": s.kind, "fmt": s.fmt, "frequency": s.frequency, "hour": s.hour, "weekday": s.weekday,
            "recipients": s.recipients, "last_sent_at": s.last_sent_at}


def _check_recipients(text: str) -> str:
    items = [x.strip() for x in text.split(",") if x.strip()]
    if not items or len(items) > 10 or any("@" not in x or " " in x for x in items):
        raise AppError(422, "RECIPIENTS_INVALID", "Enter up to 10 e-mail addresses separated by commas.")
    return ", ".join(items)


@router.get("/schedules")
def list_schedules(user: User = Depends(require_roles(*ADMIN_ROLES)), master: Session = Depends(get_master_db)):
    return [_out(s) for s in master.scalars(select(ReportSchedule).where(ReportSchedule.company_name == user.company_name).order_by(ReportSchedule.id))]


@router.post("/schedules", status_code=201)
def add_schedule(body: ScheduleIn, request: Request, user: User = Depends(require_roles("admin")),
                 master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    s = ReportSchedule(company_name=user.company_name, created_by=user.username, **{**body.model_dump(), "recipients": _check_recipients(body.recipients)})
    master.add(s)
    master.commit()
    audit.append(db, user.id, user.company_name, "report.schedule_add", f"schedule:{s.id}", client_ip(request))
    return _out(s)


@router.delete("/schedules/{schedule_id}")
def remove_schedule(schedule_id: int, request: Request, user: User = Depends(require_roles("admin")),
                    master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    s = master.get(ReportSchedule, schedule_id)
    if not s or s.company_name != user.company_name:
        raise AppError(404, "SCHEDULE_NOT_FOUND", "Schedule not found.")
    master.delete(s)
    master.commit()
    audit.append(db, user.id, user.company_name, "report.schedule_remove", f"schedule:{schedule_id}", client_ip(request))
    return {"removed": True}
