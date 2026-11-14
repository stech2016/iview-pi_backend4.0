"""On-call roster: who is told first, who next if nobody acknowledges."""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core import geo, normalize
from app.core.constants import REGIONS
from app.core.errors import AppError
from app.db.session import get_master_db
from app.deps import ADMIN_ROLES, client_ip, get_company_db, require_roles
from app.models.master import OnCallStep, User
from app.services import audit

router = APIRouter(prefix="/oncall", tags=["oncall"])
any_admin = require_roles(*ADMIN_ROLES)


class StepIn(BaseModel):
    user_id: int
    wait_minutes: int = Field(default=10, ge=1, le=240)   # how long this person has before the next one is told


class RosterIn(BaseModel):
    state: str | None = None
    region: str | None = None
    steps: list[StepIn] = Field(max_length=6)


def _scope(user: User, state: str | None, region: str | None) -> tuple[str, str]:
    if user.role == "regional_admin":
        return user.state, user.region
    state, region = normalize.state(state), normalize.region(region)
    if not geo.is_state(state) or region not in REGIONS:
        raise AppError(422, "REGION_INVALID", "Choose a State and Region for this roster.")
    return state, region


@router.get("")
def get_roster(state: str | None = None, region: str | None = None, user: User = Depends(any_admin), master: Session = Depends(get_master_db)):
    state, region = _scope(user, state, region)
    rows = master.execute(select(OnCallStep, User.username).join(User, User.id == OnCallStep.user_id).where(
        OnCallStep.company_name == user.company_name, OnCallStep.state == state, OnCallStep.region == region).order_by(OnCallStep.step_no)).all()
    return {"state": state, "region": region,
            "steps": [{"step": s.step_no, "user_id": s.user_id, "username": name, "wait_minutes": s.wait_minutes} for s, name in rows]}


@router.put("")
def set_roster(body: RosterIn, request: Request, user: User = Depends(any_admin),
               master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    state, region = _scope(user, body.state, body.region)
    ids = [s.user_id for s in body.steps]
    if len(set(ids)) != len(ids):
        raise AppError(422, "ROSTER_DUPLICATE", "Each person can appear once in the roster.")
    found = set(master.scalars(select(User.id).where(User.id.in_(ids), User.company_name == user.company_name, User.status == "approved")))
    if found != set(ids):
        raise AppError(422, "ROSTER_USER_INVALID", "Every person in the roster must be an approved user of your company.")
    master.execute(delete(OnCallStep).where(OnCallStep.company_name == user.company_name, OnCallStep.state == state, OnCallStep.region == region))
    for n, s in enumerate(body.steps, 1):
        master.add(OnCallStep(company_name=user.company_name, state=state, region=region, step_no=n, user_id=s.user_id, wait_minutes=s.wait_minutes))
    master.commit()
    audit.append(db, user.id, user.company_name, "oncall.set", f"{state}/{region}:{len(ids)} steps", client_ip(request))
    return {"state": state, "region": region, "steps": len(ids)}
