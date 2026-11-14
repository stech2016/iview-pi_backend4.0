"""Saved screens kept as DATA (not code):
  process  the animation rules of a device's live process view. Default = app/data/animations.json (per template); an administrator may
           override it per device ("Edit animation") and go back with "Reset to template".
  hmi      drag-and-drop HMI screens for the company. Objects that WRITE use the normal signed, limited, confirmed control path and need
           Control write permission - this file only stores the layout.
FUTURE (outbound connectors / webhooks / MES / ERP): a 'screen.saved' webhook can be fired from save_hmi() / save_process()."""
import json
import os

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.timeutil import utcnow
from app.deps import client_ip, get_company_db, get_scoped_device, require_roles, ADMIN_ROLES
from app.models.master import User
from app.models.tenant import DeviceRegister, Screen
from app.services import audit, perms

router = APIRouter(prefix="/screens", tags=["screens"])
MAX_BYTES = 200_000
MAX_OBJECTS = 300
OBJECT_TYPES = {"value", "lamp", "button", "switch", "slider", "gauge", "tank", "pump", "motor", "fan", "valve", "pipe", "flame", "thermometer",
                "counter", "trend", "alarm_list", "text", "image"}
_DEFAULTS: dict | None = None


def defaults() -> dict:
    global _DEFAULTS
    if _DEFAULTS is None:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "animations.json"), encoding="utf-8") as f:
            _DEFAULTS = json.load(f)
    return _DEFAULTS


class ConfigIn(BaseModel):
    name: str = Field(default="Main", min_length=1, max_length=80)
    config: dict


def _validate(config: dict) -> str:
    objs = config.get("objects")
    if not isinstance(objs, list) or len(objs) > MAX_OBJECTS:
        raise AppError(422, "SCREEN_OBJECTS", f"A screen needs an 'objects' list of at most {MAX_OBJECTS} items.")
    for o in objs:
        if not isinstance(o, dict) or o.get("type") not in OBJECT_TYPES:
            raise AppError(422, "SCREEN_OBJECT_TYPE", "An object has an unknown type.")
    text = json.dumps(config, separators=(",", ":"))
    if len(text) > MAX_BYTES:
        raise AppError(413, "SCREEN_TOO_BIG", "This screen is too large.")
    return text


def guess_template(metrics: set[str]) -> str | None:
    """The industry template whose sensor names match this device best (a device made from a template keeps its sensor names). None if nothing matches."""
    from app.api.v1.setup import templates
    best, score = None, 0
    for key, t in templates().items():
        hit = len(metrics & {s["metric_name"] for s in t["sensors"]})
        if hit > score:
            best, score = key, hit
    return best


def _generic(regs: list) -> list:
    """A device that is not from a template: one value tile per sensor, in a grid (a gauge for numbers, a lamp for on / off points)."""
    out = []
    for i, r in enumerate(regs[:12]):
        bit = r.data_type == "bool" or r.register_type == "coil"
        out.append({"type": "lamp" if bit else "gauge", "bind": {"metric": r.metric_name}, "label": r.metric_name,
                    "x": 4 + (i % 4) * 24, "y": 4 + (i // 4) * 30})
    return out


def _scene(db: Session, device_id: int, template: str | None) -> list:
    d = defaults()
    regs = list(db.scalars(select(DeviceRegister).where(DeviceRegister.device_id == device_id)))
    key = template if template in d else guess_template({r.metric_name for r in regs})
    return d[key] if key in d else _generic(regs)


# ---------------------------------------------------------------- process view (animation rules)
@router.get("/process/{device_id}")
def get_process(device_id: int, template: str | None = Query(None, max_length=40), user: User = Depends(perms.require("monitor", 1)),
                db: Session = Depends(get_company_db)):
    get_scoped_device(db, user, device_id)
    row = db.scalar(select(Screen).where(Screen.kind == "process", Screen.device_id == device_id))
    if row:
        return {"source": "device", "config": json.loads(row.config), "updated_by": row.updated_by}
    return {"source": "template", "config": {"objects": _scene(db, device_id, template)}, "updated_by": None}


@router.put("/process/{device_id}")
def save_process(device_id: int, body: ConfigIn, request: Request, user: User = Depends(perms.require("config", 2)), db: Session = Depends(get_company_db)):
    get_scoped_device(db, user, device_id)
    text = _validate(body.config)
    row = db.scalar(select(Screen).where(Screen.kind == "process", Screen.device_id == device_id))
    if not row:
        row = Screen(kind="process", device_id=device_id, name="process")
        db.add(row)
    row.config, row.updated_by, row.updated_at = text, user.username, utcnow()
    db.commit()
    audit.append(db, user.id, user.company_name, "screen.process.save", f"device:{device_id}", client_ip(request))
    return {"source": "device", "config": body.config, "updated_by": user.username}


@router.delete("/process/{device_id}")
def reset_process(device_id: int, request: Request, user: User = Depends(perms.require("config", 2)), db: Session = Depends(get_company_db)):
    get_scoped_device(db, user, device_id)
    row = db.scalar(select(Screen).where(Screen.kind == "process", Screen.device_id == device_id))
    if row:
        db.delete(row)
        db.commit()
    audit.append(db, user.id, user.company_name, "screen.process.reset", f"device:{device_id}", client_ip(request))
    return {"reset": True}


# ---------------------------------------------------------------- HMI screens
def _hmi(r: Screen) -> dict:
    return {"id": r.id, "name": r.name, "config": json.loads(r.config), "updated_by": r.updated_by, "updated_at": r.updated_at.isoformat()}


@router.get("/hmi")
def list_hmi(user: User = Depends(perms.require("monitor", 1)), db: Session = Depends(get_company_db)):
    return [_hmi(r) for r in db.scalars(select(Screen).where(Screen.kind == "hmi").order_by(Screen.name))]


@router.post("/hmi", status_code=201)
def create_hmi(body: ConfigIn, request: Request, user: User = Depends(require_roles(*ADMIN_ROLES)), db: Session = Depends(get_company_db)):
    text = _validate(body.config)
    if db.scalar(select(Screen.id).where(Screen.kind == "hmi", Screen.name == body.name)):
        raise AppError(409, "SCREEN_EXISTS", "A screen with this name already exists.")
    r = Screen(kind="hmi", name=body.name, config=text, updated_by=user.username)
    db.add(r)
    db.commit()
    audit.append(db, user.id, user.company_name, "screen.hmi.create", body.name, client_ip(request))
    return _hmi(r)


@router.put("/hmi/{screen_id}")
def save_hmi(screen_id: int, body: ConfigIn, request: Request, user: User = Depends(require_roles(*ADMIN_ROLES)), db: Session = Depends(get_company_db)):
    r = db.get(Screen, screen_id)
    if not r or r.kind != "hmi":
        raise AppError(404, "SCREEN_NOT_FOUND", "Screen not found.")
    r.name, r.config, r.updated_by, r.updated_at = body.name, _validate(body.config), user.username, utcnow()
    db.commit()
    audit.append(db, user.id, user.company_name, "screen.hmi.save", r.name, client_ip(request))
    return _hmi(r)


@router.delete("/hmi/{screen_id}")
def delete_hmi(screen_id: int, request: Request, user: User = Depends(require_roles(*ADMIN_ROLES)), db: Session = Depends(get_company_db)):
    r = db.get(Screen, screen_id)
    if not r or r.kind != "hmi":
        raise AppError(404, "SCREEN_NOT_FOUND", "Screen not found.")
    db.delete(r)
    db.commit()
    audit.append(db, user.id, user.company_name, "screen.hmi.delete", r.name, client_ip(request))
    return {"removed": True}
