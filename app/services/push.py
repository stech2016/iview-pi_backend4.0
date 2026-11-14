"""Phone notifications (Firebase Cloud Messaging, HTTP v1). Free. Off until FCM_SERVICE_ACCOUNT_B64 is set in .env.

  notify(company, user_ids, title, body, data)   sends in the background; never slows or breaks alarm handling.
  A phone token that Google says is dead (app uninstalled) is removed from the database.
"""
import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("push")

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
CHANNEL = "stech_alarms"          # the Android channel the app creates (high importance = pop-up on screen)

_lock = threading.Lock()
_cache: dict = {"token": None, "exp": 0.0}


def enabled() -> bool:
    return bool(settings.FCM_SERVICE_ACCOUNT_B64.strip())


def _account() -> dict:
    return json.loads(base64.b64decode(settings.FCM_SERVICE_ACCOUNT_B64.strip()).decode())


def _post(url: str, data: bytes, headers: dict) -> dict:
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
        raw = resp.read()
    return json.loads(raw.decode()) if raw else {}


def _access_token() -> str:
    """Google OAuth token made from the service-account key (cached for ~55 minutes)."""
    with _lock:
        if _cache["token"] and time.time() < _cache["exp"] - 60:
            return _cache["token"]
        acc = _account()
        uri = settings.FCM_TOKEN_URL or acc.get("token_uri") or "https://oauth2.googleapis.com/token"
        now = int(time.time())
        assertion = jwt.encode({"iss": acc["client_email"], "scope": SCOPE, "aud": uri, "iat": now, "exp": now + 3000},
                               acc["private_key"], algorithm="RS256")
        out = _post(uri, urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}).encode(),
            {"content-type": "application/x-www-form-urlencoded"})
        _cache["token"], _cache["exp"] = out["access_token"], time.time() + int(out.get("expires_in", 3000))
        return _cache["token"]


def send_one(token: str, title: str, body: str, data: dict | None = None) -> str:
    """Returns 'ok', 'dead' (this phone token will never work again) or 'error'."""
    msg = {"token": token, "notification": {"title": title, "body": body},
           "data": {k: str(v) for k, v in (data or {}).items()},
           "android": {"priority": "HIGH", "notification": {"channel_id": CHANNEL, "sound": "default",
                                                              "notification_priority": "PRIORITY_MAX", "visibility": "PUBLIC"}},
           # iPhone (APNs): alert at once, with sound. Needs the APNs key uploaded in Firebase (see README, iPhone section).
           "apns": {"headers": {"apns-priority": "10"}, "payload": {"aps": {"sound": "default"}}}}
    url = f"{settings.FCM_API_URL.rstrip('/')}/v1/projects/{_account()['project_id']}/messages:send"
    try:
        _post(url, json.dumps({"message": msg}).encode(),
              {"authorization": f"Bearer {_access_token()}", "content-type": "application/json"})
        return "ok"
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()[:400]
        except Exception:
            pass
        if exc.code == 404 or (exc.code == 400 and "UNREGISTERED" in detail) or "registration-token-not-registered" in detail:
            return "dead"
        if exc.code in (401, 403):
            with _lock:
                _cache["token"] = None
        log.warning("push_failed", status=exc.code, detail=detail[:200])
        return "error"
    except Exception as exc:
        log.warning("push_failed", error=type(exc).__name__)
        return "error"


def _run(company: str, user_ids: list[int], title: str, body: str, data: dict) -> None:
    from app.db.session import MasterSession, tenant_session
    from app.models.tenant import FcmToken
    try:
        with MasterSession() as master, tenant_session(master, company) as db:
            rows = list(db.execute(select(FcmToken.id, FcmToken.token).where(FcmToken.user_id.in_(user_ids))))
            dead = [rid for rid, tok in rows if send_one(tok, title, body, data) == "dead"]
            if dead:
                db.execute(delete(FcmToken).where(FcmToken.id.in_(dead)))
                db.commit()
    except Exception as exc:
        log.warning("push_skipped", error=type(exc).__name__)


def notify(company: str, user_ids: list[int], title: str, body: str, data: dict | None = None) -> None:
    if not enabled() or not user_ids:
        return
    threading.Thread(target=_run, args=(company, list(user_ids), title, body[:300], data or {}), daemon=True).start()


def alarm_title(severity: str, device_name: str | None) -> str:
    return f"{severity.upper()} alarm - {device_name or 'STECH iVIEW'}"
