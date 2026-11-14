"""Plant data is kept until the user deletes it (or the user picks a 'keep for' time). Removes the automatic 5-year delete. Idempotent.

Revision ID: 0008
Revises: 0007
"""
from alembic import op
from sqlalchemy import text

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    try:
        with bind.begin_nested():   # plain PostgreSQL (no TimescaleDB) has no such policy: skip quietly
            bind.execute(text("SELECT remove_retention_policy('telemetry', if_exists => TRUE)"))
    except Exception:
        pass


def downgrade() -> None:
    pass
