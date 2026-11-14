"""People of the company. The Super admin creates everybody; a person with 'Manage users' (Users = Read and Write) can create
people inside his own Country / State / Region, never above his own rights. The new person gets a one-time password and must
choose his own at the first login."""
import secrets
import string

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import geo, normalize
from app.core.constants import REGIONS
from app.core.errors import AppError
from app.core.security import hash_password
from app.db.session import get_master_db
from app.deps import can_manage_user, client_ip, get_company_db
from app.models.master import User
from app.schemas.auth import MessageOut, USERNAME_RE, UserOut, validate_password_strength
from app.services import audit, mail, perms
from app.services.perms import AREAS, LEVEL_NAME, TITLES

router = APIRouter(prefix="/users", tags=["users"])

AREA_TEXT = {
    "monitor": ("Monitor", "Dashboard, devices, trends, plant map. Write: also maintenance mode and shift hand-over notes."),
    "alarms": ("Alarms", "See alarms. Write: acknowledge, comment, shelve."),
    "config": ("Configuration", "Read: see the setup and ask for a change. Write: change devices and sensors directly."),
    "control": ("Control", "Read: see the command history. Write: send values and commands to the plant."),
    "reports": ("Reports and sharing", "View and export reports. Write: also share read-only links and QR labels."),
    "users": ("Manage users", "See the people of the own area. Write: create, change and switch off people of the own area."),
}


def temp_password() -> str:
    """A strong one-time password that passes the normal password rules."""
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(12)) + secrets.choice("@#$%!") + secrets.choice(string.digits)
        try:
            return validate_password_strength(pw)
        except ValueError:
            continue


class CreateUserIn(BaseModel):
    username: str
    email: EmailStr
    job_title: str
    country: str = Field(default=geo.DEFAULT_COUNTRY, max_length=64)
    state: str = ""
    region: str = ""
    phone: str | None = Field(default=None, max_length=24)
    permissions: dict | None = None          # leave empty = what the job title pre-ticks
    send_email: bool = True

    @field_validator("username")
    @classmethod
    def _u(cls, v):
        if not USERNAME_RE.match(v):
            raise ValueError("Username must be 3-32 characters: letters, numbers, dot, dash or underscore")
        return v.lower()

    @field_validator("job_title")
    @classmethod
    def _t(cls, v):
        if v not in TITLES:
            raise ValueError("Job title must be one of: " + ", ".join(TITLES))
        return v

    @field_validator("phone")
    @classmethod
    def _p(cls, v):
        v = (v or "").strip()
        return v or None


class UpdateUserIn(BaseModel):
    job_title: str | None = None
    permissions: dict | None = None
    country: str | None = Field(default=None, max_length=64)
    state: str | None = None
    region: str | None = None
    phone: str | None = Field(default=None, max_length=24)

    @model_validator(mode="after")
    def _t(self):
        if self.job_title is not None and self.job_title not in TITLES:
            raise ValueError("Job title must be one of: " + ", ".join(TITLES))
        return self


class CreatedOut(BaseModel):
    user: UserOut
    temporary_password: str          # shown ONCE: it is not stored in a readable form
    email_sent: bool


def _check_place(country: str, state: str, region: str) -> tuple[str, str, str]:
    """Checks the place (any capital letters accepted) and returns it with the standard spelling."""
    country, state, region = normalize.place(country, state, region)
    if not geo.is_country(country):
        raise AppError(422, "BAD_COUNTRY", "Select a valid country.")
    if not state or not geo.is_state(state, country):
        raise AppError(422, "BAD_STATE", "Select a State / province that belongs to the country.")
    if region not in REGIONS:
        raise AppError(422, "BAD_REGION", "Region must be one of North, South, East, West.")
    return country, state, region


def _check_levels(actor: User, title: str, levels: dict[str, int]) -> None:
    """The Super admin may tick anything. Anybody else: only titles below Admin, never more than his own level, and never 'Manage users'."""
    if actor.role == "admin":
        return
    if title == "Admin":
        raise AppError(403, "ONLY_SUPER_ADMIN", "Only the Super admin can create an Admin.")
    mine = perms.effective(actor)
    for a in AREAS:
        if levels[a] > mine[a]:
            raise AppError(403, "ABOVE_YOUR_RIGHTS", f"You cannot give '{AREA_TEXT[a][0]}' = {LEVEL_NAME[levels[a]]}: your own level is {LEVEL_NAME[mine[a]]}.")
    if levels["users"] > 1:
        raise AppError(403, "ONLY_SUPER_ADMIN", "Only the Super admin can give 'Manage users' (Read and Write).")


def _scope_filter(stmt, actor: User):
    stmt = stmt.where(User.company_name == actor.company_name, User.id != actor.id)
    if actor.role != "admin":
        stmt = stmt.where(User.role.in_(("operator", "guest")), User.country == actor.country, User.state == actor.state, User.region == actor.region)
    return stmt


def _target(master: Session, actor: User, user_id: int) -> User:
    u = master.get(User, user_id)
    if not u or u.company_name != actor.company_name:
        raise AppError(404, "USER_NOT_FOUND", "User not found in your company.")
    if not can_manage_user(actor, u):
        raise AppError(403, "OUTSIDE_YOUR_AREA", "This person is outside your Country, State and Region, or is an administrator. Ask your Super admin.")
    return u


@router.get("/options")
def options(me: User = Depends(perms.require("users", 1))):
    """What the 'create person' form needs: job titles with their pre-ticked levels, and the areas."""
    mine = perms.effective(me)
    titles = [{"title": t, "role": r, "permissions": dict(zip(AREAS, lv))} for t, (r, lv) in TITLES.items()
              if me.role == "admin" or t != "Admin"]
    return {"titles": titles, "areas": [{"key": a, "label": AREA_TEXT[a][0], "help": AREA_TEXT[a][1], "max": 2 if me.role == "admin" else (mine[a] if a != "users" else min(1, mine[a]))}
                                        for a in AREAS],
            "levels": [{"value": v, "label": n} for v, n in LEVEL_NAME.items()],
            "fixed_place": None if me.role == "admin" else {"country": me.country, "state": me.state, "region": me.region},
            "mine": mine}


@router.get("", response_model=list[UserOut])
def list_users(status: str = "all", me: User = Depends(perms.require("users", 1)), master: Session = Depends(get_master_db)):
    stmt = _scope_filter(select(User), me).order_by(User.created_at.desc())
    if status != "all":
        stmt = stmt.where(User.status == status)
    return list(master.scalars(stmt))


@router.post("", response_model=CreatedOut, status_code=201)
def create_user(body: CreateUserIn, request: Request, me: User = Depends(perms.require("users", 2)),
                master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    if me.role == "admin":
        country, state, region = body.country, body.state, body.region
    else:   # a 'Manage users' holder creates only inside his own area, whatever the form says
        country, state, region = me.country, me.state, me.region
    country, state, region = _check_place(country, state, region)
    levels = perms.clean(body.permissions) if body.permissions is not None else perms.preset(body.job_title)
    _check_levels(me, body.job_title, levels)
    if master.scalar(select(User.id).where(User.username == body.username)):
        raise AppError(409, "USERNAME_TAKEN", "This username is already taken. Please choose another one.")
    if master.scalar(select(User.id).where(func.lower(User.email) == str(body.email).lower())):
        raise AppError(409, "EMAIL_TAKEN", "An account with this e-mail address already exists.")
    password = temp_password()
    u = User(username=body.username, email=str(body.email).lower(), password_hash=hash_password(password), company_name=me.company_name,
             role=perms.role_of(body.job_title), job_title=body.job_title, perms=perms.store(levels), state=state, region=region, country=country,
             phone=body.phone, status="approved", must_change_password=True, created_by_id=me.id, email_verified=True)
    master.add(u)
    try:
        master.commit()
    except IntegrityError:
        master.rollback()
        raise AppError(409, "USER_EXISTS", "This username or e-mail is already registered.")
    audit.append(db, me.id, me.company_name, "user.create", f"user:{u.username}:{u.job_title}:{state}/{region}", client_ip(request))
    sent = body.send_email and mail.send_new_user_email(u.email, u.username, password, me.company_name, me.username)
    return CreatedOut(user=UserOut.model_validate(u), temporary_password=password, email_sent=bool(sent))


@router.put("/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UpdateUserIn, request: Request, me: User = Depends(perms.require("users", 2)),
                master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    u = _target(master, me, user_id)
    title = body.job_title or u.job_title or {"regional_admin": "Admin", "operator": "Operator", "guest": "Guest"}.get(u.role, "Operator")
    if body.job_title is not None and body.job_title != u.job_title:
        levels = perms.clean(body.permissions) if body.permissions is not None else perms.preset(body.job_title)
    elif body.permissions is not None:
        levels = perms.clean(body.permissions)
    else:
        levels = None
    if levels is not None:
        _check_levels(me, title, levels)
        u.job_title, u.role, u.perms = title, perms.role_of(title), perms.store(levels)
    if me.role == "admin" and (body.country or body.state or body.region):
        country, state, region = body.country or u.country, body.state or u.state, body.region or u.region
        country, state, region = _check_place(country, state, region)
        u.country, u.state, u.region = country, state, region
    elif body.country or body.state or body.region:
        raise AppError(403, "ONLY_SUPER_ADMIN", "Only the Super admin can move a person to another State or Region.")
    if "phone" in body.model_fields_set:
        u.phone = (body.phone or "").strip() or None
    u.token_version += 1       # the new rights apply at once: the person signs in again
    master.commit()
    audit.append(db, me.id, me.company_name, "user.update", f"user:{u.username}:{u.job_title}", client_ip(request))
    return u


def _set_status(user_id: int, status: str, action: str, request: Request, me: User, master: Session, db: Session) -> User:
    u = _target(master, me, user_id)
    if u.role == "admin":
        raise AppError(403, "NOT_ALLOWED", "The company owner cannot be switched off.")
    if status == "deactivated" and u.status != "approved":
        raise AppError(409, "NOT_ACTIVE", f"This account is already {u.status}.")
    if status == "approved" and u.status != "deactivated":
        raise AppError(409, "NOT_DEACTIVATED", "This account is not switched off.")
    u.status = status
    u.token_version += 1
    master.commit()
    audit.append(db, me.id, me.company_name, action, f"user:{u.username}", client_ip(request))
    return u


@router.post("/{user_id}/deactivate", response_model=UserOut)
def deactivate(user_id: int, request: Request, me: User = Depends(perms.require("users", 2)),
               master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    return _set_status(user_id, "deactivated", "user.deactivate", request, me, master, db)


@router.post("/{user_id}/activate", response_model=UserOut)
def reactivate(user_id: int, request: Request, me: User = Depends(perms.require("users", 2)),
               master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    return _set_status(user_id, "approved", "user.activate", request, me, master, db)


@router.post("/{user_id}/reset-password", response_model=CreatedOut)
def reset_password(user_id: int, request: Request, me: User = Depends(perms.require("users", 2)),
                   master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    """A new one-time password (the person forgot it, or it was lost). The old password and all sessions stop working."""
    u = _target(master, me, user_id)
    password = temp_password()
    u.password_hash, u.must_change_password = hash_password(password), True
    u.token_version += 1
    master.commit()
    audit.append(db, me.id, me.company_name, "user.reset_password", f"user:{u.username}", client_ip(request))
    sent = mail.send_new_user_email(u.email, u.username, password, me.company_name, me.username)
    return CreatedOut(user=UserOut.model_validate(u), temporary_password=password, email_sent=sent)


@router.post("/{user_id}/force-logout", response_model=MessageOut)
def force_logout(user_id: int, request: Request, me: User = Depends(perms.require("users", 2)),
                 master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    u = _target(master, me, user_id)
    u.token_version += 1
    master.commit()
    audit.append(db, me.id, me.company_name, "user.force_logout", f"user:{u.username}", client_ip(request))
    return MessageOut(message=f"{u.username} has been signed out of all sessions.")
