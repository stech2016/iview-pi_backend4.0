from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_company_db, require_roles
from app.models.master import User
from app.models.tenant import AuditLog
from app.services import audit

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("")
def list_audit(limit: int = 100, user: User = Depends(require_roles("admin")), db: Session = Depends(get_company_db)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(max(limit, 1), 500)))
    return [{"id": r.id, "user_id": r.user_id, "action": r.action, "resource": r.resource,
             "timestamp": r.timestamp.isoformat(), "ip_address": r.ip_address, "row_hash": r.row_hash} for r in rows]


@router.get("/verify")
def verify(user: User = Depends(require_roles("admin")), db: Session = Depends(get_company_db)):
    return audit.verify_chain(db)
