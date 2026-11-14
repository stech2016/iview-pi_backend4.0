"""Optional City and Plant name per device (information only). Idempotent.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
from sqlalchemy import text

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE devices ADD COLUMN IF NOT EXISTS city VARCHAR(80)"))
    op.execute(text("ALTER TABLE devices ADD COLUMN IF NOT EXISTS plant_name VARCHAR(120)"))
    # limit change requests proposed by the Super admin's server
    op.execute(text("ALTER TABLE change_requests ADD COLUMN IF NOT EXISTS source VARCHAR(8)"))
    op.execute(text("ALTER TABLE change_requests ADD COLUMN IF NOT EXISTS decision_note TEXT"))


def downgrade() -> None:
    pass
