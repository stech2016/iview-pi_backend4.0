import socket
from urllib.parse import urlparse

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings


def _redis_reachable() -> bool:
    """One quick check at startup: without Redis, keep the rate-limit counters in memory instead of waiting on it."""
    try:
        u = urlparse(settings.REDIS_URL)
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 6379), timeout=1):
            return True
    except OSError:
        return False


# Redis-backed counters when Redis is running (they hold across several server processes);
# otherwise in-memory counters, which are fine for a single backend and make login/forgot-password fast.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.REDIS_URL if _redis_reachable() else "memory://",
    default_limits=["300/minute"],
    in_memory_fallback_enabled=True,
    swallow_errors=True,
)
