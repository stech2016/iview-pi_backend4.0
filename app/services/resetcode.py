"""Short 6-digit codes (easy to type on a phone): "forgot password" (RESET_CODE_MINUTES) and e-mail activation (ACTIVATION_CODE_MINUTES).
Kept in Valkey, 5 tries, works once. `purpose` keeps the two kinds apart."""
import hashlib
import hmac
import json
import secrets

import redis

from app.core.config import settings

MAX_TRIES = 5
_client: redis.Redis | None = None


def _r() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
    return _client


def _key(user_id: int, purpose: str) -> str:
    return f"{purpose}:{user_id}"


def _digest(user_id: int, code: str, purpose: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"{purpose}:{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


def issue(user_id: int, purpose: str = "pwreset", minutes: int | None = None) -> str | None:
    """A new code (the old one stops working). None when Valkey is not reachable."""
    try:
        code = f"{secrets.randbelow(10**6):06d}"
        _r().setex(_key(user_id, purpose), (minutes or settings.RESET_CODE_MINUTES) * 60,
                   json.dumps({"h": _digest(user_id, code, purpose), "tries": 0}))
        return code
    except Exception:
        return None


def check(user_id: int, code: str, purpose: str = "pwreset") -> bool:
    """True once for the right code. Wrong guesses are counted; after MAX_TRIES the code is thrown away."""
    try:
        raw = _r().get(_key(user_id, purpose))
        if not raw:
            return False
        rec = json.loads(raw)
        if rec["tries"] >= MAX_TRIES:
            _r().delete(_key(user_id, purpose))
            return False
        if hmac.compare_digest(rec["h"], _digest(user_id, code.strip(), purpose)):
            _r().delete(_key(user_id, purpose))
            return True
        rec["tries"] += 1
        ttl = max(_r().ttl(_key(user_id, purpose)), 1)
        _r().setex(_key(user_id, purpose), ttl, json.dumps(rec))
        return False
    except Exception:
        return False


def clear(user_id: int, purpose: str = "pwreset") -> None:
    """The password was changed (by the link or the code): any unused code is thrown away."""
    try:
        _r().delete(_key(user_id, purpose))
    except Exception:
        pass
