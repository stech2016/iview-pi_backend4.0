"""Plant PCs (gateways): the Gateway page (list / add / download / reset key / remove) and the settings each plant script fetches.

Super admin: every plant PC of the company. Regional admin: only the plant PCs of their own State + Region.
A State + Region may have several plant PCs; each device names the one that reads it.
"""
import hmac
import json

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core import geo, normalize
from app.core.constants import REGIONS
from app.core.errors import AppError
from app.core.rate_limit import limiter
from app.core.timeutil import utcnow
from app.db.session import get_master_db, open_tenant_session
from app.deps import ADMIN_ROLES, client_ip, get_company_db, in_scope, require_roles, scope_devices
from app.models.master import Gateway, User
from app.models.tenant import Device
from app.schemas.auth import MessageOut
from app.services import audit, csettings, enroll, gateway as svc, gwcontrol, perms, selfupdate

router = APIRouter(prefix="/gateway", tags=["gateway"])
admin_level = require_roles(*ADMIN_ROLES)
MAX_PER_REGION = 20


class GatewayIn(BaseModel):
    state: str
    region: str
    name: str | None = Field(default=None, max_length=80)

    @field_validator("state", mode="before")
    @classmethod
    def _state_case(cls, v):
        return normalize.state(v)

    @field_validator("region", mode="before")
    @classmethod
    def _region_case(cls, v):
        return normalize.region(v)

    @field_validator("state")
    @classmethod
    def _state(cls, v):
        if not geo.is_state(v):
            raise ValueError("Select a valid State / province")
        return v

    @field_validator("region")
    @classmethod
    def _region(cls, v):
        if v not in REGIONS:
            raise ValueError("Region must be one of North, South, East, West")
        return v


def _rows(master: Session, company: str) -> list[Gateway]:
    return list(master.scalars(select(Gateway).where(Gateway.company_name == company)))


def _may_use(user: User, info: svc.GwInfo) -> bool:
    """Super admin: any gateway. Regional admin: the gateway of their own State + Region."""
    return user.role == "admin" or (info.site_state == user.state and info.site_region == user.region)


def _fresh_info(user: User, master: Session, gid: str) -> svc.GwInfo:
    info = svc.build_infos(user.company_name, _rows(master, user.company_name)).get(gid)
    if not info:
        raise AppError(404, "GATEWAY_NOT_FOUND", "This gateway was not found.")
    if not _may_use(user, info):
        raise AppError(403, "OUTSIDE_YOUR_REGION", "This gateway belongs to another State / Region. Ask the Super admin.")
    return info


def _items(user: User, master: Session, db: Session) -> list[dict]:
    rows = _rows(master, user.company_name)
    by_id = {r.gateway_id: r for r in rows}
    infos = svc.build_infos(user.company_name, rows)
    specs = svc.device_specs(db)
    out = []
    for gid, info in infos.items():
        if not _may_use(user, info):
            continue
        mine = [d for d in specs if info.allows(d["state"], d["region"], d["plant_pc"])]
        row = by_id.get(gid)
        live = gwcontrol.load_info(user.company_name, gid) if gwcontrol.is_online(row) else {}
        item = {
            "gateway_id": gid, "name": info.name, "is_main": info.is_main,
            "site_state": info.site_state, "site_region": info.site_region,
            "online": gwcontrol.is_online(row), "ever_connected": bool(row and row.state != "never"),
            "last_seen": row.last_seen.isoformat() if row and row.last_seen else None,
            "devices": len(mine), "sensors": sum(len(d["registers"]) for d in mine),
            "version": live.get("ver"), "auto_update": True if row is None else bool(row.auto_update),
            "latest_version": (selfupdate.release() or {}).get("version"), "devices_ok": live.get("ok"), "devices_total": live.get("total"), "backlog": live.get("backlog"),
        }
        # the company-wide 'main' script is only worth showing while it is in use or still has something to read
        if info.is_main and not (item["ever_connected"] or item["devices"] or not info.claimed):
            continue
        out.append(item)
    out.sort(key=lambda i: (not i["is_main"], i["name"].lower()))
    return out


def _cover(user: User, master: Session, items: list[dict]) -> dict | None:
    """For a Regional admin: who reads their State + Region right now - their own plant PCs, or the company-wide script."""
    if user.role == "admin":
        return None
    own = [i for i in items if not i["is_main"]]
    main = master.scalar(select(Gateway).where(Gateway.company_name == user.company_name, Gateway.gateway_id == svc.MAIN))
    return {"state": user.state, "region": user.region, "has_own": bool(own),
            "main_online": gwcontrol.is_online(main), "main_ever": bool(main and main.state != "never")}


@router.get("/list")
def list_gateways(user: User = Depends(admin_level), master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    """Every plant PC the caller may see, with status, how many devices / sensors it reads and (when running) its version."""
    items = _items(user, master, db)
    return {"gateways": items, "cover": _cover(user, master, items),
            "can_add": True,
            "has_devices": db.scalar(scope_devices(select(Device.id).where(Device.protocol != "Gateway"), user).limit(1)) is not None}


@router.get("/summary")
def summary(user: User = Depends(admin_level), master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    """For the two pills on the dashboard: the server is reachable (this answer), and how many plant PCs that have devices are connected."""
    items = _items(user, master, db)
    needed = [i for i in items if i["devices"] > 0]
    ok = [i for i in needed if i["online"]]
    standby = [i for i in items if i["devices"] == 0]
    # V2.0: the customers' own gateways count the same way (every switched-on one is expected to be connected)
    from app.models.master import PlantLink
    from app.services import extgw
    stmt = select(PlantLink).where(PlantLink.company_name == user.company_name, PlantLink.enabled.is_(True))
    if user.role != "admin":
        stmt = stmt.where(PlantLink.site_state == user.state, PlantLink.site_region == user.region)
    links = list(master.scalars(stmt))
    linked_ok = [r for r in links if extgw.is_online(r)]
    total, connected = len(needed) + len(links), len(ok) + len(linked_ok)
    return {"server": True, "plant_pcs": total, "connected": connected, "all_connected": bool(total) and connected == total,
            "standby": len(standby), "waiting": [i["name"] for i in needed if not i["online"]] + [r.name for r in links if not extgw.is_online(r)],
            "customer_gateways": len(links), "customer_gateways_connected": len(linked_ok)}


@router.get("/choices")
def choices(state: str = Query(max_length=64), region: str = Query(max_length=10), user: User = Depends(perms.require("config", 1)),
            master: Session = Depends(get_master_db)):
    """The plant PCs of one State + Region, for the 'Plant PC' choice when adding a device. Operators use it too."""
    if not in_scope(user, state, region):
        raise AppError(403, "OUTSIDE_YOUR_REGION", "This State / Region is outside your assigned area.")
    rows = [r for r in _rows(master, user.company_name) if r.site_state == state and r.site_region == region]
    infos = svc.build_infos(user.company_name, rows)
    return {"plant_pcs": [{"id": g, "name": i.name} for g, i in infos.items() if i.site_state]}


@router.get("/status")
def status(user: User = Depends(admin_level), master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    """One summary for the whole company / region (older app versions and the 'not connected yet' strip)."""
    items = _items(user, master, db)
    seen = [i for i in items if i["ever_connected"]]
    last = max((i["last_seen"] for i in seen if i["last_seen"]), default=None)
    return {
        "online": bool(seen) and all(i["online"] for i in seen),
        "ever_connected": bool(seen),
        "last_seen": last,
        "devices": sum(i["devices"] for i in items), "sensors": sum(i["sensors"] for i in items),
        "has_devices": db.scalar(scope_devices(select(Device.id).where(Device.protocol != "Gateway"), user).limit(1)) is not None,
    }


@router.post("", status_code=201)
def create_gateway(body: GatewayIn, request: Request, user: User = Depends(admin_level),
                   master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    """Add a plant script for one State + Region. It reads only the devices tagged with that State + Region."""
    if user.role == "regional_admin" and (body.state != user.state or body.region != user.region):
        raise AppError(403, "OUTSIDE_YOUR_REGION", "A Regional admin can add a gateway only for their own State and Region.")
    rows = _rows(master, user.company_name)
    here = [r for r in rows if r.site_state == body.state and r.site_region == body.region]
    if len(here) >= MAX_PER_REGION:
        raise AppError(409, "TOO_MANY_PLANT_PCS", f"{body.state} / {body.region} already has {MAX_PER_REGION} plant PCs.")
    name = (body.name or "").strip() or (f"{body.state} - {body.region}" if not here else f"{body.state} - {body.region} PC {len(here) + 1}")
    if any((r.name or "").strip().lower() == name.lower() for r in here):
        raise AppError(409, "GATEWAY_NAME_EXISTS", f"A plant PC named '{name}' already exists in {body.state} / {body.region}. Choose another name.")
    gid = svc.slug_for(body.state, body.region, {r.gateway_id for r in rows})
    row = Gateway(company_name=user.company_name, gateway_id=gid, name=name,
                  site_state=body.state, site_region=body.region, state="never")
    master.add(row)
    master.commit()
    svc.invalidate(user.company_name)
    gwcontrol.notify_config(user.company_name)  # the company-wide script gives these devices up
    audit.append(db, user.id, user.company_name, "gateway.create", f"gateway:{gid}", client_ip(request))
    return {"gateway_id": gid, "name": row.name}


@router.delete("/{gid}", status_code=204)
def delete_gateway(gid: str, request: Request, user: User = Depends(admin_level),
                   master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    if gid == svc.MAIN:
        raise AppError(409, "CANNOT_DELETE_MAIN", "The company-wide gateway cannot be removed.")
    _fresh_info(user, master, gid)
    row = master.scalar(select(Gateway).where(Gateway.company_name == user.company_name, Gateway.gateway_id == gid))
    if row:
        master.delete(row)
        master.commit()
    db.execute(update(Device).where(Device.plant_pc == gid).values(plant_pc=None))   # its devices go to the region's first plant PC
    db.commit()
    svc.invalidate(user.company_name)
    gwcontrol.notify_config(user.company_name)  # its devices go back to the company-wide script
    audit.append(db, user.id, user.company_name, "gateway.delete", f"gateway:{gid}", client_ip(request))


@router.post("/{gid}/reset-key", response_model=MessageOut)
def reset_key(gid: str, request: Request, user: User = Depends(admin_level),
              master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    """Cancels the key inside every copy of this gateway's script already downloaded (lost / stolen PC).
    Download the script again and replace link.json on the plant PC."""
    _fresh_info(user, master, gid)
    row = master.scalar(select(Gateway).where(Gateway.company_name == user.company_name, Gateway.gateway_id == gid))
    if not row:  # the company-wide script that has never connected has no row yet
        row = Gateway(company_name=user.company_name, gateway_id=gid, state="never")
        master.add(row)
    row.key_version = (row.key_version or 0) + 1
    master.commit()
    svc.invalidate(user.company_name)
    audit.append(db, user.id, user.company_name, "gateway.reset_key", f"gateway:{gid}", client_ip(request))
    return MessageOut(message="Key reset. The old script stops working within about 20 seconds. Download the script again for this plant.")


class SetupCodeOut(BaseModel):
    code: str
    expires_in_minutes: int
    gateway_id: str
    gateway_name: str


@router.post("/{gid}/setup-code", response_model=SetupCodeOut)
def setup_code(gid: str, request: Request, user: User = Depends(admin_level),
               master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    """STGW4.0 gateway box: a one-time setup code for ONE gateway, to type on the box's setup page. Works once, expires, and a
    new code cancels the older one. The box swaps it for its connection details; the secret key is never typed or shown."""
    info = _fresh_info(user, master, gid)
    svc.mqtt_block()  # fail early with a clear message when the server is not set up
    if not settings.PUBLIC_API_URL:
        raise AppError(409, "PUBLIC_API_URL_MISSING", "The server address is not set. On the server put PUBLIC_API_URL=https://your-domain in .env.")
    code, minutes = enroll.make_code(user.company_name, gid)
    audit.append(db, user.id, user.company_name, "gateway.setup_code", f"gateway:{gid}", client_ip(request))
    return SetupCodeOut(code=code, expires_in_minutes=minutes, gateway_id=gid, gateway_name=info.name)


class EnrollIn(BaseModel):
    code: str = Field(min_length=6, max_length=24)


@router.post("/enroll")
@limiter.limit("10/minute")
def enroll_box(request: Request, body: EnrollIn, master: Session = Depends(get_master_db)):
    """A gateway box swaps its setup code for its connection details (the contents of link.json). No login: the code is the proof.
    One use only; wrong / used / expired codes all get the same answer."""
    found = enroll.redeem(body.code)
    if found is None:
        raise AppError(404, "CODE_INVALID", "This setup code is wrong, already used or expired. Make a new one in the app (Gateway page, Setup code).")
    company, gid = found
    svc.invalidate(company)  # always the newest key
    link = svc.build_link(company, gid)
    try:
        db = open_tenant_session(master, company)
    except AppError:
        db = None
    if db is not None:
        try:
            audit.append(db, None, company, "gateway.box_enrolled", f"gateway:{gid}", client_ip(request))
        finally:
            db.close()
    return Response(content=json.dumps(link), media_type="application/json", headers={"Cache-Control": "no-store"})


@router.post("/download-link")
def download_link(request: Request, gateway_id: str = Query(svc.MAIN, max_length=32), platform: str = Query("windows", pattern="^(windows|linux|macos)$"), user: User = Depends(admin_level),
                  master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    """Short-lived link (10 min) for the pre-linked script of ONE gateway. The app opens it in the browser."""
    _fresh_info(user, master, gateway_id)
    svc.mqtt_block()  # fail early with a clear message when the server is not set up
    if not settings.PUBLIC_API_URL:
        raise AppError(409, "PUBLIC_API_URL_MISSING", "The server address is not set. On the server put PUBLIC_API_URL=https://your-domain in .env.")
    if platform != "windows" and svc.program_for(platform) is None:
        raise AppError(409, "PROGRAM_NOT_AVAILABLE", f"The {platform} gateway program is not in this server build yet. Choose Windows, or update the server first.")
    audit.append(db, user.id, user.company_name, "gateway.download", f"gateway:{gateway_id} {platform}", client_ip(request))
    return {"url": f"{settings.PUBLIC_API_URL.rstrip('/')}{settings.API_PREFIX}/gateway/download?token={svc.download_token(user.company_name, gateway_id, platform)}"}


@router.get("/download")
def download(token: str):
    company, gid = svc.parse_token(token)
    platform = svc.token_platform(token)
    svc.invalidate(company)  # always the newest key
    filename = "STECH_Gateway.zip" if gid == svc.MAIN else f"STECH_Gateway_{gid}.zip"
    return Response(svc.build_zip(company, gid, platform), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


class AutoUpdateIn(BaseModel):
    enabled: bool


@router.post("/{gid}/auto-update", response_model=MessageOut)
def set_auto_update(gid: str, body: AutoUpdateIn, request: Request, user: User = Depends(admin_level),
                    master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    """Switch the signed automatic update of ONE plant script on or off."""
    _fresh_info(user, master, gid)
    row = master.scalar(select(Gateway).where(Gateway.company_name == user.company_name, Gateway.gateway_id == gid))
    if not row:
        row = Gateway(company_name=user.company_name, gateway_id=gid, state="never")
        master.add(row)
    row.auto_update = body.enabled
    master.commit()
    audit.append(db, user.id, user.company_name, "gateway.auto_update_on" if body.enabled else "gateway.auto_update_off", f"gateway:{gid}", client_ip(request))
    return MessageOut(message="Automatic updates are on." if body.enabled else "Automatic updates are off. This plant PC keeps its current script version.")


def _authenticate(authorization: str | None) -> tuple[str, str, svc.GwInfo]:
    bad = AppError(401, "GATEWAY_AUTH", "Gateway credentials rejected. Download the script again from the app.")
    if not authorization or not authorization.startswith("Gateway "):
        raise bad
    bits = authorization[len("Gateway "):].split(":")
    if len(bits) == 2:
        company, gid, secret = bits[0], svc.MAIN, bits[1]
    elif len(bits) == 3:
        company, gid, secret = bits
    else:
        raise bad
    if not svc.GID_RE.match(gid):
        raise bad
    info = svc.get_info(company, gid)
    if not info or not hmac.compare_digest(info.secret, secret):
        raise bad
    return company, gid, info


def _note_old_key(master: Session, company: str, gid: str, request: Request) -> None:
    """A cancelled (or wrong) key is still being used against a real gateway: one audit entry per gateway per hour, so the administrator
    can see that something still holds the old key. Never raises: a gateway asking for its settings must not fail because of this note."""
    try:
        db = open_tenant_session(master, company)             # fails for a company that does not exist: nothing is written for made-up names
        try:
            if enroll._r().set(f"stgw:oldkey:{company}:{gid}", "1", nx=True, ex=3600):
                audit.append(db, None, company, "gateway.old_key_used", f"gateway:{gid}", client_ip(request))
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass


UPDATE_EVENTS = {"updated": "gateway.script_updated", "rolled_back": "gateway.script_update_rolled_back",
                 "program_updated": "gateway.program_updated", "program_rolled_back": "gateway.program_update_rolled_back"}


@router.get("/update")
@limiter.limit("30/minute")
def update(request: Request, current: str = Query("0", max_length=20), launcher: str = Query("", max_length=10), platform: str = Query("", max_length=10), event: str | None = Query(default=None, max_length=20),
           from_: str = Query("", alias="from", max_length=20), to: str = Query("", max_length=20), reason: str = Query("", max_length=80),
           authorization: str | None = Header(default=None), master: Session = Depends(get_master_db)):
    """A plant script asks here whether a newer SIGNED version exists. The reply carries the script, its SHA-256 and the
    Ed25519 signature made in the build pipeline; the script verifies all three itself. Same Gateway key as /devices."""
    company, gid, _ = _authenticate(authorization)
    row = master.scalar(select(Gateway).where(Gateway.company_name == company, Gateway.gateway_id == gid))
    auto = True if row is None else bool(row.auto_update)
    out: dict = {"auto": auto, "update": False, "reported": False}
    if event in UPDATE_EVENTS:  # the script reports an update it installed (or undid) so it appears in the audit log
        try:
            db = open_tenant_session(master, company)
        except AppError:
            db = None
        if db is not None:
            try:
                audit.append(db, None, company, UPDATE_EVENTS[event], f"gateway:{gid} {from_}->{to}" + (f" ({reason})" if reason else ""), client_ip(request))
                out["reported"] = True
            finally:
                db.close()
    rel = selfupdate.release()
    out["latest"] = rel["version"] if rel else None
    if rel and auto and selfupdate.vtuple(rel["version"]) > selfupdate.vtuple(current):
        out.update(update=True, **selfupdate.encode(rel))
    prog = selfupdate.program_release(platform) if (auto and launcher and platform) else None   # the ready-made program itself
    if prog and selfupdate.vtuple(prog["version"]) > selfupdate.vtuple(launcher):
        out["program"] = {k: prog[k] for k in ("version", "sha256", "signature", "size")}
    return out


@router.get("/update/program")
@limiter.limit("6/hour")
def update_program(request: Request, platform: str = Query(..., pattern="^(windows|linux|macos)$"), authorization: str | None = Header(default=None)):
    """The signed ready-made program for this system (same Gateway key). The plant script checks its SHA-256 and signature before using it."""
    _authenticate(authorization)
    prog = selfupdate.program_release(platform)
    if not prog:
        raise AppError(404, "PROGRAM_NOT_AVAILABLE", "No signed program for this system in this server build.")
    return FileResponse(prog["path"], media_type="application/octet-stream", headers={"Cache-Control": "no-store"})


@router.get("/devices")
@limiter.limit("300/minute")
def devices(request: Request, authorization: str | None = Header(default=None), master: Session = Depends(get_master_db)):
    """A plant script gets (and re-checks) ITS device list from here.
    Authorization: Gateway <company>:<gateway>:<key>   (older scripts: Gateway <company>:<key> = the 'main' gateway)."""
    bad = AppError(401, "GATEWAY_AUTH", "Gateway credentials rejected. Download the script again from the app.")
    if not authorization or not authorization.startswith("Gateway "):
        raise bad
    bits = authorization[len("Gateway "):].split(":")
    if len(bits) == 2:
        company, gid, secret = bits[0], svc.MAIN, bits[1]
    elif len(bits) == 3:
        company, gid, secret = bits
    else:
        raise bad
    if not svc.GID_RE.match(gid):
        raise bad
    info = svc.get_info(company, gid)
    if not info:
        raise bad
    if not hmac.compare_digest(info.secret, secret):
        _note_old_key(master, company, gid, request)          # a REAL gateway of a REAL company, but the key is not the current one
        raise bad
    try:
        db = open_tenant_session(master, company)
    except AppError:
        raise bad
    try:
        return {"company": company, "gateway_id": gid, "name": info.name, "server_time": utcnow().isoformat(),
                "modbus": {"unit_id": settings.MODBUS_UNIT_ID, "word_order": settings.MODBUS_WORD_ORDER},
                "poll_seconds": csettings.get(company, "plant_poll_seconds"),
                "devices": svc.device_specs(db, info)}
    finally:
        db.close()
