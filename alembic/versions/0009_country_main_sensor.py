"""Country per device and the 'main reading' flag per sensor (dashboard tile shows two main readings). Idempotent.

Revision ID: 0009
Revises: 0008
"""
from alembic import op
from sqlalchemy import text

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE devices ADD COLUMN IF NOT EXISTS country VARCHAR(64) NOT NULL DEFAULT 'India'"))
    op.execute(text("ALTER TABLE device_registers ADD COLUMN IF NOT EXISTS is_main BOOLEAN NOT NULL DEFAULT false"))


def downgrade() -> None:
    pass
