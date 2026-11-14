"""v1.1.5: alarm rule kinds, downtime log, batch tracking, saved screens (animation + HMI), command reason. Idempotent.

Revision ID: 0011
Revises: 0010
"""
from alembic import op
from sqlalchemy import text

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

STATEMENTS = [
    "ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS kind VARCHAR(10) NOT NULL DEFAULT 'level'",
    "ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS deadband DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS window_seconds INTEGER NOT NULL DEFAULT 60",
    "ALTER TABLE commands ADD COLUMN IF NOT EXISTS reason VARCHAR(200)",
    "ALTER TABLE commands ADD COLUMN IF NOT EXISTS pulse BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE commands ADD COLUMN IF NOT EXISTS approved_by VARCHAR(64)",
    """CREATE TABLE IF NOT EXISTS downtime_events (
         id SERIAL PRIMARY KEY,
         device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
         started_at TIMESTAMPTZ NOT NULL,
         ended_at TIMESTAMPTZ,
         source VARCHAR(10) NOT NULL DEFAULT 'runbit',
         reason VARCHAR(80),
         note VARCHAR(200),
         set_by VARCHAR(64)
       )""",
    "CREATE INDEX IF NOT EXISTS ix_downtime_device_id ON downtime_events (device_id)",
    "CREATE INDEX IF NOT EXISTS ix_downtime_started ON downtime_events (started_at)",
    """CREATE TABLE IF NOT EXISTS batches (
         id SERIAL PRIMARY KEY,
         device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
         batch_no VARCHAR(64) NOT NULL,
         product VARCHAR(120),
         started_at TIMESTAMPTZ NOT NULL,
         ended_at TIMESTAMPTZ,
         started_by VARCHAR(64) NOT NULL,
         ended_by VARCHAR(64),
         summary TEXT
       )""",
    "CREATE INDEX IF NOT EXISTS ix_batches_device_id ON batches (device_id)",
    """CREATE TABLE IF NOT EXISTS screens (
         id SERIAL PRIMARY KEY,
         kind VARCHAR(10) NOT NULL,
         device_id INTEGER REFERENCES devices(id) ON DELETE CASCADE,
         name VARCHAR(80) NOT NULL,
         config TEXT NOT NULL,
         updated_by VARCHAR(64) NOT NULL DEFAULT '',
         updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
       )""",
    "CREATE INDEX IF NOT EXISTS ix_screens_kind_device ON screens (kind, device_id)",
]


def upgrade() -> None:
    for sql in STATEMENTS:
        op.execute(text(sql))


def downgrade() -> None:
    pass
