r"""Poller: the backend asks Modbus TCP and OPC-UA devices for their values every few seconds.

Two ways to run it:
  * inside the API process (local `python app\main.py`): started automatically when POLLER_ENABLED=true
  * on its own (Docker / servers):                         python -m app.workers.poller

What one round does, for every company database and every Modbus TCP / OPC-UA device:
  1. connect to the device (ip:port)             2. read every mapped register / node
  3. send the RAW values to ingest_readings      (it applies scaling, saves, raises alarms, pushes live updates)
A device that does not answer is skipped for a while (1, 2, 4 ... max 60 rounds' worth of seconds) so one dead
gateway never slows the others. Sensor values are never written to the logs.
"""
import asyncio
import inspect
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger
from app.db.session import MasterSession, tenant_session
from app.core.timeutil import utcnow
from app.models.master import CompanyStorage, User
from app.models.tenant import Alarm, Device, Notification
from app.services import live, oncall, push
from app.services.alarms import alarm_dict, escalation_minutes, place_text
from app.services.ingest import ingest_readings

log = get_logger("poller")

POLLED_PROTOCOLS = ("Modbus TCP", "OPC-UA")   # the server can reach these itself; serial / converter / UDP / TLS devices go through the plant script
WORDS = {"int16": 1, "uint16": 1, "bool": 1, "int32": 2, "uint32": 2, "int64": 4, "uint64": 4, "float32": 2, "float64": 4}
STRUCT = {"int16": ">h", "uint16": ">H", "int32": ">i", "uint32": ">I", "int64": ">q", "uint64": ">Q", "float32": ">f", "float64": ">d"}


# ----------------------------------------------------------------- Modbus
def decode_registers(words: list[int], data_type: str, word_order: str = "big", byte_order: str = "big", bit: int | None = None) -> float:
    """16-bit register words -> number. 'little' word order swaps the 16-bit words of 32/64-bit values,
    'little' byte order swaps the two bytes inside every word. `bit` reads one bit of the first word."""
    if data_type == "bool":
        return float((words[0] >> bit) & 1) if bit is not None else (1.0 if words[0] else 0.0)
    if word_order == "little" and len(words) > 1:
        words = list(reversed(words))
    raw = b"".join(struct.pack("<H" if byte_order == "little" else ">H", w & 0xFFFF) for w in words)
    return float(struct.unpack(STRUCT[data_type], raw)[0])


def _unit_kwarg(fn) -> str:
    """pymodbus 3.7-3.9 call it `slave`, newer versions `device_id`."""
    return "device_id" if "device_id" in inspect.signature(fn).parameters else "slave"


def read_modbus(device: Device, timeout: float, unit_id: int, word_order: str) -> list[tuple[str, float, None]]:
    from pymodbus.client import ModbusTcpClient

    word_order = device.word_order or word_order
    unit_id = device.unit_id if device.unit_id is not None else unit_id
    base = device.address_base or 0
    client = ModbusTcpClient(device.ip_address, port=device.port, timeout=timeout, retries=1)
    if not client.connect():
        raise ConnectionError("modbus connect failed")
    out: list[tuple[str, float, None]] = []
    try:
        for r in device.registers:
            if r.register_address is None or not r.register_type:
                continue
            fn = {"holding": client.read_holding_registers, "input": client.read_input_registers,
                  "coil": client.read_coils, "discrete": client.read_discrete_inputs}[r.register_type]
            kw = {_unit_kwarg(fn): unit_id}
            address = r.register_address - base
            if r.register_type in ("coil", "discrete"):
                resp = fn(address, count=1, **kw)
                if resp.isError():
                    raise ConnectionError(f"modbus error at {r.register_type}:{r.register_address}")
                value = 1.0 if resp.bits[0] else 0.0
            else:
                dtype = r.data_type if r.data_type in WORDS else "float32"
                resp = fn(address, count=WORDS[dtype], **kw)
                if resp.isError():
                    raise ConnectionError(f"modbus error at {r.register_type}:{r.register_address}")
                value = decode_registers(list(resp.registers), dtype, word_order, device.byte_order or "big", r.bit_index)
            out.append((r.metric_name, value, None))
    finally:
        client.close()
    return out


# ----------------------------------------------------------------- OPC-UA
async def _read_opcua(device: Device, timeout: float) -> list[tuple[str, float, None]]:
    from asyncua import Client

    out: list[tuple[str, float, None]] = []
    async with Client(url=f"opc.tcp://{device.ip_address}:{device.port}", timeout=timeout) as client:
        for r in device.registers:
            if not r.node_id:
                continue
            value = await client.get_node(r.node_id).read_value()
            out.append((r.metric_name, float(value), None))  # True/False become 1.0/0.0
    return out


def read_opcua(device: Device, timeout: float) -> list[tuple[str, float, None]]:
    return asyncio.run(asyncio.wait_for(_read_opcua(device, timeout), timeout * 4))


# ----------------------------------------------------------------- one round
_locks: dict[tuple[str, int], threading.Lock] = {}
_locks_guard = threading.Lock()


def _device_lock(key: tuple[str, int]) -> threading.Lock:
    """One reader per device at a time (a Siemens MB_SERVER accepts a single client)."""
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def poll_now(db, master, device: Device, company: str) -> None:
    """Read the device right now (used by the app's Refresh button) and store the new values."""
    if device.protocol not in POLLED_PROTOCOLS or not device.registers:
        raise AppError(422, "NOT_POLLABLE", "This device has no sensors, or it is read by the plant script (serial, converter and secure Modbus devices are read there, not by the server).")
    try:
        with _device_lock((company, device.id)):
            if device.protocol == "Modbus TCP":
                readings = read_modbus(device, settings.POLL_TIMEOUT_SECONDS, settings.MODBUS_UNIT_ID, settings.MODBUS_WORD_ORDER)
            else:
                readings = read_opcua(device, settings.POLL_TIMEOUT_SECONDS)
            if readings:
                ingest_readings(db, master, device, readings)
    except AppError:
        raise
    except Exception as exc:
        raise AppError(502, "DEVICE_UNREACHABLE", f"Could not read the device at {device.ip_address}:{device.port} ({type(exc).__name__}). Check power, cable and IP.")


def _is_muted(db, alarm) -> bool:
    """An alarm of a device in maintenance, or a shelved one, is not escalated."""
    from app.services import alarms as alarm_svc
    dev = db.get(Device, alarm.device_id)
    return bool(dev and alarm_svc.muted(db, dev, alarm.metric_name or ""))


class Poller:
    def __init__(self):
        self._stop = threading.Event()
        self._fails: dict[tuple[str, int], int] = {}      # (company, device_id) -> consecutive failures
        self._skip_until: dict[tuple[str, int], float] = {}
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="poll")
        self._last_escalation = 0.0

    def stop(self):
        self._stop.set()

    def _poll_device(self, company: str, device_id: int):
        key = (company, device_id)
        if time.monotonic() < self._skip_until.get(key, 0):
            return
        try:
            with MasterSession() as master, tenant_session(master, company) as db:
                device = db.scalar(select(Device).where(Device.id == device_id))
                if not device or not device.registers:
                    return
                with _device_lock(key):
                    if device.protocol == "Modbus TCP":
                        readings = read_modbus(device, settings.POLL_TIMEOUT_SECONDS, settings.MODBUS_UNIT_ID, settings.MODBUS_WORD_ORDER)
                    else:
                        readings = read_opcua(device, settings.POLL_TIMEOUT_SECONDS)
                    if readings:
                        ingest_readings(db, master, device, readings)
            if self._fails.pop(key, 0):
                log.info("poll_device_back_online", company=company, device_id=device_id)
            self._skip_until.pop(key, None)
        except AppError as exc:
            log.warning("poll_rejected", company=company, device_id=device_id, code=exc.code)
        except Exception as exc:  # device off / wrong IP / wrong node: back off, keep the others running
            n = self._fails.get(key, 0) + 1
            self._fails[key] = n
            self._skip_until[key] = time.monotonic() + min(2 ** (n - 1) * settings.POLL_INTERVAL_SECONDS, 60)
            if n == 1:
                log.warning("poll_device_unreachable", company=company, device_id=device_id, error=type(exc).__name__)

    def escalate_unacknowledged(self):
        """An alarm nobody acknowledged within the admin-set time for its priority is escalated once: every administrator gets a notification."""
        now = utcnow()
        with MasterSession() as master:
            for st in master.scalars(select(CompanyStorage).where(CompanyStorage.db_name.is_not(None))):
                company = st.company_name
                try:
                    admins = list(master.scalars(select(User.id).where(
                        User.company_name == company, User.role == "admin", User.status == "approved")))
                    if not admins:
                        continue
                    wait = escalation_minutes(master, company)  # minutes per priority, set by the admin (0 = never)
                    with tenant_session(master, company) as db:
                        open_alarms = [a for a in db.scalars(select(Alarm).where(
                            Alarm.cleared_at.is_(None), Alarm.acknowledged_by.is_(None))).all() if not _is_muted(db, a)]
                        oncall.run(master, db, company, open_alarms)   # v1.1: the on-call roster, step by step
                        late = [a for a in open_alarms
                                if wait.get(a.severity, 0) > 0 and a.raised_at < now - timedelta(minutes=wait[a.severity])]
                        for a in late:
                            if db.scalar(select(Notification.id).where(
                                    Notification.alarm_id == a.id, Notification.channel == "escalation").limit(1)):
                                continue
                            dev = db.get(Device, a.device_id)
                            regional = list(master.scalars(select(User.id).where(   # the Regional admin of that State + Region too
                                User.company_name == company, User.role == "regional_admin", User.status == "approved",
                                User.state == dev.state, User.region == dev.region))) if dev else []
                            for uid in admins + regional:
                                db.add(Notification(user_id=uid, alarm_id=a.id, channel="escalation", sent_at=utcnow()))
                            db.commit()
                            push.notify(company, admins + regional, f"NOT ACKNOWLEDGED - {a.severity.upper()} alarm",
                                        f"{dev.device_name if dev else 'Device'}: {a.message}", {"alarm_id": a.id, "severity": a.severity})
                            log.info("alarm_escalated", company=company, alarm_id=a.id)
                            live.publish(company, {"type": "alarm", "event": "escalated", "alarm": alarm_dict(a, dev.device_name if dev else None, place_text(dev.city, dev.plant_name) if dev else None)})
                except Exception as exc:
                    log.warning("escalation_skipped", company=company, error=type(exc).__name__)

    def run_once(self):
        targets: list[tuple[str, int]] = []
        with MasterSession() as master:
            companies = [s.company_name for s in master.scalars(select(CompanyStorage).where(CompanyStorage.db_name.is_not(None)))]
            for company in companies:
                try:
                    with tenant_session(master, company) as db:
                        ids = db.scalars(select(Device.id).where(Device.protocol.in_(POLLED_PROTOCOLS))).all()
                    targets += [(company, i) for i in ids]
                except Exception as exc:
                    log.warning("poll_company_skipped", company=company, error=type(exc).__name__)
        list(self._pool.map(lambda t: self._poll_device(*t), targets))

    def run_forever(self):
        log.info("poller_started", interval=settings.POLL_INTERVAL_SECONDS)
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.run_once()
            except Exception as exc:
                log.warning("poll_round_failed", error=type(exc).__name__)
            if time.monotonic() - self._last_escalation >= 60:
                self._last_escalation = time.monotonic()
                try:
                    self.escalate_unacknowledged()
                except Exception as exc:
                    log.warning("escalation_failed", error=type(exc).__name__)
            self._stop.wait(max(1.0, settings.POLL_INTERVAL_SECONDS - (time.monotonic() - started)))
        log.info("poller_stopped")


def start_in_background() -> Poller:
    """Used by the API process: runs the poller in a daemon thread."""
    poller = Poller()
    threading.Thread(target=poller.run_forever, name="poller", daemon=True).start()
    return poller


if __name__ == "__main__":
    configure_logging(settings.LOG_LEVEL)
    Poller().run_forever()
