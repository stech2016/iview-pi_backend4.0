"""Plant health panel, alarm response report, plant map and shift handover."""
import io
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.devices import _merge_seen, _statuses
from app.core.errors import AppError
from app.core.timeutil import utcnow
from app.db.session import get_master_db
from app.deps import client_ip, get_company_db, in_scope, scope_devices
from app.models.master import Handover, User
from app.models.tenant import Alarm, AlarmShelf, Device, Telemetry
from app.services import audit, perms, plantsummary
from sqlalchemy import func

router = APIRouter(prefix="/plant", tags=["plant"])


def _devices(db: Session, user: User, state: str | None, region: str | None) -> list[Device]:
    stmt = scope_devices(select(Device), user)
    if state:
        stmt = stmt.where(Device.state == state)
    if region:
        stmt = stmt.where(Device.region == region)
    return list(db.scalars(stmt))


def _states(db: Session, master: Session, user: User, devices: list[Device]) -> dict:
    last = _merge_seen(user.company_name, dict(db.execute(select(Telemetry.device_id, func.max(Telemetry.timestamp))
                                               .where(Telemetry.timestamp > utcnow() - timedelta(days=365)).group_by(Telemetry.device_id)).all()))
    return _statuses(master, user.company_name, devices, last)[0]


def _summary(db: Session, master: Session, user: User, state: str | None, region: str | None) -> dict:
    devices = _devices(db, user, state, region)
    return plantsummary.health(db, user.company_name, devices, _states(db, master, user, devices))


@router.get("/health")
def plant_health(state: str | None = None, region: str | None = None, user: User = Depends(perms.require("monitor", 1)),
                 db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    return _summary(db, master, user, state, region)


@router.get("/alarm-report")
def alarm_report(days: int = 7, state: str | None = None, region: str | None = None, user: User = Depends(perms.require("reports", 1)),
                 db: Session = Depends(get_company_db)):
    return plantsummary.alarm_report(db, _devices(db, user, state, region), min(max(days, 1), 90))


@router.get("/map")
def plant_map(user: User = Depends(perms.require("monitor", 1)), db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    """Devices that have a position (latitude / longitude set on the device) with their live status; the app draws them on a map."""
    devices = [d for d in _devices(db, user, None, None) if d.latitude is not None and d.longitude is not None and d.protocol != "Gateway"]
    states = _states(db, master, user, devices)
    open_alarms = dict(db.execute(select(Alarm.device_id, func.count()).where(Alarm.cleared_at.is_(None)).group_by(Alarm.device_id)).all())
    return [{"id": d.id, "name": d.device_name, "plant": d.plant_name, "state": d.state, "region": d.region, "latitude": d.latitude,
             "longitude": d.longitude, "status": states.get(d.id, "offline"), "active_alarms": open_alarms.get(d.id, 0)} for d in devices]


@router.get("/events")
def trend_events(device_id: int, range: str = "8h", user: User = Depends(perms.require("monitor", 1)), db: Session = Depends(get_company_db)):
    """Markers for a trend chart: alarms (raised) and writes made on this device within the range, oldest first."""
    from app.api.v1.devices import RANGES
    from app.models.tenant import Command
    from app.deps import get_scoped_device
    d = get_scoped_device(db, user, device_id)
    since = utcnow() - RANGES.get(range, timedelta(hours=8))
    out = [{"type": "alarm", "time": a.raised_at.isoformat(), "metric": a.metric_name, "severity": a.severity, "text": a.message}
           for a in db.scalars(select(Alarm).where(Alarm.device_id == d.id, Alarm.raised_at >= since).order_by(Alarm.raised_at).limit(100))]
    out += [{"type": "write", "time": c.created_at.isoformat(), "metric": c.metric_name, "severity": "info",
             "text": f"{c.requested_by}: {c.metric_name} {c.previous_value if c.previous_value is not None else '?'} -> {c.value:g} ({c.state})"}
            for c in db.scalars(select(Command).where(Command.device_id == d.id, Command.created_at >= since).order_by(Command.created_at).limit(100))]
    return sorted(out, key=lambda e: e["time"])


# ------------------------------------------------------------------ shift handover
class HandoverIn(BaseModel):
    state: str | None = None
    region: str | None = None
    shift_label: str | None = Field(None, max_length=40)
    note: str = Field("", max_length=4000)


class SignIn(BaseModel):
    note: str = Field("", max_length=2000)


class HandoverOut(BaseModel):
    id: int
    state: str
    region: str
    from_user: str
    shift_label: str | None = None
    note: str
    summary: dict
    created_at: str
    signed_by: str | None = None
    signed_at: str | None = None
    sign_note: str | None = None


def _out(h: Handover) -> HandoverOut:
    try:
        summary = json.loads(h.summary)
    except ValueError:
        summary = {}
    return HandoverOut(id=h.id, state=h.state, region=h.region, from_user=h.from_user, shift_label=h.shift_label, note=h.note, summary=summary,
                       created_at=h.created_at.isoformat(), signed_by=h.signed_by, signed_at=h.signed_at.isoformat() if h.signed_at else None, sign_note=h.sign_note)


def _site(user: User, state: str | None, region: str | None) -> tuple[str, str]:
    if user.role != "admin":
        return user.state, user.region          # everybody else hands over their own State + Region only
    if not state or not region:
        raise AppError(422, "SITE_REQUIRED", "Choose the State and Region this handover is for.")
    return state, region


def snapshot(db: Session, master: Session, user: User, state: str, region: str) -> dict:
    devices = _devices(db, user, state, region)
    ids = [d.id for d in devices]
    now = utcnow()
    names = {d.id: d.device_name for d in devices}
    open_alarms = list(db.scalars(select(Alarm).where(Alarm.device_id.in_(ids), Alarm.cleared_at.is_(None)).order_by(Alarm.raised_at.desc()).limit(15))) if ids else []
    shelves = list(db.scalars(select(AlarmShelf).where(AlarmShelf.device_id.in_(ids), AlarmShelf.until > now))) if ids else []
    return {
        "health": plantsummary.health(db, user.company_name, devices, _states(db, master, user, devices)),
        "open_alarms": [{"device": names.get(a.device_id), "metric": a.metric_name, "severity": a.severity, "message": a.message,
                         "raised_at": a.raised_at.isoformat(), "acknowledged_by": a.acknowledged_by} for a in open_alarms],
        "shelved": [{"device": names.get(s.device_id), "metric": s.metric_name, "until": s.until.isoformat(), "by": s.shelved_by, "reason": s.reason} for s in shelves],
        "maintenance": [{"device": d.device_name, "until": d.maintenance_until.isoformat(), "note": d.maintenance_note} for d in devices
                        if d.maintenance_until and d.maintenance_until > now],
        "commands_12h": plantsummary.commands_since(db, now - timedelta(hours=12), ids),
    }


@router.get("/handover/draft")
def handover_draft(state: str | None = None, region: str | None = None, user: User = Depends(perms.require("alarms", 1)),
                   db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    """What iVIEW will attach to the handover: shown to the person before they write their note."""
    s, r = _site(user, state, region)
    return {"state": s, "region": r, "summary": snapshot(db, master, user, s, r)}


@router.post("/handover", response_model=HandoverOut, status_code=201)
def create_handover(body: HandoverIn, request: Request, user: User = Depends(perms.require("alarms", 2)),
                    db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    s, r = _site(user, body.state, body.region)
    h = Handover(company_name=user.company_name, state=s, region=r, from_user=user.username, from_user_id=user.id,
                 shift_label=body.shift_label, note=body.note, summary=json.dumps(snapshot(db, master, user, s, r), default=str))
    master.add(h)
    master.commit()
    master.refresh(h)
    audit.append(db, user.id, user.company_name, "handover.create", f"handover:{h.id} {s}/{r}", client_ip(request))
    return _out(h)


@router.get("/handover", response_model=list[HandoverOut])
def list_handovers(limit: int = 30, user: User = Depends(perms.require("monitor", 1)), master: Session = Depends(get_master_db)):
    stmt = select(Handover).where(Handover.company_name == user.company_name).order_by(Handover.id.desc()).limit(min(max(limit, 1), 100))
    if user.role != "admin":
        stmt = stmt.where(Handover.state == user.state, Handover.region == user.region)
    return [_out(h) for h in master.scalars(stmt)]


def _get(master: Session, user: User, hid: int) -> Handover:
    h = master.scalar(select(Handover).where(Handover.id == hid, Handover.company_name == user.company_name))
    if not h or not in_scope(user, h.state, h.region):
        raise AppError(404, "HANDOVER_NOT_FOUND", "Handover not found or outside your assigned state/region.")
    return h


@router.post("/handover/{hid}/sign", response_model=HandoverOut)
def sign_handover(hid: int, body: SignIn, request: Request, user: User = Depends(perms.require("alarms", 2)),
                  db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    h = _get(master, user, hid)
    if h.signed_at:
        raise AppError(409, "ALREADY_SIGNED", "This handover was already signed.")
    if h.from_user_id == user.id:
        raise AppError(403, "CANNOT_SIGN_OWN", "The incoming person signs the handover, not the person who wrote it.")
    h.signed_by, h.signed_by_id, h.signed_at, h.sign_note = user.username, user.id, utcnow(), body.note or None
    master.commit()
    audit.append(db, user.id, user.company_name, "handover.sign", f"handover:{h.id}", client_ip(request))
    return _out(h)


@router.get("/handover/{hid}/pdf")
def handover_pdf(hid: int, user: User = Depends(perms.require("reports", 1)), master: Session = Depends(get_master_db)):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from app.services.export import local_str, tz_label
    from app.core.timeutil import as_utc
    h = _get(master, user, hid)
    s = json.loads(h.summary or "{}")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, ht = A4
    y = [ht - 40]

    def line(text: str, size: float = 9, bold: bool = False, gap: float = 13):
        if y[0] < 50:
            c.showPage()
            y[0] = ht - 40
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(36, y[0], text[:110])
        y[0] -= gap
    line(f"STECH iVIEW - Shift handover: {h.company_name}", 14, True, 20)
    line(f"{h.state} / {h.region}   Shift: {h.shift_label or '-'}   Written {local_str(as_utc(h.created_at))} {tz_label()}", 9)
    line(f"From: {h.from_user}    Signed by: {h.signed_by or 'not signed yet'}" + (f" at {local_str(as_utc(h.signed_at))} {tz_label()}" if h.signed_at else ""), 9, gap=18)
    hl = s.get("health", {})
    line("Plant at handover", 11, True)
    line(f"Health {hl.get('score')}%   Devices online {hl.get('devices_online')}/{hl.get('devices_total')}   Active alarms {hl.get('active_alarms')}   "
         f"Alarms last hour {hl.get('alarms_last_hour')}   Stale alarms {hl.get('stale_alarms')}   Uptime 24h {hl.get('uptime_24h_pct')}%", gap=18)
    line("Note from the outgoing person", 11, True)
    for part in (h.note or "-").splitlines() or ["-"]:
        line(part)
    y[0] -= 6
    line("Open alarms", 11, True)
    for a in s.get("open_alarms", []) or [{"message": "none"}]:
        line(f"[{a.get('severity', '')}] {a.get('device', '')} {a.get('metric') or ''}: {a.get('message', '')}" + (f" (ack {a['acknowledged_by']})" if a.get("acknowledged_by") else ""))
    y[0] -= 6
    line("Shelved alarms / Maintenance", 11, True)
    for x in s.get("shelved", []):
        line(f"Shelved: {x['device']} {x['metric']} until {x['until'][:16]} by {x['by']} - {x.get('reason') or ''}")
    for x in s.get("maintenance", []):
        line(f"Maintenance: {x['device']} until {x['until'][:16]} - {x.get('note') or ''}")
    if not s.get("shelved") and not s.get("maintenance"):
        line("none")
    y[0] -= 6
    line("Writes to the plant in the last 12 hours", 11, True)
    for x in s.get("commands_12h", []) or [{"device": "none"}]:
        line(f"{x.get('at', '')[:16]} {x.get('device', '')} {x.get('metric', '')} = {x.get('value', '')} [{x.get('state', '')}] by {x.get('by', '')}" if x.get("metric") else "none")
    if h.sign_note:
        y[0] -= 6
        line("Note from the incoming person", 11, True)
        line(h.sign_note)
    c.save()
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="handover_{h.id}.pdf"'})
