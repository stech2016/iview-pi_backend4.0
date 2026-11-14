import os
import sys

# lets `python app\main.py` work from the backend folder (adds backend/ to the import path)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.pages import router as pages_router
from app.api.v1.router import api_router
from app.core.tzctx import TimezoneMiddleware
from app.core import errors
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import limiter
from app.db import provision
from app.db.base import MasterBase
from app.db.session import master_engine
from app.models import master as _master_models  # noqa: F401

configure_logging(settings.LOG_LEVEL)
log = get_logger("app")


def _upgrade_tenant_databases() -> None:
    """Apply pending Alembic migrations to every company database (idempotent, safe on each start)."""
    from sqlalchemy import select

    from app.db.session import MasterSession, primary_url
    from app.models.master import CompanyStorage

    with MasterSession() as master:
        for st in master.scalars(select(CompanyStorage).where(CompanyStorage.db_name.is_not(None))):
            try:
                provision.provision_schema(primary_url(st))
            except Exception as exc:  # one unreachable company DB must not stop the server
                log.warning("tenant_upgrade_skipped", company=st.company_name, error=type(exc).__name__)


def _upgrade_master_tables() -> None:
    """The gateways table used to allow ONE row per company. Adds the per-gateway columns and the (company, gateway) key.
    Safe to run on every start (IF NOT EXISTS); an advisory lock keeps two API workers from running it at the same time."""
    from sqlalchemy import text

    steps = [
        "ALTER TABLE gateways ADD COLUMN IF NOT EXISTS gateway_id VARCHAR(32) NOT NULL DEFAULT 'main'",
        "ALTER TABLE gateways ADD COLUMN IF NOT EXISTS name VARCHAR(80)",
        "ALTER TABLE gateways ADD COLUMN IF NOT EXISTS site_state VARCHAR(64)",
        "ALTER TABLE gateways ADD COLUMN IF NOT EXISTS site_region VARCHAR(10)",
        "ALTER TABLE gateways ADD COLUMN IF NOT EXISTS key_version INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE gateways ADD COLUMN IF NOT EXISTS auto_update BOOLEAN NOT NULL DEFAULT true",
        "ALTER TABLE gateways DROP CONSTRAINT IF EXISTS gateways_company_name_key",
        "DROP INDEX IF EXISTS ix_gateways_company_name",
        "CREATE INDEX IF NOT EXISTS ix_gateways_company_name ON gateways (company_name)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_gateway_company_gid ON gateways (company_name, gateway_id)",
        # v1.1: people
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS region_company VARCHAR(80)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS city VARCHAR(80)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS plant_name VARCHAR(120)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(20)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_chat_id VARCHAR(32)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS country VARCHAR(64) NOT NULL DEFAULT 'India'",
        "DROP INDEX IF EXISTS ix_users_parent_company",
        "ALTER TABLE users DROP COLUMN IF EXISTS parent_company",
        # v1.1.5: job title + permission levels, one-time password, e-mail activation, terms
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS job_title VARCHAR(24)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS perms TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_by_id INTEGER",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT true",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS terms_accepted_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ",
    ]
    try:
        with master_engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(748201)"))
            for sql in steps:
                conn.execute(text(sql))
    except Exception as exc:
        log.warning("master_upgrade_failed", error=type(exc).__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    provision.ensure_database(settings.local_admin_url, settings.MASTER_DB_NAME)
    MasterBase.metadata.create_all(master_engine)  # master registry tables
    _upgrade_master_tables()
    _upgrade_tenant_databases()
    from app.services import gwcontrol
    gwcontrol.install_config_hook()  # saving a device/sensor in the app instantly tells the plant script to reload
    log.info("startup_complete", env=settings.ENVIRONMENT)
    yield


app = FastAPI(title=f"{settings.APP_NAME} API", version=settings.APP_VERSION, lifespan=lifespan,
              docs_url="/api/v1/docs", openapi_url="/api/v1/openapi.json", redoc_url=None)
app.state.limiter = limiter

app.add_exception_handler(errors.AppError, errors.app_error_handler)
app.add_exception_handler(StarletteHTTPException, errors.http_exception_handler)
app.add_exception_handler(RequestValidationError, errors.validation_handler)
app.add_exception_handler(RateLimitExceeded, errors.rate_limit_handler)
app.add_exception_handler(Exception, errors.unhandled_handler)

app.add_middleware(SlowAPIMiddleware)
app.add_middleware(TimezoneMiddleware)   # the viewer's own time zone for reports (header X-Tz-Offset)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    # Flutter Web dev server picks random localhost ports; only allowed in development.
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$" if settings.is_dev else None,
    allow_credentials=False,  # Bearer tokens, not cookies
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Tz-Offset"],
    expose_headers=["Content-Disposition", "X-Renewed-Token"],
    max_age=600,
)


@app.middleware("http")
async def renew_session(request: Request, call_next):
    """Sliding session: while a person keeps using the app, a normal (30-minute) sign-in is quietly extended.
    After a SUCCESSFUL call with a token that has less than half its time left, the reply carries X-Renewed-Token; the app swaps it in.
    'Keep me signed in' tokens (30 days) are left alone, and a token that was revoked or expired never gets here (the call failed)."""
    response = await call_next(request)
    auth = request.headers.get("authorization", "")
    if response.status_code < 400 and auth.lower().startswith("bearer ") and not request.url.path.endswith(("/auth/logout", "/auth/login")):
        try:
            from app.core.security import renewed_token
            new = renewed_token(auth[7:])
            if new:
                response.headers["X-Renewed-Token"] = new
        except Exception:
            pass
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Strict browser-side protections on every reply (the API is JSON, so nothing may be framed, sniffed or cached)."""
    response = await call_next(request)
    h = response.headers
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "no-referrer"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    if request.url.path.startswith(settings.API_PREFIX):   # JSON API: nothing to load, frame or cache (the reset-password page has its own inline script)
        h["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        h["Cache-Control"] = "no-store"
    else:
        h["Content-Security-Policy"] = "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"
    if not settings.is_dev:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.middleware("http")
async def access_log(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    log.info("request", method=request.method, path=request.url.path, status=response.status_code,
             ms=round((time.perf_counter() - start) * 1000, 1),
             ip=request.client.host if request.client else None)
    return response


app.include_router(api_router, prefix=settings.API_PREFIX)
app.include_router(pages_router)


if __name__ == "__main__":  # python app\main.py
    import uvicorn

    # inside Docker only; the compose file publishes the port on 127.0.0.1
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")),  # nosec B104
                proxy_headers=True, forwarded_allow_ips="*", access_log=False)
