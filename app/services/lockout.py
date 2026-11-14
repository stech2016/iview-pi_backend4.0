"""Login lockout (brute-force protection), on top of the per-IP rate limit.

5 wrong passwords for one username from one address lock that pair for 15 minutes; 20 wrong passwords for one
username from anywhere lock the username for 15 minutes. A correct login clears the counters.
Counters live in Valkey (shared by all workers) and fall back to memory if Valkey is down.
"""
import time

import redis

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger

log = get_logger("lockout")
WINDOW = 15 * 60
PER_IP, PER_USER = 5, 20
_client: redis.Redis | None = None
_mem: dict[str, tuple[int, float]] = {}


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    return _client


def _keys(ident: str, ip: str | None) -> tuple[str, str]:
    return f"lock:ip:{ident}:{ip}", f"lock:user:{ident}"


def _get(key: str) -> int:
    try:
        v = _redis().get(key)
        return int(v) if v else 0
    except Exception:
        n, exp = _mem.get(key, (0, 0.0))
        return n if exp > time.time() else 0


def _incr(key: str) -> None:
    try:
        p = _redis().pipeline()
        p.incr(key)
        p.expire(key, WINDOW)
        p.execute()
    except Exception:
        n, exp = _mem.get(key, (0, 0.0))
        _mem[key] = ((n + 1) if exp > time.time() else 1, time.time() + WINDOW)


def check(ident: str, ip: str | None) -> None:
    a, b = _keys(ident, ip)
    if _get(a) >= PER_IP or _get(b) >= PER_USER:
        log.warning("login_locked", ip=ip)
        raise AppError(429, "ACCOUNT_LOCKED", "Too many wrong passwords. Wait 15 minutes, or use 'Forgot password'.")


def failed(ident: str, ip: str | None) -> None:
    for k in _keys(ident, ip):
        _incr(k)


def clear(ident: str, ip: str | None) -> None:
    a, _ = _keys(ident, ip)
    _mem.pop(a, None)
    try:
        _redis().delete(a)
    except Exception:
        pass
