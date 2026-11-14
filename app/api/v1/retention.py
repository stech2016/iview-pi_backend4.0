"""Storage screen: used / free space, 'keep data for', regional shares, free up space."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core import geo, normalize
from app.core.constants import REGIONS
from app.core.errors import AppError
from app.db.session import get_master_db
from app.deps import ADMIN_ROLES, client_ip, get_company_db, require_roles, scope_devices
from app.models.master import StorageShare, User
from app.models.tenant import Device
from app.services import audit, erp, retention, tablefile
from app.services.export import _offset_minutes, iter_file

router = APIRouter(prefix="/retention", tags=["storage"])
any_admin = require_roles(*ADMIN_ROLES)
super_admin = require_roles("admin")


class KeepIn(BaseModel):
    keep_days: int


class ShareIn(BaseModel):
    state: str
    region: str
    share_gb: float = Field(gt=0, le=100000)


class FreeUpIn(BaseModel):
    older_than_days: int = Field(ge=7, le=3650)
    confirm: bool
    state: str | None = None        # Super admin may free one region; a Regional admin always frees their own
    region: str | None = None


@router.get("")
def get_usage(state: str | None = None, region: str | None = None, user: User = Depends(any_admin),
              master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    """Regional admin: always their own State + Region. Super admin: the whole company, or one region when state + region are given."""
    if user.role == "regional_admin":
        state, region = user.state, user.region
    out = retention.usage(master, db, user.company_name, state, region)
    out["choices"] = list(retention.KEEP_CHOICES)
    if user.role == "admin" and not state:
        out["shares"] = [{"state": s.state, "region": s.region, "share_gb": s.share_gb} for s in master.scalars(
            select(StorageShare).where(StorageShare.company_name == user.company_name).order_by(StorageShare.state, StorageShare.region))]
    return out


@router.put("/keep")
def set_keep(body: KeepIn, request: Request, user: User = Depends(super_admin),
             master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    if body.keep_days not in retention.KEEP_CHOICES:
        raise AppError(422, "KEEP_DAYS_INVALID", "Choose keep until deleted, or 1, 3, 6 months, or 1, 2, 3, 5 years.")
    p = retention.policy(master, user.company_name, create=True)
    p.keep_days, p.updated_by = body.keep_days, user.username
    master.commit()
    audit.append(db, user.id, user.company_name, "storage.keep_days", f"days:{body.keep_days}", client_ip(request))
    return {"keep_days": p.keep_days}


@router.put("/shares")
def set_share(body: ShareIn, request: Request, user: User = Depends(super_admin),
              master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    body.state, body.region = normalize.state(body.state), normalize.region(body.region)
    if not geo.is_state(body.state) or body.region not in REGIONS:
        raise AppError(422, "REGION_INVALID", "Choose a valid State and Region.")
    p = retention.policy(master, user.company_name)
    if p and p.quota_gb:
        others = sum(s.share_gb for s in master.scalars(select(StorageShare).where(
            StorageShare.company_name == user.company_name, (StorageShare.state != body.state) | (StorageShare.region != body.region))))
        if others + body.share_gb > p.quota_gb:
            raise AppError(422, "SHARE_TOO_BIG", f"The shares would add up to more than the {p.quota_gb:g} GB your plan includes.")
    row = master.scalar(select(StorageShare).where(StorageShare.company_name == user.company_name,
                                                   StorageShare.state == body.state, StorageShare.region == body.region))
    if row:
        row.share_gb = body.share_gb
    else:
        master.add(StorageShare(company_name=user.company_name, state=body.state, region=body.region, share_gb=body.share_gb))
    master.commit()
    audit.append(db, user.id, user.company_name, "storage.share", f"{body.state}/{body.region}:{body.share_gb:g}GB", client_ip(request))
    return {"state": body.state, "region": body.region, "share_gb": body.share_gb}


@router.delete("/shares")
def clear_share(state: str, region: str, request: Request, user: User = Depends(super_admin),
                master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    master.execute(delete(StorageShare).where(StorageShare.company_name == user.company_name,
                                              StorageShare.state == state, StorageShare.region == region))
    master.commit()
    audit.append(db, user.id, user.company_name, "storage.share_removed", f"{state}/{region}", client_ip(request))
    return {"removed": True}


@router.get("/export-before")
def export_before(request: Request, older_than_days: int = Query(ge=7, le=3650), format: str = Query(default="xlsx", pattern="^(csv|xlsx)$"),
                  user: User = Depends(super_admin), db: Session = Depends(get_company_db)):
    """The export to take BEFORE freeing up space: daily min / average / max / last of every sensor for ALL readings that
    "free up" would delete (everything older than N days), in one file."""
    cut = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    devices = list(db.scalars(scope_devices(select(Device).where(Device.protocol != "Gateway"), user)))
    rows = erp.rows(db, devices, datetime(2000, 1, 1, tzinfo=timezone.utc), cut, _offset_minutes())
    tmp, filename, media = tablefile.build(erp.title(user.company_name), erp.HEADERS, rows, format, f"{user.company_name}_before_{cut:%Y%m%d}", group=8)
    audit.append(db, user.id, user.company_name, "storage.export_before", f"older_than_days:{older_than_days}:{format}", client_ip(request))
    return StreamingResponse(iter_file(tmp), media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/free-up")
def free_up(body: FreeUpIn, request: Request, user: User = Depends(super_admin), db: Session = Depends(get_company_db)):
    """Deletes stored readings older than N days. The app shows a confirm box and offers the export first."""
    if not body.confirm:
        raise AppError(422, "CONFIRM_REQUIRED", "Confirm that the readings should be deleted. This cannot be undone.")
    state, region = body.state, body.region
    n = retention.delete_older(db, body.older_than_days, state, region)
    audit.append(db, user.id, user.company_name, "storage.free_up", f"older_than_days:{body.older_than_days}:{state or 'all'}/{region or 'all'}:rows:{n}", client_ip(request))
    return {"deleted_rows": n}
