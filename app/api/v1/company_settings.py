"""Company settings screen: every adjustable value of the plant (limits, timings, write lock) with its default."""
import json

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.timeutil import utcnow
from app.db.session import get_master_db
from app.deps import client_ip, get_company_db, get_current_user, require_roles
from app.models.master import CompanySettings, User
from app.services import audit, csettings, live

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingOut(BaseModel):
    key: str
    type: str
    label: str
    unit: str
    help: str
    default: bool | int | float | str
    value: bool | int | float | str
    changed: bool
    min: float | None = None
    max: float | None = None
    choices: list[str] | None = None


class SettingsIn(BaseModel):
    values: dict[str, bool | int | float | str | None]   # null = back to the default


@router.get("", response_model=list[SettingOut])
def list_settings(user: User = Depends(get_current_user)):
    return csettings.as_list(user.company_name)


@router.put("", response_model=list[SettingOut])
def change_settings(body: SettingsIn, request: Request, user: User = Depends(require_roles("admin")),
                    master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    company = user.company_name
    row = master.scalar(select(CompanySettings).where(CompanySettings.company_name == company))
    current = json.loads(row.values) if row and row.values else {}
    new, changes = dict(current), []
    for key, raw in body.values.items():
        if key not in csettings.REGISTRY:
            raise AppError(422, "UNKNOWN_SETTING", f"There is no setting called '{key}'.")
        if raw is None:
            new.pop(key, None)
        else:
            new[key] = csettings.clean(key, raw)
        if new.get(key) != current.get(key):
            changes.append(f"{key}={new.get(key, 'default')}")
    off, stale = new.get("gateway_offline_seconds", csettings.REGISTRY["gateway_offline_seconds"][1]), new.get("device_stale_seconds", csettings.REGISTRY["device_stale_seconds"][1])
    if stale >= off:
        raise AppError(422, "STALE_NOT_BELOW_OFFLINE", "The grey 'stale' time must be shorter than the 'Offline' time.")
    shelf_ok = new.get("alarms_per_hour_target", csettings.REGISTRY["alarms_per_hour_target"][1]) <= new.get("alarms_per_hour_limit", csettings.REGISTRY["alarms_per_hour_limit"][1])
    if not shelf_ok:
        raise AppError(422, "TARGET_ABOVE_LIMIT", "The alarms-per-hour target cannot be higher than the action limit.")
    if changes:
        if row is None:
            row = CompanySettings(company_name=company, values="{}")
            master.add(row)
        row.values, row.updated_by, row.updated_at = json.dumps(new), user.username, utcnow()
        master.commit()
        csettings.invalidate(company)
        audit.append(db, user.id, company, "settings.change", "; ".join(changes)[:240], client_ip(request))
        live.publish(company, {"type": "settings", "state": "ALL", "region": "ALL"})
    return csettings.as_list(company)
