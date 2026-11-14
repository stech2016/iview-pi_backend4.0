"""v2.0: the Detected list of customer gateways (tags that nobody chose yet). Idempotent.

Revision ID: 0012
Revises: 0011
"""
from alembic import op
from sqlalchemy import text

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS detected_tags (
         id SERIAL PRIMARY KEY,
         plant_id VARCHAR(16) NOT NULL,
         ext_device VARCHAR(120) NOT NULL,
         tag VARCHAR(64) NOT NULL,
         sample VARCHAR(40),
         kind VARCHAR(8) NOT NULL DEFAULT 'number',
         count INTEGER NOT NULL DEFAULT 1,
         first_seen TIMESTAMPTZ NOT NULL,
         last_seen TIMESTAMPTZ NOT NULL,
         dismissed BOOLEAN NOT NULL DEFAULT false,
         CONSTRAINT uq_detected_tag UNIQUE (plant_id, ext_device, tag)
       )""",
    "CREATE INDEX IF NOT EXISTS ix_detected_tags_plant_id ON detected_tags (plant_id)",
]


def upgrade() -> None:
    conn = op.get_bind()
    for sql in STATEMENTS:
        conn.execute(text(sql))


def downgrade() -> None:
    op.get_bind().execute(text("DROP TABLE IF EXISTS detected_tags"))
