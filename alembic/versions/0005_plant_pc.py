"""Which plant PC reads a device (several plant PCs per State + Region). Idempotent.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
from sqlalchemy import text

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE devices ADD COLUMN IF NOT EXISTS plant_pc VARCHAR(32)"))


def downgrade() -> None:
    pass
