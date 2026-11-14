"""Baseline tenant schema + TimescaleDB hypertable, compression and retention policies.

Revision ID: 0001
"""
import re

from alembic import op
from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import TenantBase
from app.models import tenant  # noqa: F401

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

log = get_logger("migration")
_INTERVAL = re.compile(r"^\d+ (days?|weeks?|months?|years?)$")


def _try(bind, sql: str, what: str) -> bool:
    """Run optional TimescaleDB DDL inside a SAVEPOINT so plain PostgreSQL still works."""
    try:
        with bind.begin_nested():
            bind.execute(text(sql))
        return True
    except Exception as exc:  # pragma: no cover
        log.warning("timescale_step_skipped", step=what, error=type(exc).__name__)
        return False


def upgrade() -> None:
    bind = op.get_bind()
    TenantBase.metadata.create_all(bind=bind)

    compress_after = settings.TELEMETRY_COMPRESS_AFTER
    retention = settings.TELEMETRY_RETENTION
    for v in (compress_after, retention):
        if not _INTERVAL.match(v):
            raise RuntimeError(f"Invalid interval in settings: {v!r}")

    if not _try(bind, "CREATE EXTENSION IF NOT EXISTS timescaledb", "extension"):
        return
    if not _try(bind, "SELECT create_hypertable('telemetry', 'timestamp', if_not_exists => TRUE, migrate_data => TRUE)", "hypertable"):
        return
    if _try(bind, "ALTER TABLE telemetry SET (timescaledb.compress, timescaledb.compress_segmentby = 'device_id, metric_name')", "compress_settings"):
        _try(bind, f"SELECT add_compression_policy('telemetry', INTERVAL '{compress_after}', if_not_exists => TRUE)", "compression_policy")
    _try(bind, f"SELECT add_retention_policy('telemetry', INTERVAL '{retention}', if_not_exists => TRUE)", "retention_policy")


def downgrade() -> None:
    TenantBase.metadata.drop_all(bind=op.get_bind())
