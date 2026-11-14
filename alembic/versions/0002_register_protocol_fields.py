"""Protocol-specific register fields. Idempotent: safe on new and on older tenant databases.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
from sqlalchemy import text

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

STATEMENTS = [
    "ALTER TABLE device_registers ADD COLUMN IF NOT EXISTS sensor_type VARCHAR(32)",
    "ALTER TABLE device_registers ADD COLUMN IF NOT EXISTS scale_factor DOUBLE PRECISION NOT NULL DEFAULT 1",
    "ALTER TABLE device_registers ADD COLUMN IF NOT EXISTS register_type VARCHAR(12)",
    "ALTER TABLE device_registers ADD COLUMN IF NOT EXISTS node_id VARCHAR(255)",
    "ALTER TABLE device_registers ADD COLUMN IF NOT EXISTS topic VARCHAR(255)",
    "ALTER TABLE device_registers ADD COLUMN IF NOT EXISTS json_field VARCHAR(64)",
    "ALTER TABLE device_registers ALTER COLUMN register_address DROP NOT NULL",
    "UPDATE device_registers SET register_type = 'holding' WHERE register_type IS NULL AND register_address IS NOT NULL",
    """DO $$ BEGIN
         IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_register_address') THEN
           ALTER TABLE device_registers DROP CONSTRAINT uq_register_address;
         END IF;
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_register_type_address')
            AND NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'uq_register_type_address') THEN
           ALTER TABLE device_registers ADD CONSTRAINT uq_register_type_address UNIQUE (device_id, register_type, register_address);
         END IF;
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_register_node') THEN
           ALTER TABLE device_registers ADD CONSTRAINT uq_register_node UNIQUE (device_id, node_id);
         END IF;
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_register_topic') THEN
           ALTER TABLE device_registers ADD CONSTRAINT uq_register_topic UNIQUE (device_id, topic, json_field);
         END IF;
       END $$""",
]


def upgrade() -> None:
    for sql in STATEMENTS:
        op.execute(text(sql))


def downgrade() -> None:
    pass
