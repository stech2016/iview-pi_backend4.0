"""Alembic environment for the *per-tenant* databases (stech_<company>).

Use:
  * programmatically: app.db.provision.provision_schema(url)   (used by the API)
  * CLI for one tenant:  TENANT_DATABASE_URL=postgresql+psycopg2://... alembic upgrade head
  * all tenants:         python scripts/migrate_all_tenants.py
"""
import os
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models import tenant  # noqa: E402,F401  (registers tables)
from app.db.base import TenantBase  # noqa: E402

config = context.config
target_metadata = TenantBase.metadata


def _url() -> str:
    url = config.attributes.get("tenant_url") or os.environ.get("TENANT_DATABASE_URL")
    if not url:
        raise RuntimeError("Set TENANT_DATABASE_URL or pass cfg.attributes['tenant_url']")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool, connect_args={"options": "-c timezone=utc"})
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
