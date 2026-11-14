from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.security import decode_token
from app.db.session import get_master_db, tenant_session_for_storage
from app.models.master import CompanyStorage, User
from app.models.tenant import Device

bearer = HTTPBearer(auto_error=False)
log = get_logger("auth")


def user_from_token(token: str, master: Session) -> User:
    payload = decode_token(token)
    user = master.get(User, int(payload["sub"]))
    # token_version is validated on EVERY request: bumping it instantly revokes tokens
    if not user or user.token_version != payload.get("tv") or user.status != "approved":
        log.warning("token_rejected", reason="revoked_or_unknown")
        raise AppError(401, "TOKEN_REVOKED", "Your session was ended. Please log in again.")
    return user


# a person who still has the one-time password may only change it (and log out)
PASSWORD_CHANGE_OK = ("/auth/change-password", "/auth/logout", "/auth/me", "/auth/terms")


def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    master: Session = Depends(get_master_db),
) -> User:
    if creds is None:
        raise AppError(401, "NOT_AUTHENTICATED", "You are not logged in. Please log in to continue.")
    user = user_from_token(creds.credentials, master)
    if user.must_change_password and not request.url.path.endswith(PASSWORD_CHANGE_OK):
        raise AppError(403, "PASSWORD_CHANGE_REQUIRED", "Choose your own password first. Your one-time password only works for this.")
    return user


def require_roles(*roles: str):
    def dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise AppError(403, "FORBIDDEN", f"Your role ({user.role}) cannot perform this action. Ask a company administrator for access.")
        return user
    return dep


ADMIN_ROLES = ("admin", "regional_admin")   # admin = Super admin (whole company); regional_admin = one State + Region


def can_manage_user(actor: User, target: User) -> bool:
    """Super admin: everybody else in the company. Anybody else with 'Manage users' (Users = Read and Write): the people of the
    SAME Country, State and Region who are not administrators."""
    from app.services import perms
    if target.company_name != actor.company_name or target.id == actor.id:
        return False
    if actor.role == "admin":
        return True
    return (perms.can(actor, "users", 2) and target.role in ("operator", "guest")
            and (target.country, target.state, target.region) == (actor.country, actor.state, actor.region))


def in_scope(user: User, state: str, region: str) -> bool:
    return user.role == "admin" or (state == user.state and region == user.region)




def get_company_db(user: User = Depends(get_current_user), master: Session = Depends(get_master_db)):
    """Resolves the caller's company -> isolated tenant DB session (never a global URL)."""
    storage = master.scalar(select(CompanyStorage).where(CompanyStorage.company_name == user.company_name))
    session = tenant_session_for_storage(storage)
    try:
        yield session
    finally:
        session.close()


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def scope_devices(stmt, user: User):
    """Super admin: whole company. Regional admin / Operators / Guests: only their assigned state AND region
    (plus the company-wide 'Plant Gateway' alarm device, which is tagged state 'ALL')."""
    if user.role != "admin":
        stmt = stmt.where(or_(Device.state == "ALL", and_(Device.state == user.state, Device.region == user.region)))
    return stmt


def get_scoped_device(db: Session, user: User, device_id: int) -> Device:
    device = db.scalar(scope_devices(select(Device).where(Device.id == device_id), user))
    if not device:
        raise AppError(404, "DEVICE_NOT_FOUND", "Device not found or outside your assigned state/region.")
    return device
