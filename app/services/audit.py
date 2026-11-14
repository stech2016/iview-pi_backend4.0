import hashlib
import hmac
import json

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.constants import GENESIS_HASH
from app.core.logging import get_logger
from app.core.timeutil import utcnow
from app.models.tenant import AuditLog
from app.services import sync

log = get_logger("audit")
_LOCK_KEY = 7310001


def compute_hash(user_id, company, action, resource, ts, ip, previous_hash: str) -> str:
    payload = json.dumps(
        [user_id, company, action, resource, ts.isoformat(), ip, previous_hash],
        separators=(",", ":"), ensure_ascii=True,
    )
    return hmac.new(settings.AUDIT_HMAC_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def append(db: Session, user_id: int | None, company: str, action: str, resource: str, ip: str | None) -> AuditLog:
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})  # serialise chain writers
    prev = db.scalar(select(AuditLog.row_hash).order_by(AuditLog.id.desc()).limit(1)) or GENESIS_HASH
    ts = utcnow()
    row = AuditLog(
        user_id=user_id, company_name=company, action=action, resource=resource[:255],
        timestamp=ts, ip_address=ip, previous_hash=prev,
        row_hash=compute_hash(user_id, company, action, resource[:255], ts, ip, prev),
    )
    db.add(row)
    db.commit()
    log.info("audit", company=company, action=action, resource=resource[:255], user_id=user_id)
    sync.mirror(db, "audit_logs", [row])
    return row


def verify_chain(db: Session) -> dict:
    prev, checked = GENESIS_HASH, 0
    for row in db.execute(select(AuditLog).order_by(AuditLog.id).execution_options(yield_per=1000)).scalars():
        ts = row.timestamp.astimezone(utcnow().tzinfo)
        expected = compute_hash(row.user_id, row.company_name, row.action, row.resource, ts, row.ip_address, row.previous_hash)
        if row.previous_hash != prev or not hmac.compare_digest(expected, row.row_hash):
            return {"valid": False, "checked": checked, "first_invalid_id": row.id}
        prev, checked = row.row_hash, checked + 1
    return {"valid": True, "checked": checked, "first_invalid_id": None}


def safe_audit(master: Session, user, action: str, resource: str, ip: str | None) -> None:
    """Best-effort audit for flows (login/logout) that must not fail if tenant DB is down."""
    from app.db.session import tenant_session

    try:
        with tenant_session(master, user.company_name) as db:
            append(db, user.id, user.company_name, action, resource, ip)
    except Exception as exc:
        log.warning("audit_write_failed", action=action, error=type(exc).__name__)
