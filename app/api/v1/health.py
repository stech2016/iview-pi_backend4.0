import socket

import redis
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import settings
from app.db.session import master_engine

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz():
    return {"status": "ok"}


@router.get("/app-info")
def app_info():
    """Open to everyone, holds nothing secret. The app (or a setup page) can use it to see which edition it reached and
    whether it is new enough."""
    links = {k: v for k, v in {"windows": settings.APP_DOWNLOAD_WINDOWS, "android": settings.APP_DOWNLOAD_ANDROID, "ios": settings.APP_DOWNLOAD_IOS,
                               "macos": settings.APP_DOWNLOAD_MACOS, "linux": settings.APP_DOWNLOAD_LINUX}.items() if v.startswith("https://")}
    return {"name": settings.APP_NAME, "api_version": 1, "version": settings.APP_VERSION, "min_app_version": settings.MIN_APP_VERSION,
            "latest_app_version": settings.APP_LATEST_VERSION or settings.APP_VERSION, "download_urls": links}


@router.get("/readyz")
def readyz():
    checks = {}
    try:
        with master_engine.connect() as c:
            c.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "down"
    try:
        redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2).ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "down"
    try:
        socket.create_connection((settings.MQTT_HOST, settings.MQTT_PORT), timeout=2).close()
        checks["mqtt"] = "ok"
    except Exception:
        checks["mqtt"] = "down"
    ready = all(v == "ok" for v in checks.values())
    return JSONResponse(status_code=200 if ready else 503, content={"status": "ready" if ready else "not_ready", "checks": checks})
