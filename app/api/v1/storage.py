from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.constants import DB_NAME_RE
from app.core.crypto import encrypt
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.rate_limit import limiter
from app.db import provision
from app.db.session import engine_cache, get_master_db, open_tenant_session
from app.deps import client_ip, require_roles
from app.models.master import CompanyStorage, User
from app.schemas.storage import DbListIn, DbListOut, StorageIn, StorageOut, StorageSavedOut
from app.services import audit

router = APIRouter(prefix="/storage", tags=["storage"])
log = get_logger("storage")
admin_only = require_roles("admin")

INVALID_REGEX = AppError(
    422, "INVALID_REGEX",
    "Invalid database name format. Use only lowercase letters, numbers, and underscores (max 63 characters, starting with a letter or underscore).",
)


def _cloud_admin(body: DbListIn):
    if not (body.cloud_host and body.cloud_user and body.cloud_password and body.cloud_port):
        raise AppError(422, "CLOUD_CREDENTIALS_REQUIRED", "Enter the cloud host, port, username and password first.")
    return provision.cloud_admin_url(body.cloud_host, body.cloud_port, body.cloud_user, body.cloud_password)


def _claimed_by_others(master: Session, company: str) -> set[str]:
    rows = master.scalars(select(CompanyStorage.db_name).where(
        CompanyStorage.company_name != company, CompanyStorage.db_name.is_not(None)))
    return set(rows)


@router.get("", response_model=StorageOut)
def get_storage(user: User = Depends(admin_only), master: Session = Depends(get_master_db)):
    s = master.scalar(select(CompanyStorage).where(CompanyStorage.company_name == user.company_name))
    if not s or not s.db_name:
        return StorageOut(configured=False)
    return StorageOut(configured=True, mode=s.mode, db_name=s.db_name, cloud_host=s.cloud_host,
                      cloud_port=s.cloud_port, cloud_user=s.cloud_user)


@router.post("/databases", response_model=DbListOut)
@limiter.limit("10/minute")
def list_databases(request: Request, body: DbListIn, user: User = Depends(admin_only), master: Session = Depends(get_master_db)):
    admin_url = settings.local_admin_url if body.mode == "local" else _cloud_admin(body)
    names = provision.list_databases(admin_url)
    hidden = _claimed_by_others(master, user.company_name) | {settings.MASTER_DB_NAME}
    return DbListOut(databases=[n for n in names if n not in hidden], suggested_new_name=f"stech_{user.company_name}")


@router.put("", response_model=StorageSavedOut)
@limiter.limit("10/minute")
def save_storage(request: Request, body: StorageIn, user: User = Depends(admin_only), master: Session = Depends(get_master_db)):
    if not DB_NAME_RE.match(body.db_name):
        raise INVALID_REGEX
    if body.db_name in _claimed_by_others(master, user.company_name) or body.db_name == settings.MASTER_DB_NAME:
        raise AppError(409, "DB_NAME_CLAIMED",
                       f"The database name '{body.db_name}' is already claimed by another company. Please choose a unique database name.")

    targets = []  # (admin_url, tenant_url)
    local_url = settings.local_url(body.db_name)
    if body.mode in ("local", "hybrid"):
        targets.append((settings.local_admin_url, local_url))
    if body.mode in ("cloud", "hybrid"):
        admin = _cloud_admin(body)
        cloud_tenant = admin.set(database=body.db_name)
        targets.append((admin, cloud_tenant))

    created_any = False
    for admin_url, tenant_url in targets:
        provision.list_databases(admin_url)  # raises BAD_CREDENTIALS if the server rejects us
        if not provision.database_exists(admin_url, body.db_name):
            if not body.create_if_missing:
                raise AppError(404, "DB_NOT_FOUND",
                               f"Database '{body.db_name}' does not exist on the server. Tick 'Create new' or pick an existing database.")
            provision.create_database(admin_url, body.db_name)
            created_any = True
        provision.provision_schema(tenant_url)

    s = master.scalar(select(CompanyStorage).where(CompanyStorage.company_name == user.company_name))
    if not s:
        s = CompanyStorage(company_name=user.company_name)
        master.add(s)
    s.mode, s.db_name = body.mode, body.db_name
    s.encrypted_local_url = encrypt(local_url.render_as_string(hide_password=False)) if body.mode != "cloud" else None
    if body.mode == "local":
        s.cloud_host = s.cloud_port = s.cloud_user = s.encrypted_cloud_password = None
    else:
        s.cloud_host, s.cloud_port, s.cloud_user = body.cloud_host, body.cloud_port, body.cloud_user
        s.encrypted_cloud_password = encrypt(body.cloud_password)  # Fernet, never plaintext
    master.commit()
    engine_cache.clear(user.company_name)  # drop stale pools
    log.info("storage_updated", company=user.company_name, mode=body.mode, db=body.db_name, created=created_any)

    try:  # audit lands in the NEW primary database
        db = open_tenant_session(master, user.company_name)
        try:
            audit.append(db, user.id, user.company_name, "storage.update", f"{body.mode}:{body.db_name}", client_ip(request))
        finally:
            db.close()
    except Exception as exc:
        log.warning("audit_write_failed", error=type(exc).__name__)

    return StorageSavedOut(mode=body.mode, db_name=body.db_name, created=created_any,
                           message=f"Storage set to {body.mode} using database '{body.db_name}'.")
