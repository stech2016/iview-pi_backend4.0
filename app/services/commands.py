"""Writing to the plant: one command from the app to a PLC and back.

    app --(server checks)--> MQTT command (signed, with an expiry) --> plant PC writes the coil / register --> reads it back
        --> acknowledgement (signed) --> cloud decides confirmed / failed --> live update to the app

States:  awaiting (only when 2nd-person approval is on) -> sent -> delivered -> written -> confirmed      or  failed / timed_out / expired
  sent       the cloud published the command                       delivered  the plant PC received it and checked it
  written    the plant PC wrote it and read the value back         confirmed  the value read back equals the value asked for
  failed     refused, error, or the PLC did not keep the value     expired    nobody received it before its expiry (it is never run later)
  timed_out  received but no answer in time
A command that expired is never replayed: the plant PC refuses a command whose expiry time has passed, whatever the broker does.
"""
import json
import math
import secrets
import threading
import time
from datetime import timedelta

import redis
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.timeutil import as_utc, utcnow
from app.db.session import MasterSession, tenant_session
from app.models.tenant import Command, Device, DeviceRegister
from app.services import audit, csettings, live
from app.services import gateway as gw

log = get_logger("commands")
FINAL = ("confirmed", "failed", "timed_out", "expired", "rejected")
ORDER = {"awaiting": -1, "sent": 0, "delivered": 1, "written": 2}
PULSE_SECONDS = 1.0             # a pulse command (fault reset) holds the bit on for this long
GRACE_SECONDS = 5               # after the expiry the plant PC still has this long to report back
INT_RANGE = {"int16": (-32768, 32767), "uint16": (0, 65535), "int32": (-2**31, 2**31 - 1), "uint32": (0, 2**32 - 1),
             "int64": (-2**63, 2**63 - 1), "uint64": (0, 2**64 - 1)}
_client: redis.Redis | None = None


def _r() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    return _client


def command_dict(c: Command) -> dict:
    return {"id": c.id, "uid": c.uid, "device_id": c.device_id, "device_name": c.device_name, "register_id": c.register_id,
            "metric_name": c.metric_name, "unit": c.unit, "value": c.value, "previous_value": c.previous_value, "readback": c.readback,
            "state": c.state, "error": c.error, "requested_by": c.requested_by, "reason": c.reason, "pulse": c.pulse, "approved_by": c.approved_by, "customer_gateway": bool(c.gateway_id and c.gateway_id.startswith("ext:")), "created_at": c.created_at.isoformat(),
            "expires_at": c.expires_at.isoformat(),
            "delivered_at": c.delivered_at.isoformat() if c.delivered_at else None,
            "written_at": c.written_at.isoformat() if c.written_at else None,
            "finished_at": c.finished_at.isoformat() if c.finished_at else None}


def to_raw(reg: DeviceRegister, value: float) -> float:
    """Engineering value -> the number written to the PLC (value / scale factor). Refuses what the data type cannot hold."""
    if not math.isfinite(value):
        raise AppError(422, "VALUE_INVALID", "Enter a number.")
    is_bit = reg.data_type == "bool" or reg.register_type == "coil"
    if is_bit:
        if value not in (0, 1, 0.0, 1.0):
            raise AppError(422, "VALUE_NOT_BIT", "This is an on / off point: send 0 (off) or 1 (on).")
        return float(value)
    if reg.write_min is None or reg.write_max is None:
        raise AppError(409, "LIMITS_MISSING", "This sensor has no write limits yet. Ask an administrator to set them.")
    if not (reg.write_min <= value <= reg.write_max):
        raise AppError(422, "VALUE_OUT_OF_LIMITS", f"The value must be between {reg.write_min:g} and {reg.write_max:g}.")
    raw = value / (reg.scale_factor or 1.0)
    if reg.data_type in INT_RANGE:
        whole = round(raw)
        if abs(raw - whole) > 1e-6 * max(1.0, abs(raw)):
            raise AppError(422, "VALUE_STEP", f"This sensor stores whole steps of {reg.scale_factor:g}. Use a multiple of it.")
        lo, hi = INT_RANGE[reg.data_type]
        if not lo <= whole <= hi:
            raise AppError(422, "VALUE_TYPE_RANGE", f"The value does not fit the data type {reg.data_type}.")
        return float(whole)
    return float(raw)


def cmd_text(uid: str, dev: int, reg: int, raw: float, exp: str) -> str:
    return f"cmd|{uid}|{dev}|{reg}|{float(raw)!r}|{exp}"


def ack_text(uid: str, stage: str, readback, error: str) -> str:
    return f"ack|{uid}|{stage}|{(float(readback) if readback is not None else None)!r}|{error}"


def new_uid() -> str:
    return secrets.token_hex(12)


def publish(company: str, gid: str, cmd: Command, device: Device, reg: DeviceRegister) -> None:
    """Signs the command with the key of ITS gateway and sends it (QoS 1, not retained: a plant PC that was off never gets it later)."""
    from app.services import extgw, gwcontrol
    if extgw.is_ext_gid(gid):          # V2.0: a customer gateway (any brand): an ordinary MQTT message, confirmed by the next reading
        extgw.publish_command(company, cmd, device, reg)
        return
    info = gw.get_info(company, gid)
    if not info:
        raise AppError(409, "GATEWAY_NOT_FOUND", "The plant PC of this device was not found.")
    topic = f"stech/{company}/gw/{gid}/cmd"
    ts = utcnow().isoformat(timespec="milliseconds").replace("+00:00", "Z")
    exp = as_utc(cmd.expires_at).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    body = {"uid": cmd.uid, "dev": device.id, "reg": reg.id, "raw": cmd.raw_value, "exp": exp, "ts": ts,
            "sig": gw.sign_text(info.secret, topic, ts, cmd_text(cmd.uid, device.id, reg.id, cmd.raw_value, exp))}
    if not gwcontrol.publish_message(topic, json.dumps(body, separators=(",", ":"))):
        raise AppError(503, "COMMAND_CHANNEL_DOWN", "The command channel to the plants is not connected right now. Nothing was sent. Try again in a moment.")


def track(company: str, cmd: Command) -> None:
    """Remember when the command expires so the watchdog can close it (Valkey sorted set)."""
    try:
        _r().zadd("cmdexp", {f"{company}:{cmd.uid}": as_utc(cmd.expires_at).timestamp() + GRACE_SECONDS})
    except Exception as exc:
        log.warning("command_track_failed", error=type(exc).__name__)


def untrack(company: str, uid: str) -> None:
    try:
        _r().zrem("cmdexp", f"{company}:{uid}")
    except Exception:
        pass


def _announce(company: str, c: Command) -> None:
    live.publish(company, {"type": "command", "command": command_dict(c), "state": c.state_name or "ALL", "region": c.region or "ALL"})


def _finish(db, company: str, c: Command, state: str, error: str | None = None, readback: float | None = None) -> None:
    c.state, c.error, c.finished_at = state, (error or None) and error[:200], utcnow()
    if readback is not None:
        c.readback = readback
    db.commit()
    untrack(company, c.uid)
    audit.append(db, c.requested_by_id, company, f"control.{state}",
                 f"device:{c.device_id}/{c.metric_name}={c.value:g}" + (f" readback={c.readback:g}" if c.readback is not None else "")
                 + (f" ({error[:80]})" if error else ""), None)
    # FUTURE OUTBOUND CONNECTOR (write finished: confirmed / failed): add ONE call here, e.g. outbound.send("command.finished", payload), so an ERP / MES / webhook receives this.
    # Nothing is sent today. Keep it behind a company setting and a timeout so a slow receiver can never block the plant data.
    _announce(company, c)
    log.info("command_finished", company=company, state=state)


def handle_ack(company: str, gid: str, topic: str, payload: bytes) -> None:
    """The plant PC reports: delivered / written (with the value read back) / failed. Unsigned or foreign messages are ignored."""
    try:
        body = json.loads(payload.decode())
        uid, stage, ts, sig = str(body["uid"]), str(body["stage"]), str(body["ts"]), str(body["sig"])
        readback = body.get("readback")
        readback = float(readback) if readback is not None else None
        error = str(body.get("error") or "")[:200]
    except (ValueError, KeyError, TypeError):
        return
    if stage not in ("delivered", "written", "failed") or not gw.verify_text(company, gid, topic, ts, ack_text(uid, stage, readback, error), sig):
        log.warning("command_ack_rejected", company=company, gateway=gid)
        return
    try:
        with MasterSession() as master, tenant_session(master, company) as db:
            c = db.scalar(select(Command).where(Command.uid == uid))
            if not c or c.gateway_id != gid or c.state in FINAL:
                return    # unknown, from the wrong plant PC, or already closed (for example expired): nothing to do
            now = utcnow()
            if stage == "delivered":
                if ORDER[c.state] < 1:
                    c.state, c.delivered_at = "delivered", now
                    db.commit()
                    _announce(company, c)
            elif stage == "failed":
                _finish(db, company, c, "failed", error or "The plant PC could not write the value.")
            else:   # written: the plant PC wrote it and read the PLC back
                reg = db.get(DeviceRegister, c.register_id)
                scale = (reg.scale_factor if reg else 1.0) or 1.0
                back = readback * scale if readback is not None else None
                c.state, c.written_at = "written", now
                if c.delivered_at is None:
                    c.delivered_at = now
                if back is not None:
                    c.readback = back
                db.commit()
                _announce(company, c)
                tol = csettings.get(company, "write_confirm_tolerance_pct") / 100.0
                if back is not None and abs(back - c.value) <= max(1e-9, tol * abs(c.value), 1e-6 * (reg.scale_factor if reg else 1.0)):
                    _finish(db, company, c, "confirmed", None, back)
                    if c.pulse and c.value == 1:
                        threading.Timer(PULSE_SECONDS, release_pulse, (company, c.uid)).start()
                else:
                    shown = "nothing" if back is None else f"{back:g}"
                    _finish(db, company, c, "failed", f"The PLC reports {shown} after the write, not {c.value:g}. It may have limited or refused the value.", back)
    except Exception as exc:
        log.warning("command_ack_failed", company=company, error=type(exc).__name__)


EXT_TIMEOUT = ("The gateway did not report the new value in time. It may not accept writes, or its command topic is not mapped to the PLC "
               "address yet (see the Integration Manual). Check the value on the plant.")


def _close_late(db, company: str, c: Command) -> None:
    """Past its time and still open: a plant PC command that nobody received expired; one that was received but never reported timed out.
    A command to a customer gateway has no 'received' step: it is confirmed by the next reading, so it times out."""
    if c.gateway_id and c.gateway_id.startswith("ext:"):
        _finish(db, company, c, "timed_out", EXT_TIMEOUT)
    elif c.state == "sent":
        _finish(db, company, c, "expired", "No plant PC received it in time, so it was cancelled. It will not be run later.")
    else:
        _finish(db, company, c, "timed_out", "The plant PC received it but did not report back in time. Check the value on the plant.")


def expire_due() -> None:
    """Closes commands whose time ran out: never delivered -> expired, delivered but never reported -> timed_out. Runs every second."""
    try:
        due = _r().zrangebyscore("cmdexp", "-inf", time.time())
    except Exception:
        return
    for member in due:
        company, _, uid = member.decode().partition(":")
        try:
            with MasterSession() as master, tenant_session(master, company) as db:
                c = db.scalar(select(Command).where(Command.uid == uid))
                if c and c.state not in FINAL:
                    _close_late(db, company, c)
                else:
                    untrack(company, uid)
        except Exception as exc:
            log.warning("command_expire_failed", company=company, error=type(exc).__name__)
            untrack(company, uid)


def resolve_late(db, company: str, rows: list[Command]) -> None:
    """When the list is read: anything past its time that the watchdog did not close yet (Valkey down) is closed now."""
    now = utcnow()
    for c in rows:
        if c.state in FINAL or now <= as_utc(c.expires_at) + timedelta(seconds=GRACE_SECONDS):
            continue
        _close_late(db, company, c)


def release_pulse(company: str, uid: str) -> None:
    """Second half of a pulse (fault reset): writes 0 back to the same point. Same signing, same plant PC checks. Runs 1 s after the 1 was confirmed."""
    try:
        with MasterSession() as master, tenant_session(master, company) as db:
            first = db.scalar(select(Command).where(Command.uid == uid))
            if not first:
                return
            device, reg = db.get(Device, first.device_id), db.get(DeviceRegister, first.register_id)
            if not device or not reg:
                return
            now = utcnow()
            cmd = Command(uid=new_uid(), device_id=device.id, device_name=device.device_name, register_id=reg.id, metric_name=reg.metric_name,
                          unit=reg.unit, value=0, raw_value=0.0, previous_value=1, state="sent", requested_by_id=first.requested_by_id,
                          requested_by=first.requested_by, gateway_id=first.gateway_id, state_name=first.state_name, region=first.region,
                          created_at=now, expires_at=now + timedelta(seconds=csettings.get(company, "command_expiry_seconds")),
                          reason=f"Pulse end (automatic) - {first.reason or ''}"[:200])
            db.add(cmd)
            db.commit()
            db.refresh(cmd)
            publish(company, cmd.gateway_id, cmd, device, reg)
            track(company, cmd)
            _announce(company, cmd)
    except Exception as exc:
        log.warning("pulse_release_failed", company=company, error=type(exc).__name__)
