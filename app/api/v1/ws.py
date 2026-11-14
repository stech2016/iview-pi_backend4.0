"""Live telemetry/alarm stream.

Auth: JWT is sent via the `Sec-WebSocket-Protocol` header as  ["stech.v1", "<jwt>"]
or, as a fallback, in a first message {"type":"auth","token":"<jwt>"}. NEVER in the query string.
Client sends {"type":"subscribe","device_id":N} to receive the readings of that one device (without it, no readings are sent - only alarms/commands).
"""
import asyncio
import json

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.security import decode_token
from app.db.session import MasterSession
from app.deps import user_from_token
from app.services.live import channel

router = APIRouter(tags=["live"])
log = get_logger("ws")


def _authenticate(token: str):
    try:
        with MasterSession() as master:
            user = user_from_token(token, master)
            return {"id": user.id, "company": user.company_name, "role": user.role,
                    "state": user.state, "region": user.region, "exp": decode_token(token)["exp"]}
    except AppError:
        return None


@router.websocket("/ws/live")
async def live(ws: WebSocket):
    offered = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    token = offered[1] if len(offered) > 1 and offered[0] == "stech.v1" else None
    await ws.accept(subprotocol="stech.v1" if offered and offered[0] == "stech.v1" else None)

    if not token:
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=5)
            token = first.get("token") if first.get("type") == "auth" else None
        except Exception:
            token = None
    ctx = await run_in_threadpool(_authenticate, token) if token else None
    if not ctx:
        log.warning("ws_auth_failed")
        await ws.close(code=4401)
        return

    await ws.send_json({"type": "ready"})
    flt = {"device_id": None}
    r = aioredis.Redis.from_url(settings.REDIS_URL)
    pubsub = r.pubsub()
    try:
        await pubsub.subscribe(channel(ctx["company"]))
    except Exception as exc:  # Redis not running: the app keeps working by polling, so close quietly (no traceback)
        log.warning("live_feed_unavailable", error=type(exc).__name__)
        await r.aclose()
        await ws.close(code=1013)
        return

    def visible(ev: dict) -> bool:
        if ctx["role"] != "admin" and ev.get("state") != "ALL" and (ev.get("state") != ctx["state"] or ev.get("region") != ctx["region"]):
            return False
        # Readings are sent ONLY for the one device the app has opened ({"type":"subscribe","device_id":N}); alarms and commands go to everybody.
        # Before, every phone received every reading of the whole plant (hundreds per second on a big plant) and threw them away.
        if ev.get("type") == "telemetry" and (flt["device_id"] is None or ev.get("device_id") != flt["device_id"]):
            return False
        return True

    async def pump():
        async for msg in pubsub.listen():
            if msg.get("type") != "message":
                continue
            ev = json.loads(msg["data"])
            if visible(ev):
                ev.pop("state", None), ev.pop("region", None)
                await ws.send_json(ev)

    async def receive():
        while True:
            m = await ws.receive_json()
            if m.get("type") == "subscribe":
                flt["device_id"] = m.get("device_id")

    async def expire():
        from app.core.timeutil import utcnow
        await asyncio.sleep(max(1, ctx["exp"] - utcnow().timestamp()))
        await ws.close(code=4401)

    tasks = [asyncio.create_task(t()) for t in (pump, receive, expire)]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await pubsub.close()
        await r.aclose()
