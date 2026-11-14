"""Every Modbus variant: connection details on devices, single-bit sensors, 64-bit types. Idempotent.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
from sqlalchemy import text

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

STATEMENTS = [
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS unit_id INTEGER",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS serial_baud INTEGER",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS serial_parity VARCHAR(1)",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS serial_databits INTEGER",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS serial_stopbits INTEGER",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS byte_order VARCHAR(6)",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS word_order VARCHAR(6)",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS address_base INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS timeout_ms INTEGER",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS retries INTEGER",
    "ALTER TABLE device_registers ADD COLUMN IF NOT EXISTS bit_index INTEGER",
    # one register may now carry several single-bit sensors: the uniqueness rule includes the bit (NULL = no bit)
    "ALTER TABLE device_registers DROP CONSTRAINT IF EXISTS uq_register_type_address",
    "DROP INDEX IF EXISTS uq_register_type_address",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_register_type_address ON device_registers (device_id, register_type, register_address, COALESCE(bit_index, -1))",
]


def upgrade() -> None:
    for sql in STATEMENTS:
        op.execute(text(sql))


def downgrade() -> None:
    pass
