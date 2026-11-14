from datetime import datetime

from sqlalchemy import DateTime, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from app.core.logging import get_logger
from app.db.base import TenantBase
from app.db.session import MasterSession, engine_cache, mirror_url
from app.models import tenant  # noqa: F401
from app.models.master import CompanyStorage
from app.workers.celery_app import celery_app

log = get_logger("celery")


def _coerce(table, row: dict) -> dict:
    out = dict(row)
    for col in table.columns:
        if isinstance(col.type, DateTime) and isinstance(out.get(col.name), str):
            out[col.name] = datetime.fromisoformat(out[col.name])
    return out


@celery_app.task(
    bind=True, name="stech.mirror_rows",
    autoretry_for=(OperationalError, DBAPIError, IntegrityError),  # network failure / FK ordering -> retry
    retry_backoff=True, retry_backoff_max=600, retry_jitter=True, max_retries=30,
)
def mirror_rows(self, company: str, table_name: str, rows: list[dict], op: str = "upsert"):
    with MasterSession() as master:
        storage = master.scalar(select(CompanyStorage).where(CompanyStorage.company_name == company))
    url = mirror_url(storage) if storage else None
    if url is None:
        return "skipped"  # company is no longer hybrid
    table = TenantBase.metadata.tables[table_name]
    engine = engine_cache.get(company, "mirror", url)
    pk = [c.name for c in table.primary_key.columns]
    with engine.begin() as conn:
        if op == "delete":
            for r in rows:
                conn.execute(delete(table).where(*[table.c[k] == r[k] for k in pk]))
        else:
            data = [_coerce(table, r) for r in rows]
            stmt = pg_insert(table).values(data)
            update_cols = {c.name: stmt.excluded[c.name] for c in table.columns if c.name not in pk}
            stmt = stmt.on_conflict_do_update(index_elements=pk, set_=update_cols) if update_cols \
                else stmt.on_conflict_do_nothing(index_elements=pk)
            conn.execute(stmt)
    log.info("mirror_done", company=company, table=table_name, rows=len(rows), op=op)
    return len(rows)
