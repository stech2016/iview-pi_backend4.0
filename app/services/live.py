"""Redis pub/sub bridge feeding the WebSocket endpoint."""
import json

import redis

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("live")
_client: redis.Redis | None = None


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    return _client


def channel(company: str) -> str:
    return f"live:{company}"


def publish(company: str, event: dict) -> None:
    try:
        _redis().publish(channel(company), json.dumps(event, default=str))
    except Exception as exc:
        log.warning("live_publish_failed", error=type(exc).__name__)


def publish_many(company: str, events: list[dict]) -> None:
    """Many events in ONE round trip to Redis (a batch of readings used to cost one trip per reading)."""
    if not events:
        return
    try:
        pipe = _redis().pipeline(transaction=False)
        for e in events:
            pipe.publish(channel(company), json.dumps(e, default=str))
        pipe.execute()
    except Exception as exc:
        log.warning("live_publish_failed", error=type(exc).__name__)
