"""v1.1 alert rules (if value is above / below X for N seconds, raise an alarm). Idempotent.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
from sqlalchemy import text

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS alert_rules (
         id SERIAL PRIMARY KEY,
         device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
         metric_name VARCHAR(64) NOT NULL,
         op VARCHAR(2) NOT NULL,
         threshold DOUBLE PRECISION NOT NULL,
         for_seconds INTEGER NOT NULL DEFAULT 0,
         severity VARCHAR(12) NOT NULL DEFAULT 'major',
         message VARCHAR(200),
         enabled BOOLEAN NOT NULL DEFAULT true,
         created_by VARCHAR(64) NOT NULL DEFAULT '',
         created_at TIMESTAMPTZ NOT NULL DEFAULT now()
       )""",
    "CREATE INDEX IF NOT EXISTS ix_alert_rules_device_id ON alert_rules (device_id)",
]


def upgrade() -> None:
    for sql in STATEMENTS:
        op.execute(text(sql))


def downgrade() -> None:
    pass
