"""STGW4.0 gateway box: the one-time SETUP CODE.

The Super admin / Regional admin presses "Setup code" for a gateway in the app and gets a short code such as K7M4-92QX.
The person at the plant types it on the box's own setup page. The box swaps the code for its connection details (what link.json
holds) over https, so the long secret key is never typed, e-mailed or shown on a screen.

A code works ONCE, expires (ENROLL_CODE_MINUTES) and a new code for the same gateway cancels the older one.
Codes live in Valkey / Redis (only a hash of the code is stored), so nothing about them reaches the database or the logs.
"""
import hashlib
import json
import secrets

import redis

from app.core.config import settings
from app.core.errors import AppError

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0 O 1 I L: nothing to mix up when it is read out or typed
CODE_LEN = 8
_client: redis.Redis | None = None


def _r() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=2, decode_responses=True)
    return _client


def normalize(code: str) -> str:
    """Accepts k7m4-92qx, K7M4 92QX, ' k7m492qx ' ... and returns K7M492QX."""
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def pretty(code: str) -> str:
    return f"{code[:CODE_LEN // 2]}-{code[CODE_LEN // 2:]}"


def _key(code: str) -> str:
    return "stgw:enroll:" + hashlib.sha256(normalize(code).encode()).hexdigest()


def _latest_key(company: str, gid: str) -> str:
    return "stgw:enroll:latest:" + hashlib.sha256(f"{company}\n{gid}".encode()).hexdigest()


def make_code(company: str, gid: str) -> tuple[str, int]:
    """A new code for ONE gateway. Returns (code as shown, minutes it stays valid). Older codes of this gateway stop working."""
    minutes = max(1, int(settings.ENROLL_CODE_MINUTES))
    code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    try:
        r = _r()
        old = r.get(_latest_key(company, gid))
        if old:
            r.delete(old)
        r.set(_key(code), json.dumps({"c": company, "g": gid}), ex=minutes * 60)
        r.set(_latest_key(company, gid), _key(code), ex=minutes * 60)
    except redis.RedisError:
        raise AppError(503, "CODES_UNAVAILABLE", "Setup codes are not available right now (the live-data service is not answering). Try again in a minute.")
    return pretty(code), minutes


def redeem(code: str) -> tuple[str, str] | None:
    """Uses the code up. Returns (company, gateway id), or None when it is wrong, already used or expired."""
    norm = normalize(code)
    if len(norm) != CODE_LEN or any(ch not in ALPHABET for ch in norm):
        return None
    try:
        raw = _r().getdel(_key(norm))
    except redis.RedisError:
        raise AppError(503, "CODES_UNAVAILABLE", "Setup codes are not available right now. Try again in a minute.")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data["c"], data["g"]
    except (ValueError, KeyError):
        return None
