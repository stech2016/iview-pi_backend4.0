"""Alert rules per device: "metric  >  threshold  for N seconds  ->  severity". Administrators only."""
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.deps import ADMIN_ROLES, client_ip, get_company_db, get_current_user, get_scoped_device, require_roles
from app.models.master import User
from app.models.tenant import AlertRule, DeviceRegister
from app.services import audit

router = APIRouter(prefix="/alert-rules", tags=["alerts"])


class RuleIn(BaseModel):
    """kind: level (threshold), deadband (threshold + margin), rate (change within window_seconds), no_data (silence for window_seconds)."""
    device_id: int
    metric_name: str = Field(min_length=1, max_length=64)
    kind: Literal["level", "rate", "deadband", "no_data"] = "level"
    op: Literal[">", "<", ">=", "<="] = ">"
    threshold: float = 0
    deadband: float = Field(default=0, ge=0)
    window_seconds: int = Field(default=60, ge=1, le=86400)
    for_seconds: int = Field(default=0, ge=0, le=86400)
    severity: Literal["critical", "major", "minor", "info"] = "major"
    message: str | None = Field(default=None, max_length=200)
    enabled: bool = True


def _check(body: RuleIn) -> None:
    if body.kind == "deadband" and body.deadband <= 0:
        raise AppError(422, "RULE_DEADBAND", "A deadband rule needs a margin greater than 0.")


def _out(r: AlertRule) -> dict:
    return {"id": r.id, "device_id": r.device_id, "metric_name": r.metric_name, "op": r.op, "threshold": r.threshold,
            "kind": r.kind, "deadband": r.deadband, "window_seconds": r.window_seconds, "for_seconds": r.for_seconds, "severity": r.severity, "message": r.message, "enabled": r.enabled}


@router.get("")
def list_rules(device_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_company_db)):
    get_scoped_device(db, user, device_id)
    return [_out(r) for r in db.scalars(select(AlertRule).where(AlertRule.device_id == device_id).order_by(AlertRule.id))]


@router.post("", status_code=201)
def create_rule(body: RuleIn, request: Request, user: User = Depends(require_roles(*ADMIN_ROLES)), db: Session = Depends(get_company_db)):
    _check(body)
    device = get_scoped_device(db, user, body.device_id)
    if not db.scalar(select(DeviceRegister.id).where(DeviceRegister.device_id == device.id, DeviceRegister.metric_name == body.metric_name)):
        raise AppError(422, "RULE_METRIC_UNKNOWN", "This device has no sensor with that name.")
    r = AlertRule(**body.model_dump(), created_by=user.username)
    db.add(r)
    db.commit()
    audit.append(db, user.id, user.company_name, "alert_rule.create", f"rule:{r.id}", client_ip(request))
    return _out(r)


@router.put("/{rule_id}")
def update_rule(rule_id: int, body: RuleIn, request: Request, user: User = Depends(require_roles(*ADMIN_ROLES)), db: Session = Depends(get_company_db)):
    _check(body)
    r = db.get(AlertRule, rule_id)
    if not r:
        raise AppError(404, "RULE_NOT_FOUND", "Rule not found.")
    get_scoped_device(db, user, r.device_id)
    for k, v in body.model_dump(exclude={"device_id"}).items():
        setattr(r, k, v)
    db.commit()
    audit.append(db, user.id, user.company_name, "alert_rule.update", f"rule:{r.id}", client_ip(request))
    return _out(r)


@router.delete("/{rule_id}")
def delete_rule(rule_id: int, request: Request, user: User = Depends(require_roles(*ADMIN_ROLES)), db: Session = Depends(get_company_db)):
    r = db.get(AlertRule, rule_id)
    if not r:
        raise AppError(404, "RULE_NOT_FOUND", "Rule not found.")
    get_scoped_device(db, user, r.device_id)
    db.delete(r)
    db.commit()
    audit.append(db, user.id, user.company_name, "alert_rule.delete", f"rule:{rule_id}", client_ip(request))
    return {"removed": True}
