"""V2.0 - the customer's own gateway (any brand): add it, hand over the login, choose which of its points the app shows.

Super admin: every gateway of the company.  Regional admin: only the gateways of their own State + Region.
Operators / Guests do not manage gateways; they simply see the points the administrators chose, for their own State + Region.

  POST /plants                       add a gateway for a State + Region: the cloud makes its broker login and shows the details ONCE
  GET  /plants                       the list (online?, how many new points are waiting in the Detected list)
  GET  /plants/{id}/connection       the same details WITHOUT the password
  POST /plants/{id}/reset-password   a new password (the old one stops working at once), shown once
  POST /plants/{id}/enable           switch the gateway on / off (off also disconnects it)
  PATCH /plants/{id}                 rename, choose how write commands are sent (auto | json | sparkplug)
  DELETE /plants/{id}                remove the gateway and its broker login (its devices and history stay)
  GET  /plants/{id}/detected         the Detected list;   POST .../detected/accept | dismiss | restore
"""
import secrets
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import geo, normalize
from app.core.config import settings
from app.core.constants import REGIONS
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.timeutil import utcnow
from app.db.session import get_master_db
from app.deps import ADMIN_ROLES, client_ip, get_company_db, require_roles
from app.models.master import PlantLink, User
from app.models.tenant import DetectedTag, Device
from app.services import audit, brokeradmin, extgw, mail

log = get_logger("plants")
router = APIRouter(prefix="/plants", tags=["plants"])
admin_level = require_roles(*ADMIN_ROLES)
MAX_PER_REGION = 20


# ------------------------------------------------------------------------------------------------ helpers
def public_host() -> str:
    return (settings.MQTT_TLS_HOST or settings.MQTT_PUBLIC_HOST or (urlparse(settings.PUBLIC_API_URL).hostname or "")).strip()


def connection(link: PlantLink, password: str | None = None) -> dict:
    """What the customer types into the gateway's own web page. The password is included only when it was just made."""
    pid = link.plant_id
    out = {
        "broker_address": public_host(), "port": settings.MQTT_TLS_PORT, "tls": True, "client_id": pid, "username": pid,
        "password": password, "keep_alive_seconds": 60, "clean_session": True,
        "json": {"topic_prefix": f"iview/{pid}", "example_topic": f"iview/{pid}/Boiler1", "example_payload": '{"Steam_Temp": 182.4, "Pressure": 9.1, "Pump_Run": true}',
                 "command_topic_filter": f"iview/{pid}/cmd/#", "status_topic": f"iview/{pid}/status"},
        "sparkplug_b": {"group_id": pid, "edge_node_id": "(your choice, e.g. Gateway1)", "will_topic": f"spBv1.0/{pid}/NDEATH/<edge node id>",
                        "command_topics": f"spBv1.0/{pid}/DCMD/# and spBv1.0/{pid}/NCMD/#"},
    }
    if not out["broker_address"]:
        out["warning"] = "The cloud address is not set on the server (MQTT_PUBLIC_HOST in .env). Ask the person who installed the server."
    return out


def _password() -> str:
    return secrets.token_urlsafe(18)


def _row(master: Session, user: User, plant_id: str) -> PlantLink:
    row = master.scalar(select(PlantLink).where(PlantLink.plant_id == plant_id, PlantLink.company_name == user.company_name))
    if not row:
        raise AppError(404, "GATEWAY_NOT_FOUND", "This gateway was not found.")
    if user.role != "admin" and (row.site_state != user.state or row.site_region != user.region):
        raise AppError(403, "OUTSIDE_YOUR_REGION", "This gateway belongs to another State / Region. Ask the Super admin.")
    return row


def _item(row: PlantLink, new_tags: int = 0, devices: int = 0) -> dict:
    return {"plant_id": row.plant_id, "name": row.name, "state": row.site_state, "region": row.site_region, "enabled": row.enabled,
            "online": extgw.is_online(row), "state_text": "disabled" if not row.enabled else "online" if extgw.is_online(row) else "never" if row.state == "never" else "offline",
            "last_seen": row.last_seen.isoformat() if row.last_seen else None, "format": row.last_format, "cmd_format": row.cmd_format,
            "created_at": row.created_at.isoformat() if row.created_at else None, "detected_new": new_tags, "devices": devices}


def _email(to: str, link: PlantLink, details: dict, by: str) -> bool:
    """The plant person gets the details once, by e-mail (when the server's mail is set up). Returns True when the mail was handed over."""
    if not mail.configured():
        return False
    j, s = details["json"], details["sparkplug_b"]
    html = (f"<h3>STECH iVIEW - connect your gateway</h3><p>{by} added your plant <b>{link.name}</b>. Type these values once into the web page of "
            "your gateway (MQTT client / MQTT publisher settings):</p>"
            f"<table cellpadding=4><tr><td>Broker address</td><td><b>{details['broker_address']}</b></td></tr>"
            f"<tr><td>Port</td><td><b>{details['port']}</b> (TLS / SSL on)</td></tr>"
            f"<tr><td>Client ID</td><td><b style='font-family:monospace'>{details['client_id']}</b></td></tr>"
            f"<tr><td>User name</td><td><b style='font-family:monospace'>{details['username']}</b></td></tr>"
            f"<tr><td>Password</td><td><b style='font-family:monospace'>{details['password']}</b></td></tr>"
            f"<tr><td>JSON topic prefix</td><td><b style='font-family:monospace'>{j['topic_prefix']}</b></td></tr>"
            f"<tr><td>Sparkplug B group id</td><td><b style='font-family:monospace'>{s['group_id']}</b></td></tr></table>"
            "<p>This is the only time the password is sent. If it is lost, ask your administrator for a new one.</p>"
            "<p>Writing to the plant: follow the Gateway Integration Manual (subscribe to the command topic and map it to the PLC address).</p>")
    try:
        mail.send_now(to, "STECH iVIEW - your gateway login details", html)
        return True
    except Exception as exc:
        log.error("plant_details_email_failed", error=type(exc).__name__)
        return False


# ------------------------------------------------------------------------------------------------ schemas
class PlantIn(BaseModel):
    state: str
    region: str
    name: str | None = Field(default=None, max_length=80)
    email: EmailStr | None = Field(default=None, description="Send the connection details once to this address (the plant person).")

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


class PlantPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    cmd_format: str | None = Field(default=None, pattern="^(auto|json|sparkplug)$")


class EnableIn(BaseModel):
    enabled: bool


class ResetIn(BaseModel):
    email: EmailStr | None = None


class AcceptItem(BaseModel):
    id: int
    name: str | None = Field(default=None, max_length=64, description="The name people see in the app (default: the gateway's own tag name).")
    unit: str | None = Field(default=None, max_length=16)
    device_name: str | None = Field(default=None, max_length=120, description="The device the point belongs to in the app (default: the gateway's own device name).")
    sensor_type: str | None = Field(default=None, max_length=32)


class AcceptIn(BaseModel):
    items: list[AcceptItem] = Field(min_length=1, max_length=500)


class IdsIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=2000)


# ------------------------------------------------------------------------------------------------ endpoints
@router.get("/health")
def health(user: User = Depends(admin_level)):
    """Super admin / Regional admin: is the server ready for customer gateways? (the broker administrator works; the TLS door answers)."""
    import socket
    import ssl
    out = {"broker_address": public_host(), "port": settings.MQTT_TLS_PORT, "broker_admin_ok": False, "tls_door_answers": False, "problems": []}
    try:
        brokeradmin._check(brokeradmin._run([{"command": "listClients", "count": 1}]))
        out["broker_admin_ok"] = True
    except AppError as exc:
        out["problems"].append(exc.message)
    host = out["broker_address"]
    if not host:
        out["problems"].append("MQTT_TLS_HOST / MQTT_PUBLIC_HOST is empty in .env: customers would not know which address to type.")
    else:
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, settings.MQTT_TLS_PORT), 4) as raw, ctx.wrap_socket(raw, server_hostname=host):
                out["tls_door_answers"] = True
        except (OSError, ssl.SSLError) as exc:
            out["problems"].append(f"Port {settings.MQTT_TLS_PORT} on {host} did not complete a TLS connection from the server itself ({type(exc).__name__}). "
                                   "Is the port open in the firewall / network security group, and does the broker log say 'TLS door 8883: ON'?")
    out["ready"] = out["broker_admin_ok"] and out["tls_door_answers"]
    return out


@router.get("")
def list_plants(user: User = Depends(admin_level), master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    stmt = select(PlantLink).where(PlantLink.company_name == user.company_name).order_by(PlantLink.name)
    if user.role != "admin":
        stmt = stmt.where(PlantLink.site_state == user.state, PlantLink.site_region == user.region)
    rows = list(master.scalars(stmt))
    new = dict(db.execute(select(DetectedTag.plant_id, func.count(DetectedTag.id)).where(DetectedTag.dismissed.is_(False)).group_by(DetectedTag.plant_id)).all())
    devs: dict[str, int] = {}
    for d in db.scalars(select(Device).where(Device.protocol == "MQTT", Device.ip_address.like("plant:%"))):
        devs[d.ip_address[6:]] = devs.get(d.ip_address[6:], 0) + 1
    return {"plants": [_item(r, new.get(r.plant_id, 0), devs.get(r.plant_id, 0)) for r in rows],
            "broker_address": public_host(), "port": settings.MQTT_TLS_PORT}


@router.post("", status_code=201)
def add_plant(body: PlantIn, request: Request, user: User = Depends(admin_level), master: Session = Depends(get_master_db),
              db: Session = Depends(get_company_db)):
    if user.role != "admin" and (body.state != user.state or body.region != user.region):
        raise AppError(403, "OUTSIDE_YOUR_REGION", "You can add gateways only for your own State and Region.")
    taken = master.scalar(select(func.count(PlantLink.id)).where(PlantLink.company_name == user.company_name,
                                                                 PlantLink.site_state == body.state, PlantLink.site_region == body.region))
    if (taken or 0) >= MAX_PER_REGION:
        raise AppError(409, "TOO_MANY_GATEWAYS", f"A State + Region can have at most {MAX_PER_REGION} gateways.")
    pid = "pl" + secrets.token_hex(5)
    while master.scalar(select(PlantLink.id).where(PlantLink.plant_id == pid)):
        pid = "pl" + secrets.token_hex(5)
    pw = _password()
    brokeradmin.create_plant(pid, pw)            # first the broker login; if that fails nothing is saved
    name = (body.name or f"{body.state} {body.region}").strip()
    row = PlantLink(company_name=user.company_name, plant_id=pid, name=name, site_state=body.state, site_region=body.region,
                    created_by=user.username, password_changed_at=utcnow())
    master.add(row)
    master.commit()
    audit.append(db, user.id, user.company_name, "plant.create", f"plant:{pid} {body.state}/{body.region}", client_ip(request))
    details = connection(row, pw)
    emailed = _email(body.email, row, details, user.username) if body.email else False
    return {"plant": _item(row), "connection": details, "emailed": emailed,
            "note": "The password is shown only now. Give it to the person who sets up the gateway."}


@router.get("/{plant_id}/connection")
def get_connection(plant_id: str, user: User = Depends(admin_level), master: Session = Depends(get_master_db)):
    row = _row(master, user, plant_id)
    return {"plant": _item(row), "connection": connection(row)}


@router.post("/{plant_id}/reset-password")
def reset_password(plant_id: str, request: Request, body: ResetIn | None = None, user: User = Depends(admin_level),
                   master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    row = _row(master, user, plant_id)
    pw = _password()
    brokeradmin.set_password(plant_id, pw)
    row.password_changed_at = utcnow()
    master.commit()
    audit.append(db, user.id, user.company_name, "plant.reset_password", f"plant:{plant_id}", client_ip(request))
    details = connection(row, pw)
    emailed = _email(body.email, row, details, user.username) if body and body.email else False
    return {"plant": _item(row), "connection": details, "emailed": emailed,
            "note": "The old password no longer works. Type the new one into the gateway."}


@router.post("/{plant_id}/enable")
def enable(plant_id: str, body: EnableIn, request: Request, user: User = Depends(admin_level), master: Session = Depends(get_master_db),
           db: Session = Depends(get_company_db)):
    row = _row(master, user, plant_id)
    brokeradmin.set_enabled(plant_id, body.enabled)
    row.enabled = body.enabled
    if not body.enabled:
        row.state, row.changed_at = "offline" if row.state == "online" else row.state, utcnow()
    master.commit()
    extgw.forget_link(plant_id)
    audit.append(db, user.id, user.company_name, "plant.enable" if body.enabled else "plant.disable", f"plant:{plant_id}", client_ip(request))
    return {"plant": _item(row)}


@router.patch("/{plant_id}")
def patch(plant_id: str, body: PlantPatch, request: Request, user: User = Depends(admin_level), master: Session = Depends(get_master_db),
          db: Session = Depends(get_company_db)):
    row = _row(master, user, plant_id)
    if body.name is not None:
        row.name = body.name.strip()
    if body.cmd_format is not None:
        row.cmd_format = body.cmd_format
    master.commit()
    extgw.forget_link(plant_id)
    audit.append(db, user.id, user.company_name, "plant.update", f"plant:{plant_id}", client_ip(request))
    return {"plant": _item(row)}


@router.delete("/{plant_id}")
def remove(plant_id: str, request: Request, user: User = Depends(admin_level), master: Session = Depends(get_master_db),
           db: Session = Depends(get_company_db)):
    row = _row(master, user, plant_id)
    brokeradmin.delete_plant(plant_id)
    db.query(DetectedTag).filter(DetectedTag.plant_id == plant_id).delete()
    master.delete(row)
    master.commit()
    db.commit()
    extgw.forget_link(plant_id)
    audit.append(db, user.id, user.company_name, "plant.delete", f"plant:{plant_id}", client_ip(request))
    return {"removed": plant_id}


@router.get("/{plant_id}/detected")
def detected(plant_id: str, show: str = Query("new", pattern="^(new|dismissed|all)$"), user: User = Depends(admin_level),
             master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    _row(master, user, plant_id)
    stmt = select(DetectedTag).where(DetectedTag.plant_id == plant_id).order_by(DetectedTag.ext_device, DetectedTag.tag)
    if show != "all":
        stmt = stmt.where(DetectedTag.dismissed.is_(show == "dismissed"))
    rows = list(db.scalars(stmt.limit(3000)))
    total = db.scalar(select(func.count(DetectedTag.id)).where(DetectedTag.plant_id == plant_id)) or 0
    return {"items": [{"id": r.id, "device": r.ext_device, "tag": r.tag, "sample": r.sample, "kind": r.kind, "count": r.count,
                       "first_seen": r.first_seen.isoformat(), "last_seen": r.last_seen.isoformat(), "dismissed": r.dismissed} for r in rows],
            "limit_reached": total >= extgw.MAX_DETECTED_PER_PLANT}


@router.post("/{plant_id}/detected/accept")
def accept(plant_id: str, body: AcceptIn, request: Request, user: User = Depends(admin_level), master: Session = Depends(get_master_db),
           db: Session = Depends(get_company_db)):
    row = _row(master, user, plant_id)
    result = extgw.accept(db, row, [i.model_dump() for i in body.items], user.username)
    audit.append(db, user.id, user.company_name, "plant.accept_tags", f"plant:{plant_id} n={result['sensors_added']}", client_ip(request))
    return result


@router.post("/{plant_id}/detected/dismiss")
def dismiss(plant_id: str, body: IdsIn, user: User = Depends(admin_level), master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    _row(master, user, plant_id)
    n = db.query(DetectedTag).filter(DetectedTag.plant_id == plant_id, DetectedTag.id.in_(body.ids)).update({"dismissed": True}, synchronize_session=False)
    db.commit()
    return {"dismissed": n}


@router.post("/{plant_id}/detected/restore")
def restore(plant_id: str, body: IdsIn, user: User = Depends(admin_level), master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    _row(master, user, plant_id)
    n = db.query(DetectedTag).filter(DetectedTag.plant_id == plant_id, DetectedTag.id.in_(body.ids)).update({"dismissed": False}, synchronize_session=False)
    db.commit()
    return {"restored": n}
