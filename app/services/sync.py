"""Hybrid sync: writes land in the local DB first; cloud mirroring goes through Celery/RabbitMQ."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.logging import get_logger

log = get_logger("sync")


def serialize(obj) -> dict:
    out = {}
    for col in inspect(obj.__class__).columns:
        v = getattr(obj, col.key)
        if isinstance(v, (datetime, date)):
            v = v.isoformat()
        elif isinstance(v, Decimal):
            v = float(v)
        out[col.key] = v
    return out


def mirror(db: Session, table: str, objs: list, op: str = "upsert", rows: list[dict] | None = None) -> None:
    if not db.info.get("hybrid"):
        return
    payload = rows if rows is not None else [serialize(o) for o in objs]
    if not payload:
        return
    try:
        from app.workers.tasks import mirror_rows

        for i in range(0, len(payload), 1000):
            mirror_rows.delay(db.info["company"], table, payload[i:i + 1000], op)
    except Exception as exc:  # broker down must never fail the user's write
        log.warning("mirror_dispatch_failed", table=table, error=type(exc).__name__)
