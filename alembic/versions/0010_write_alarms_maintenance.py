"""v1.1.5: write commands, writable sensors, alarm comments and shelving, maintenance mode, map position. Idempotent.

Revision ID: 0010
Revises: 0009
"""
from alembic import op
from sqlalchemy import text

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

STATEMENTS = [
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS maintenance_until TIMESTAMPTZ",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS maintenance_note VARCHAR(200)",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS maintenance_by VARCHAR(64)",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION",
    "ALTER TABLE device_registers ADD COLUMN IF NOT EXISTS writable BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE device_registers ADD COLUMN IF NOT EXISTS write_min DOUBLE PRECISION",
    "ALTER TABLE device_registers ADD COLUMN IF NOT EXISTS write_max DOUBLE PRECISION",
    "ALTER TABLE device_registers ADD COLUMN IF NOT EXISTS stuck_minutes INTEGER",
    """CREATE TABLE IF NOT EXISTS alarm_comments (
         id SERIAL PRIMARY KEY,
         alarm_id INTEGER NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
         user_id INTEGER NOT NULL,
         username VARCHAR(32) NOT NULL,
         text VARCHAR(500) NOT NULL,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now()
       )""",
    "CREATE INDEX IF NOT EXISTS ix_alarm_comments_alarm_id ON alarm_comments (alarm_id)",
    """CREATE TABLE IF NOT EXISTS alarm_shelves (
         id SERIAL PRIMARY KEY,
         device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
         metric_name VARCHAR(64) NOT NULL,
         until TIMESTAMPTZ NOT NULL,
         reason VARCHAR(200) NOT NULL,
         shelved_by VARCHAR(32) NOT NULL,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         CONSTRAINT uq_shelf_device_metric UNIQUE (device_id, metric_name)
       )""",
    "CREATE INDEX IF NOT EXISTS ix_alarm_shelves_device_id ON alarm_shelves (device_id)",
    """CREATE TABLE IF NOT EXISTS commands (
         id SERIAL PRIMARY KEY,
         uid VARCHAR(32) NOT NULL UNIQUE,
         device_id INTEGER NOT NULL,
         device_name VARCHAR(120) NOT NULL,
         register_id INTEGER NOT NULL,
         metric_name VARCHAR(64) NOT NULL,
         unit VARCHAR(16),
         value DOUBLE PRECISION NOT NULL,
         raw_value DOUBLE PRECISION NOT NULL,
         previous_value DOUBLE PRECISION,
         readback DOUBLE PRECISION,
         state VARCHAR(12) NOT NULL DEFAULT 'sent',
         error VARCHAR(200),
         requested_by_id INTEGER NOT NULL,
         requested_by VARCHAR(64) NOT NULL,
         gateway_id VARCHAR(32) NOT NULL DEFAULT 'main',
         state_name VARCHAR(64) NOT NULL DEFAULT '',
         region VARCHAR(10) NOT NULL DEFAULT '',
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         expires_at TIMESTAMPTZ NOT NULL,
         delivered_at TIMESTAMPTZ,
         written_at TIMESTAMPTZ,
         finished_at TIMESTAMPTZ
       )""",
    "CREATE INDEX IF NOT EXISTS ix_commands_device_id ON commands (device_id)",
    "CREATE INDEX IF NOT EXISTS ix_commands_state ON commands (state)",
    "CREATE INDEX IF NOT EXISTS ix_commands_register_state ON commands (register_id, state)",
]


def upgrade() -> None:
    for sql in STATEMENTS:
        op.execute(text(sql))


def downgrade() -> None:
    pass
