"""Last time each device answered, shared by every backend process through Valkey (Redis).

The ingest worker and the API run as separate processes (and the API has two workers), so a plain in-memory dict in
one of them is invisible to the others. That made a steady device look OFFLINE after logging out and in again:
unchanged readings are not stored, so the stored time alone stops moving. Valkey is shared by all of them, so the
Devices list shows the same 'last seen' for everyone. Falls back to the in-process copy if Valkey is unreachable.
"""
from datetime import datetime

import redis

from app.core.config import settings
from app.core.logging import get_logger
from app.core.timeutil import as_utc, utcnow

log = get_logger("lastseen")
KEEP_SECONDS = 400 * 24 * 3600                     # a little over one year, like the dashboard's look-back
_client: redis.Redis | None = None
_local: dict[tuple[str, int], datetime] = {}       # (company, device_id) -> last answer, this process only


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    return _client


def _key(company: str) -> str:
    return f"lastseen:{company}"


def _parse(value) -> datetime | None:
    try:
        text = value.decode() if isinstance(value, bytes) else str(value)
        return as_utc(datetime.fromisoformat(text))
    except (ValueError, AttributeError):
        return None


def record(company: str, device_id: int, ts: datetime | None = None) -> None:
    """The device answered just now (called after every accepted reading, changed or not)."""
    ts = ts or utcnow()
    _local[(company, device_id)] = ts
    try:
        pipe = _redis().pipeline()
        pipe.hset(_key(company), str(device_id), ts.isoformat())
        pipe.expire(_key(company), KEEP_SECONDS)
        pipe.execute()
    except Exception as exc:
        log.warning("lastseen_write_failed", error=type(exc).__name__)


def get_all(company: str) -> dict[int, datetime]:
    """device_id -> last answer, for every device of the company."""
    out = {dev: ts for (comp, dev), ts in _local.items() if comp == company}
    try:
        for key, value in _redis().hgetall(_key(company)).items():
            ts = _parse(value)
            try:
                dev = int(key)
            except (ValueError, TypeError):
                continue
            if ts and (dev not in out or ts > out[dev]):
                out[dev] = ts
    except Exception as exc:
        log.warning("lastseen_read_failed", error=type(exc).__name__)
    return out


def get(company: str, device_id: int) -> datetime | None:
    best = _local.get((company, device_id))
    try:
        ts = _parse(_redis().hget(_key(company), str(device_id)))
        if ts and (best is None or ts > best):
            best = ts
    except Exception as exc:
        log.warning("lastseen_read_failed", error=type(exc).__name__)
    return best
