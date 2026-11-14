"""Read-only share links (a live page anybody with the link can open - nothing can be changed through it) and QR labels for devices."""
import hashlib
import io
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.v1.devices import _merge_seen, _statuses
from app.core.config import settings
from app.core.errors import AppError
from app.core.timeutil import as_utc, utcnow
from app.db.session import MasterSession, get_master_db, tenant_session
from app.deps import client_ip, get_company_db, scope_devices
from app.models.master import ShareLink, User
from app.models.tenant import Alarm, Device, Telemetry
from app.services import audit, csettings, perms

router = APIRouter(prefix="/share", tags=["share"])
public = APIRouter(prefix="/public", tags=["public"])


class ShareIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    scope: str = Field(..., pattern="^(device|region)$")
    device_id: int | None = None
    state: str | None = None
    region: str | None = None
    days: int = Field(7, ge=1, le=365)


class ShareOut(BaseModel):
    id: int
    name: str
    scope: str
    device_id: int | None = None
    state: str | None = None
    region: str | None = None
    expires_at: str
    revoked: bool
    created_by: str
    created_at: str
    last_opened_at: str | None = None
    active: bool


class ShareCreated(ShareOut):
    url: str      # shown ONCE: only a hash of the secret is stored


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _out(s: ShareLink) -> dict:
    return {"id": s.id, "name": s.name, "scope": s.scope, "device_id": s.device_id, "state": s.state, "region": s.region,
            "expires_at": s.expires_at.isoformat(), "revoked": s.revoked, "created_by": s.created_by, "created_at": s.created_at.isoformat(),
            "last_opened_at": s.last_opened_at.isoformat() if s.last_opened_at else None,
            "active": not s.revoked and as_utc(s.expires_at) > utcnow()}


def base_url() -> str:
    return (settings.PUBLIC_API_URL or "").rstrip("/")


@router.post("", response_model=ShareCreated, status_code=201)
def create_link(body: ShareIn, request: Request, user: User = Depends(perms.require("reports", 2)),
                db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    company = user.company_name
    longest = csettings.get(company, "share_link_max_days")
    if body.days > longest:
        raise AppError(422, "SHARE_TOO_LONG", f"A share link can work for {longest} days at most.")
    state = region = None
    if body.scope == "device":
        if body.device_id is None:
            raise AppError(422, "DEVICE_REQUIRED", "Choose the device to share.")
        d = db.scalar(scope_devices(select(Device).where(Device.id == body.device_id), user))
        if not d:
            raise AppError(404, "DEVICE_NOT_FOUND", "Device not found or outside your assigned state/region.")
    else:
        state, region = (body.state, body.region) if user.role == "admin" else (user.state, user.region)
        if not state or not region:
            raise AppError(422, "SITE_REQUIRED", "Choose the State and Region to share.")
    token = secrets.token_urlsafe(32)
    link = ShareLink(token_hash=_hash(token), company_name=company, name=body.name, scope=body.scope, device_id=body.device_id,
                     state=state, region=region, expires_at=utcnow() + timedelta(days=body.days), created_by=user.username)
    master.add(link)
    master.commit()
    master.refresh(link)
    audit.append(db, user.id, company, "share.create", f"share:{link.id} {body.scope}", client_ip(request))
    return {**_out(link), "url": f"{base_url()}/api/v1/public/{token}/view"}


@router.get("", response_model=list[ShareOut])
def list_links(user: User = Depends(perms.require("reports", 1)), master: Session = Depends(get_master_db)):
    stmt = select(ShareLink).where(ShareLink.company_name == user.company_name).order_by(ShareLink.id.desc()).limit(200)
    return [_out(s) for s in master.scalars(stmt) if user.role == "admin" or s.created_by == user.username]


@router.delete("/{link_id}", response_model=ShareOut)
def revoke_link(link_id: int, request: Request, user: User = Depends(perms.require("reports", 2)),
                db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    s = master.scalar(select(ShareLink).where(ShareLink.id == link_id, ShareLink.company_name == user.company_name))
    if not s or (user.role != "admin" and s.created_by != user.username):
        raise AppError(404, "SHARE_NOT_FOUND", "Share link not found.")
    s.revoked = True
    master.commit()
    audit.append(db, user.id, user.company_name, "share.revoke", f"share:{s.id}", client_ip(request))
    return _out(s)


@router.get("/labels")
def qr_labels(device_ids: str, user: User = Depends(perms.require("monitor", 1)), db: Session = Depends(get_company_db)):
    """A PDF page of QR labels (4 x 6 on A4) to stick on the equipment. Scanning one opens that device in the app."""
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode import qr
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    try:
        ids = [int(x) for x in device_ids.split(",") if x.strip()][:60]
    except ValueError:
        raise AppError(422, "BAD_IDS", "device_ids must be numbers separated by commas.")
    devices = list(db.scalars(scope_devices(select(Device).where(Device.id.in_(ids)), user).order_by(Device.device_name))) if ids else []
    if not devices:
        raise AppError(404, "DEVICE_NOT_FOUND", "No device found in your assigned state/region.")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    cols, rows = 3, 6
    cw, ch = w / cols, h / rows
    for i, d in enumerate(devices):
        if i and i % (cols * rows) == 0:
            c.showPage()
        x, y = (i % cols) * cw, h - ((i // cols) % rows + 1) * ch
        widget = qr.QrCodeWidget(f"{base_url()}/open/device/{d.id}")
        x0, y0, x1, y1 = widget.getBounds()
        size = ch - 40
        dr = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
        dr.add(widget)
        renderPDF.draw(dr, c, x + (cw - size) / 2, y + 28)
        c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(x + cw / 2, y + 16, d.device_name[:34])
        c.setFont("Helvetica", 7.5)
        c.drawCentredString(x + cw / 2, y + 6, f"{d.plant_name or ''} {d.state}/{d.region}"[:44])
    c.save()
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="stech_qr_labels.pdf"'})


# ------------------------------------------------------------------ the public, read-only page (no login)
def _find(master: Session, token: str) -> ShareLink:
    s = master.scalar(select(ShareLink).where(ShareLink.token_hash == _hash(token)))
    if not s or s.revoked or as_utc(s.expires_at) <= utcnow():
        raise AppError(404, "LINK_INVALID", "This link has ended or was withdrawn.")
    return s


@public.get("/{token}")
def public_data(token: str):
    with MasterSession() as master:
        s = _find(master, token)
        s.last_opened_at = utcnow()
        master.commit()
        company, scope, device_id, state, region, name = s.company_name, s.scope, s.device_id, s.state, s.region, s.name
        with tenant_session(master, company) as db:
            stmt = select(Device).where(Device.protocol != "Gateway")
            stmt = stmt.where(Device.id == device_id) if scope == "device" else stmt.where(Device.state == state, Device.region == region)
            devices = list(db.scalars(stmt.order_by(Device.device_name).limit(60)))
            ids = [d.id for d in devices]
            last = _merge_seen(company, dict(db.execute(select(Telemetry.device_id, func.max(Telemetry.timestamp))
                                                       .where(Telemetry.device_id.in_(ids)).group_by(Telemetry.device_id)).all())) if ids else {}
            states = _statuses(master, company, devices, last)[0]
            latest: dict[int, list] = {}
            if ids:
                for dev, metric, val in db.execute(text("""SELECT DISTINCT ON (device_id, metric_name) device_id, metric_name, value FROM telemetry
                                                       WHERE device_id = ANY(:ids) AND "timestamp" > :since ORDER BY device_id, metric_name, "timestamp" DESC"""),
                                                    {"ids": ids, "since": utcnow() - timedelta(days=7)}).all():
                    latest.setdefault(dev, []).append({"metric": metric, "value": float(val)})
            alarms = list(db.scalars(select(Alarm).where(Alarm.device_id.in_(ids), Alarm.cleared_at.is_(None)).order_by(Alarm.raised_at.desc()).limit(30))) if ids else []
            names = {d.id: d.device_name for d in devices}
            return {"name": name, "company": company, "generated_at": utcnow().isoformat(),
                    "devices": [{"name": d.device_name, "plant": d.plant_name, "status": states.get(d.id, "offline"), "values": latest.get(d.id, [])} for d in devices],
                    "alarms": [{"device": names.get(a.device_id), "metric": a.metric_name, "severity": a.severity, "message": a.message,
                                "raised_at": a.raised_at.isoformat()} for a in alarms]}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex"><title>STECH iVIEW - live view</title>
<style>:root{color-scheme:light dark;--bg:#F4F6F8;--c:#fff;--t:#1B2630;--m:#5C6B77;--b:#D7DDE2}
@media(prefers-color-scheme:dark){:root{--bg:#14171A;--c:#1D2125;--t:#E6E8EA;--m:#9AA3AB;--b:#333A40}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--t);font-family:Segoe UI,Roboto,Arial,sans-serif;padding:16px}
h1{font-size:20px;margin:0}small{color:var(--m)}.g{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));margin:14px 0}
.c{background:var(--c);border:1px solid var(--b);border-radius:10px;padding:12px}.s{font-size:12px;font-weight:600}
.online{color:#2E9E5B}.stale{color:#8A949C}.offline,.plant_offline{color:#D64545}.r{display:flex;justify-content:space-between;font-size:14px;margin-top:4px}
.a{border-left:4px solid #D64545;padding:6px 10px;margin:6px 0;background:var(--c);border-radius:6px;font-size:14px}.a.major{border-color:#E8892B}.a.minor,.a.advisory{border-color:#C9A227}</style></head>
<body><h1 id="n">STECH iVIEW</h1><small id="u">Read-only live view. Nothing here can be changed.</small><div class="g" id="d"></div><h2 style="font-size:16px">Active alarms</h2><div id="al"></div>
<script>
var T=location.pathname.replace(/\\/view$/,"");
function e(t,c,x){var n=document.createElement(t);if(c)n.className=c;if(x!==undefined)n.textContent=x;return n}
function load(){fetch(T,{cache:"no-store"}).then(function(r){if(!r.ok)throw 0;return r.json()}).then(function(j){
document.getElementById("n").textContent=j.name;document.getElementById("u").textContent="Updated "+new Date(j.generated_at).toLocaleTimeString()+" - read-only";
var d=document.getElementById("d");d.textContent="";j.devices.forEach(function(x){var c=e("div","c");c.appendChild(e("b","",x.name));c.appendChild(e("div","s "+x.status,x.status.replace("_"," ").toUpperCase()));
x.values.slice(0,6).forEach(function(v){var r=e("div","r");r.appendChild(e("span","",v.metric));r.appendChild(e("b","",String(Math.round(v.value*100)/100)));c.appendChild(r)});d.appendChild(c)});
var a=document.getElementById("al");a.textContent="";if(!j.alarms.length)a.appendChild(e("small","","No active alarms."));
j.alarms.forEach(function(x){a.appendChild(e("div","a "+x.severity,x.device+": "+x.message))})}).catch(function(){document.getElementById("u").textContent="This link has ended or was withdrawn."})}
load();setInterval(load,3000);
</script></body></html>"""


@public.get("/{token}/view", response_class=HTMLResponse)
def public_view(token: str):
    with MasterSession() as master:
        _find(master, token)
    return HTMLResponse(PAGE, headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
