"""MQTT subscriber: topics mapped in the device register table (stech/<company>/...)  ->  telemetry.

Run:  python -m app.workers.mqtt_ingest
Payload: plain number, or JSON where the register's "JSON field" (e.g. temp or data.temp) holds the value.
"""
import json
import multiprocessing
import os
import queue
import threading
import time
from datetime import datetime

import paho.mqtt.client as mqtt
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger
from app.db.session import MasterSession, tenant_session
from app.models.tenant import Alarm, AlertRule, Device, DeviceRegister
from app.services import gateway as gw
from app.services import commands, extgw, gwcontrol, jobs
from app.services.ingest import ingest_readings

configure_logging(settings.LOG_LEVEL)
log = get_logger("mqtt_ingest")


def on_connect(client, userdata, flags, reason_code, properties=None):
    log.info("mqtt_connected", reason=str(reason_code))
    client.subscribe("stech/#", qos=1)
    client.subscribe("iview/#", qos=1)          # V2.0: customer gateways, JSON
    client.subscribe("spBv1.0/#", qos=1)        # V2.0: customer gateways, Sparkplug B


def _pick(body, field):
    """Value from a plain number or from a (possibly dotted) field of a JSON message."""
    if field:
        for part in field.split("."):
            body = body[part]
    elif isinstance(body, dict):
        body = body["value"]
    return float(body)


def _on_gateway_message(company: str, gid: str, topic: str, register_id: int, payload: bytes) -> None:
    """stech/<company>/gw/<gateway>/r<sensor id>  (older scripts: stech/<company>/r<sensor id> = gateway 'main').
    payload {"value":..,"timestamp":"..","sig":".."}  (sig = HMAC with THAT gateway's own key)."""
    try:
        body = json.loads(payload.decode())
        value, ts_text, sig = float(body["value"]), str(body["timestamp"]), str(body["sig"])
    except (ValueError, KeyError, TypeError):
        log.warning("gateway_message_malformed", company=company)
        return
    if not gw.verify(company, gid, topic, ts_text, value, sig):
        log.warning("gateway_signature_rejected", company=company, gateway=gid)  # wrong / cancelled key: possible spoofing, never stored
        return
    ts = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
    with MasterSession() as master, tenant_session(master, company) as db:
        reg = db.get(DeviceRegister, register_id)
        device = db.get(Device, reg.device_id) if reg else None
        if not device or device.company_name != company:
            log.warning("gateway_unknown_sensor", company=company)
            return
        info = gw.get_info(company, gid)
        if not info or not info.allows(device.state, device.region, device.plant_pc):  # a gateway may only write its own State + Region
            log.warning("gateway_out_of_scope", company=company, gateway=gid)
            return
        ingest_readings(db, master, device, [(reg.metric_name, value, ts)])  # scaling is applied in ingest_readings


# ---------------------------------------------------------------------------------------------------------------------
# Plant readings arrive in large numbers (one message per sensor per second per plant). Saving them one by one cost about
# 8 ms each, so one worker fell behind at ~110 readings/s. The MQTT thread now only drops them into this queue; a batch
# thread collects what arrived in the last few milliseconds and saves ALL readings of one device with ONE database round
# trip (one session, one lookup, one commit). Result: the cost per reading falls several times and the delay stays tiny.
# ---------------------------------------------------------------------------------------------------------------------
BATCH_WINDOW = 0.05     # seconds to gather messages before saving them together
BATCH_MAX = 2000        # most messages saved in one round
_inbox: "queue.Queue[tuple]" = queue.Queue()


def _verify(items: list[tuple]) -> dict[str, list]:
    """items = (company, gateway id, topic, sensor id, payload). Every message is checked: signature of ITS gateway."""
    by_company: dict[str, list] = {}
    for item in items:
        company, gid, topic, rid, payload = item[:5]
        try:
            body = json.loads(payload.decode())
            value, ts_text, sig = float(body["value"]), str(body["timestamp"]), str(body["sig"])
        except (ValueError, KeyError, TypeError):
            log.warning("gateway_message_malformed", company=company)
            continue
        if not gw.verify(company, gid, topic, ts_text, value, sig):
            log.warning("gateway_signature_rejected", company=company, gateway=gid)
            continue
        try:
            ts = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
        except ValueError:
            log.warning("gateway_message_malformed", company=company)
            continue
        by_company.setdefault(company, []).append((gid, rid, value, ts))
    return by_company


def _store(company: str, rows: list) -> None:
    """Saves the verified readings of one company. Sensor belongs to the company, gateway allowed for that State + Region."""
    with MasterSession() as master, tenant_session(master, company) as db:
        regs = {r.id: r for r in db.scalars(select(DeviceRegister).where(DeviceRegister.id.in_({rid for _, rid, _, _ in rows})))}
        devices = {d.id: d for d in db.scalars(select(Device).options(selectinload(Device.registers)).where(
            Device.id.in_({r.device_id for r in regs.values()}), Device.company_name == company))}
        per_device: dict[int, list] = {}
        for gid, rid, value, ts in rows:
            reg = regs.get(rid)
            device = devices.get(reg.device_id) if reg else None
            if not device:
                log.warning("gateway_unknown_sensor", company=company)
                continue
            info = gw.get_info(company, gid)
            if not info or not info.allows(device.state, device.region, device.plant_pc):
                log.warning("gateway_out_of_scope", company=company, gateway=gid)
                continue
            per_device.setdefault(device.id, []).append((reg.metric_name, value, ts))
        # every device's open alarms and alert rules in 2 queries for the whole batch (was 2 queries per device)
        open_by, rules_by = {i: {} for i in per_device}, {i: [] for i in per_device}
        for a in db.scalars(select(Alarm).where(Alarm.device_id.in_(list(per_device)), Alarm.cleared_at.is_(None))):
            open_by[a.device_id][a.metric_name] = a
        for r in db.scalars(select(AlertRule).where(AlertRule.device_id.in_(list(per_device)), AlertRule.enabled.is_(True))):
            rules_by[r.device_id].append(r)
        for device_id, readings in per_device.items():
            try:
                ingest_readings(db, master, devices[device_id], readings, {"open": open_by[device_id], "rules": rules_by[device_id]})
            except AppError as exc:
                log.warning("mqtt_rejected", code=exc.code)
                db.rollback()


RETRY_PAUSE_MAX = 10    # seconds between tries while the database is not answering (1, 2, 4 ... up to this)


def _save_batch(items: list[tuple]) -> None:
    """The database may be busy or restarting: the readings are KEPT and tried again until they are stored (never thrown away).
    Saving the same readings twice is harmless (a reading is stored once per device + time + sensor)."""
    for company, rows in _verify(items).items():
        pause = 1.0
        while True:
            try:
                _store(company, rows)
                break
            except OperationalError as exc:
                log.warning("ingest_waiting_for_database", company=company, readings=len(rows), retry_in=pause,
                            detail=str(getattr(exc, "orig", exc))[:160])
                time.sleep(pause)
                pause = min(pause * 2, RETRY_PAUSE_MAX)
            except Exception as exc:   # one bad batch must never stop the worker
                log.warning("gateway_batch_failed", error=type(exc).__name__, detail=str(exc)[:160])
                break


def _gather(source) -> list[tuple]:
    """Waits for a message, then keeps collecting for BATCH_WINDOW seconds (or BATCH_MAX messages)."""
    items = [source.get()]
    deadline = time.monotonic() + BATCH_WINDOW
    while len(items) < BATCH_MAX:
        wait = deadline - time.monotonic()
        if wait <= 0:
            break
        try:
            items.append(source.get(timeout=wait))
        except queue.Empty:
            break
    return items


def _step(source, ack=None) -> None:
    """Take what arrived, store it, and ONLY THEN tell the broker it was received. If this program stops before that point
    (crash, power cut, restart) the broker still holds the readings and sends them again: nothing is lost, and saving a
    reading twice is harmless."""
    items = _gather(source)
    _save_batch(items)
    if ack:
        ack([i[5] for i in items if len(i) > 5 and i[5] is not None])


def batch_loop(source=None, ack=None) -> None:
    source = source or _inbox
    while True:
        try:
            _step(source, ack)
        except Exception as exc:
            log.warning("gateway_batch_failed", error=type(exc).__name__)


_client: "mqtt.Client | None" = None


def _ack_now(mids: list) -> None:
    for mid in mids:
        try:
            _client.ack(mid, 1)
        except Exception as exc:    # not connected right now: the broker sends the message again after the reconnect
            log.warning("mqtt_ack_failed", error=type(exc).__name__)


def ack_pump(ack_q) -> None:
    """Workers (other processes) report what they stored; the one MQTT connection lives here, so the confirmation is sent from here."""
    while True:
        _ack_now(ack_q.get())


# ---- several workers: a device always goes to the SAME worker (its "last value" memory and alert-rule timers live there)
def worker_count() -> int:
    try:
        want = int(os.environ.get("INGEST_WORKERS", "0"))
    except ValueError:
        want = 0
    if want > 0:
        return want
    # Readings wait on the database, so 2 workers per CPU keep every CPU busy - but every worker is a process of about 100 MB, so the
    # count is also limited by memory (one worker per 600 MB of RAM). A small server then runs one saver thread instead of starving.
    return max(1, min(16, (os.cpu_count() or 1) * 2, _memory_mb() // 600))


def _memory_mb() -> int:
    """Memory this program may use: the container limit if there is one, else the machine's RAM. Unknown -> a large number (no limit)."""
    best = 10 ** 9
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    best = int(line.split()[1]) // 1024
                    break
    except (OSError, ValueError, IndexError):
        pass
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path) as f:
                raw = f.read().strip()
            if raw.isdigit():
                best = min(best, int(raw) // (1024 * 1024))
        except (OSError, ValueError):
            pass
    return best


def _worker_main(q, ack_q) -> None:
    configure_logging(settings.LOG_LEVEL)
    batch_loop(q, lambda mids: ack_q.put(mids) if mids else None)


_DEVICE_OF: dict[tuple[str, int], int] = {}   # (company, sensor id) -> device id
_LAST_REFRESH: dict[str, float] = {}


def _device_of(company: str, rid: int) -> int:
    dev = _DEVICE_OF.get((company, rid))
    if dev is None and time.monotonic() - _LAST_REFRESH.get(company, 0) > 5:   # new sensor added in the app: look again (at most every 5 s)
        _LAST_REFRESH[company] = time.monotonic()
        try:
            with MasterSession() as master, tenant_session(master, company) as db:
                for r_id, d_id in db.execute(select(DeviceRegister.id, DeviceRegister.device_id)):
                    _DEVICE_OF[(company, r_id)] = d_id
        except Exception as exc:
            log.warning("sensor_map_failed", error=type(exc).__name__)
        dev = _DEVICE_OF.get((company, rid))
    return dev if dev is not None else rid


def dispatch_loop(queues: list, procs: list, ctx, ack_q) -> None:
    n = len(queues)
    while True:
        batch = _gather(_inbox)
        for i, proc in enumerate(procs):
            if not proc.is_alive():       # a worker that ever dies is started again, its waiting messages are kept
                log.warning("ingest_worker_restarted", worker=i)
                procs[i] = ctx.Process(target=_worker_main, args=(queues[i], ack_q), daemon=True)
                procs[i].start()
        for item in batch:
            queues[_device_of(item[0], item[3]) % n].put(item)


# ---- V2.0: what customer gateways (any brand) publish. One thread parses and stores it; a message is confirmed to the broker after that.
_ext_inbox: "queue.Queue[tuple]" = queue.Queue(maxsize=200000)


def ext_loop(source=None, ack=None) -> None:
    source = source or _ext_inbox
    while True:
        try:
            items = _gather(source)
            extgw.process([(i[0], i[1]) for i in items])
            if ack:
                ack([i[2] for i in items if i[2] is not None])
        except Exception as exc:
            log.warning("ext_batch_failed", error=type(exc).__name__)


def _route(msg) -> bool:
    """Topic stech/<company>/...  ->  every register of that company mapped to this exact topic.
    Returns True when the message was put in the queue for saving: it is confirmed to the broker later, after it is stored."""
    try:
        parts = msg.topic.split("/")
        if parts[0] in ("iview", "spBv1.0"):
            try:
                _ext_inbox.put_nowait((msg.topic, msg.payload, msg.mid if msg.qos > 0 else None))
                return True
            except queue.Full:       # (a flood): not confirmed, the broker keeps it and offers it again later
                log.warning("ext_inbox_full")
                return True
        if len(parts) < 3 or parts[0] != "stech":
            return False
        company = parts[1]
        if len(parts) == 5 and parts[2] == "gw":  # plant script: status (Last Will) / heartbeat / config / readings
            gid, kind = parts[3], parts[4]
            if not gw.GID_RE.match(gid):
                return False
            if kind in ("status", "hb"):
                gwcontrol.handle_status(company, gid, kind, msg.payload)
            elif kind == "ack":    # the plant PC reports on a command it was sent (delivered / written + read back / failed)
                commands.handle_ack(company, gid, msg.topic, msg.payload)
            elif kind[:1] == "r" and kind[1:].isdigit():  # reading from a plant gateway: signed, mapped by sensor id
                _inbox.put((company, gid, msg.topic, int(kind[1:]), msg.payload, msg.mid if msg.qos > 0 else None))
                return True
            return False  # (config pings are for the scripts)
        if len(parts) == 3 and parts[2][:1] == "r" and parts[2][1:].isdigit():  # older plant script: the 'main' gateway
            _inbox.put((company, gw.MAIN, msg.topic, int(parts[2][1:]), msg.payload, msg.mid if msg.qos > 0 else None))
            return True
        raw = msg.payload.decode().strip()
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw
        ts = None
        if isinstance(body, dict) and body.get("timestamp"):
            ts = datetime.fromisoformat(str(body["timestamp"]).replace("Z", "+00:00"))
        with MasterSession() as master, tenant_session(master, company) as db:
            regs = db.scalars(
                select(DeviceRegister).where(DeviceRegister.topic == msg.topic)
            ).all()
            by_device = {}
            for r in regs:
                try:
                    by_device.setdefault(r.device_id, []).append((r.metric_name, _pick(body, r.json_field), ts))  # scaling is applied in ingest_readings
                except (KeyError, TypeError, ValueError):
                    log.warning("mqtt_field_missing", company=company, metric=r.metric_name)
            for device_id, readings in by_device.items():
                device = db.scalar(select(Device).where(Device.id == device_id, Device.company_name == company))
                if device:
                    ingest_readings(db, master, device, readings)
            if not regs:
                log.warning("mqtt_unmapped_topic", company=company)
    except AppError as exc:
        log.warning("mqtt_rejected", code=exc.code)
    except Exception as exc:
        log.warning("mqtt_message_failed", error=type(exc).__name__)
    return False


def on_message(client, userdata, msg):
    """A plant reading is confirmed to the broker only after it is stored (see _step); everything else is confirmed at once."""
    if not _route(msg) and msg.qos > 0:
        try:
            client.ack(msg.mid, msg.qos)
        except Exception as exc:
            log.warning("mqtt_ack_failed", error=type(exc).__name__)


def main():
    global _client
    threading.Thread(target=gwcontrol.watchdog, name="gateway-watchdog", daemon=True).start()
    threading.Thread(target=extgw.watchdog, name="plant-link-watchdog", daemon=True).start()
    threading.Thread(target=ext_loop, args=(None, _ack_now), name="ext-saver", daemon=True).start()
    n = worker_count()
    if n <= 1:
        threading.Thread(target=batch_loop, args=(None, _ack_now), name="batch-saver", daemon=True).start()
    else:
        ctx = multiprocessing.get_context("spawn")
        queues = [ctx.Queue() for _ in range(n)]
        ack_q = ctx.Queue()
        procs = [ctx.Process(target=_worker_main, args=(q, ack_q), daemon=True) for q in queues]
        for proc in procs:
            proc.start()
        threading.Thread(target=dispatch_loop, args=(queues, procs, ctx, ack_q), name="dispatcher", daemon=True).start()
        threading.Thread(target=ack_pump, args=(ack_q,), name="ack-pump", daemon=True).start()
        log.info("ingest_workers_started", workers=n)
    threading.Thread(target=jobs.loop, name="timed-jobs", daemon=True).start()   # v1.1: scheduled reports + nightly clean-up
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="stech-ingest", clean_session=False)   # the broker keeps readings while this program restarts
    client.manual_ack_set(True)   # ... and a reading is confirmed only after it is stored
    _client = client
    if settings.MQTT_USERNAME:
        client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
    client.on_connect, client.on_message = on_connect, on_message
    client.reconnect_delay_set(1, 30)
    while True:
        try:
            client.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=30)
            client.loop_forever()
        except Exception as exc:
            log.warning("mqtt_connect_failed", error=type(exc).__name__)
            time.sleep(5)


if __name__ == "__main__":
    main()
