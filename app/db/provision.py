"""Create / inspect Postgres databases and provision tenant schemas with Alembic."""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from app.core.constants import DB_NAME_RE, RESERVED_DBS
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.session import PG_CONNECT_ARGS

log = get_logger("provision")
BACKEND_DIR = Path(__file__).resolve().parents[2]

BAD_CREDS = AppError(
    401, "BAD_CREDENTIALS",
    "Failed to connect to the database using the provided cloud credentials. "
    "Please verify your host, port, username, and password.",
)


def _admin_engine(url: URL):
    return create_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool, connect_args=PG_CONNECT_ARGS)


def cloud_admin_url(host: str, port: int, user: str, password: str) -> URL:
    return URL.create("postgresql+psycopg2", username=user, password=password, host=host, port=port, database="postgres")


def list_databases(admin_url: URL) -> list[str]:
    try:
        with _admin_engine(admin_url).connect() as conn:
            rows = conn.execute(text("SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname"))
            return [r[0] for r in rows if r[0] not in RESERVED_DBS]
    except OperationalError:
        raise BAD_CREDS
    except Exception:
        raise BAD_CREDS


def database_exists(admin_url: URL, name: str) -> bool:
    try:
        with _admin_engine(admin_url).connect() as conn:
            return conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).first() is not None
    except Exception:
        raise BAD_CREDS


def create_database(admin_url: URL, name: str) -> None:
    if not DB_NAME_RE.match(name):  # defence in depth: name is interpolated into DDL
        raise AppError(422, "INVALID_REGEX", "Invalid database name format. Use only lowercase letters, numbers, and underscores (max 63 characters, starting with a letter or underscore).")
    try:
        with _admin_engine(admin_url).connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:
        log.error("create_database_failed", db=name, error=type(exc).__name__)
        raise AppError(503, "DB_CREATE_FAILED", f"Could not create database '{name}'. Check that the database user has CREATEDB permission, then try again.")


def ensure_database(admin_url: URL, name: str) -> bool:
    if database_exists(admin_url, name):
        return False
    create_database(admin_url, name)
    return True


def provision_schema(tenant_url: URL) -> None:
    """Run Alembic migrations (tables, hypertable, compression + retention policies)."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["tenant_url"] = tenant_url.render_as_string(hide_password=False)
    try:
        command.upgrade(cfg, "head")
    except Exception as exc:
        log.error("provision_schema_failed", error=type(exc).__name__, detail=str(exc)[:300])
        raise AppError(503, "PROVISION_FAILED", "The database was reachable but its tables could not be created. Verify the database user has full rights on this database and try again.")
    log.info("tenant_schema_ready", db=tenant_url.database)
