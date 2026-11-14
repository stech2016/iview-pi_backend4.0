"""Change requests (operator changes waiting for admin approval). Idempotent.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
from sqlalchemy import text

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS change_requests (
         id SERIAL PRIMARY KEY,
         device_id INTEGER,
         device_name VARCHAR(120),
         requested_by_id INTEGER NOT NULL,
         requested_by VARCHAR(64) NOT NULL,
         action VARCHAR(24) NOT NULL,
         register_id INTEGER,
         summary TEXT NOT NULL,
         payload TEXT NOT NULL,
         status VARCHAR(12) NOT NULL DEFAULT 'pending',
         decided_by VARCHAR(64),
         decided_at TIMESTAMPTZ,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now()
       )""",
    "CREATE INDEX IF NOT EXISTS ix_change_requests_status ON change_requests (status)",
]


def upgrade() -> None:
    for sql in STATEMENTS:
        op.execute(text(sql))


def downgrade() -> None:
    pass
