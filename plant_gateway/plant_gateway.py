r"""STECH iVIEW plant gateway  (version 3.2).

Runs 24/7 on the plant PC. Reads the PLCs and meters of ONE plant (one State + Region) every second and sends every
change to the cloud over an outgoing encrypted MQTT connection. Nothing to edit: the devices and sensors you add in
the app are picked up at once. A company with several plants / zones runs one copy of this script per plant, each
with its own link.json and its own key; the cloud shows them all under the one parent company.

    PLC --(plant network or serial cable)--> this script --(outgoing MQTT + TLS)--> cloud --> STECH iVIEW app

Reads, and writes ONLY the points that an administrator marked "writable" in the app (company write lock on, value inside
the limits set in the app, command signed and not expired). After every write the value is read back and reported.
The PLC keeps its own interlocks - this script is never a safety system.
Protocols:
    Modbus TCP              Ethernet, port 502
    Modbus RTU              serial cable: RS-485 / RS-422 / RS-232 (USB-serial adapter), many slaves on one cable
    Modbus ASCII            serial cable, text frames
    Modbus RTU over TCP     serial-to-Ethernet converters in "transparent" mode
    Modbus UDP              Modbus frames over UDP
    Modbus TLS              secure Modbus (needs modbus_tls/ca.pem, see below)
    OPC-UA

Files next to this script:
    link.json      given by the app (company, gateway name and its own key). Do not share it.
    outbox.db      readings waiting to be sent (kept if the internet is down, sent when it returns; the newest are sent first after a long outage)
    gateway.log    what the script is doing
    gateway.lock   makes sure only one copy of this gateway runs on the PC
    modbus_tls\    only for Modbus TLS devices: ca.pem (the device's CA certificate), optional client.pem + client.key
"""
import asyncio
import errno
import base64
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import queue
import re
import sqlite3
import subprocess
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

# Under the ready-made program the folder is the program's folder and the script itself is kept inside the program (a hidden-away
# file in the folder "program"), so the customer sees no .py file. Run as a plain script (developers, tests) both are next to it.
HERE = os.environ.get("STECH_HOME") or os.path.dirname(os.path.abspath(__file__))
VERSION = "3.5"
EXIT_ALREADY_RUNNING = 3  # START.bat stops quietly when it sees this
POLL_SECONDS = 1.0        # default: every device is read this often (link.json "poll_seconds" changes it, 0.25 at the fastest; a slow serial bus simply takes as long as it needs)
REFRESH_SECONDS = 5       # backup check; the normal update is the instant "settings changed" message
KEEPALIVE = 5             # broker notices a dead script after about 7 s (Last Will message)
HEARTBEAT = 1             # "I am alive" message every second; it also carries which devices answer, so the app shows Offline within seconds
RESEND_UNCHANGED = 10     # unchanged value is repeated every 10 s so the app knows the sensor is alive
MAX_BACKOFF = 30          # a silent device / unreachable link is retried after 1, 2, 4 ... at most 30 seconds
BLOCK_REGS = 100          # most registers fetched with one request (the Modbus limit is 125; some devices allow fewer, then they are read one by one)
BLOCK_GAP = 10            # values at most this many registers apart share a request
BLOCK_BITS = 400          # coils / discrete inputs per request
BLOCK_BIT_GAP = 32
SERIAL_GAP = 0.02         # quiet time between two requests on a serial cable (seconds)

# ---------------------------------------------------------------- signed self-update
# The cloud offers a newer plant_gateway.py together with an Ed25519 signature made in the build pipeline. The matching
# PUBLIC key is in link.json ("update_key"); the private key is never on the server. This script installs a new version
# only when (1) the signature is valid, (2) the SHA-256 matches, (3) the version is higher than the running one,
# (4) it compiles and starts in a test run. After the swap the new version must connect to the cloud within
# CONFIRM_WITHIN seconds, otherwise the previous version is put back and the bad version is never tried again.
UPDATE_FIRST = 60         # first check this long after start
UPDATE_EVERY = 600        # then every 10 minutes
CONFIRM_WITHIN = 120      # a fresh update must be connected to the cloud by then, or it is rolled back
NEEDS_LAUNCHER = 1        # the ready-made program (launcher) version this script needs; the updater refuses a script that needs a newer one
EXIT_PROGRAM_SWAP = 11    # the program around this script has a new version waiting: its supervisor swaps it in
EXIT_PROGRAM_BACK = 12    # the new program did not work: its supervisor puts the old one back
EXIT_UPDATED = 10         # START.bat restarts the script on any exit code except 3

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _recover_x(y: int, sign: int):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _I % _P
    if (x * x - x2) % _P != 0:
        return None
    return _P - x if (x & 1) != sign else x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _pt_add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _pt_mul(s: int, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _pt_add(q, p)
        p = _pt_add(p, p)
        s >>= 1
    return q


def _pt_decode(b: bytes):
    y = int.from_bytes(b, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def ed25519_verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    """RFC 8032 Ed25519 signature check (pure Python, so the plant PC needs no extra package)."""
    try:
        if len(pub) != 32 or len(sig) != 64:
            return False
        a, r = _pt_decode(pub), _pt_decode(sig[:32])
        s = int.from_bytes(sig[32:], "little")
        if a is None or r is None or s >= _L:
            return False
        h = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(), "little") % _L
        lhs, rhs = _pt_mul(s, _G), _pt_add(r, _pt_mul(h, a))
        return (lhs[0] * rhs[2] - rhs[0] * lhs[2]) % _P == 0 and (lhs[1] * rhs[2] - rhs[1] * lhs[2]) % _P == 0
    except Exception:
        return False


def program_message(platform: str, version: str, sha256: str) -> bytes:
    return f"STECH-PLANT-PROGRAM\n{platform}\n{version}\n{sha256}".encode()


def my_platform() -> str:
    return "windows" if sys.platform.startswith("win") else ("macos" if sys.platform == "darwin" else "linux")


def under_launcher() -> bool:
    """True when running inside the ready-made program (not as a plain Python script)."""
    return bool(os.environ.get("STECH_LAUNCHER")) and bool(getattr(sys, "frozen", False))


def vtuple(v) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v)))


def script_file() -> str:
    """The file the signed self-update replaces: the script itself."""
    return os.environ.get("STECH_SCRIPT") or os.path.join(HERE, "plant_gateway.py")


def release_message(version: str, sha256: str) -> bytes:
    return f"STECH-PLANT-SCRIPT\n{version}\n{sha256}".encode()


def _jread(name: str):
    try:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _jwrite(name: str, data) -> None:
    write_json_atomic(os.path.join(HERE, name), data)


def _jdel(name: str) -> None:
    try:
        os.remove(os.path.join(HERE, name))
    except OSError:
        pass


def rollback_update(reason: str) -> bool:
    """Puts the previous version back. Returns True when it did."""
    pending = _jread("update_pending.json") or {}
    cur = script_file()
    bak = cur + ".bak"
    if not os.path.isfile(bak):
        _jdel("update_pending.json")
        return False
    tmp = cur + ".rb"
    with open(bak, "rb") as f, open(tmp, "wb") as g:
        g.write(f.read())
    os.replace(tmp, cur)
    if pending.get("to"):
        _jwrite("update_blocked.json", {"version": pending["to"]})
    _jwrite("update_report.json", {"event": "rolled_back", "from": pending.get("from", ""), "to": pending.get("to", ""), "reason": reason[:80]})
    _jdel("update_pending.json")
    logging.getLogger("gateway").error("update %s -> %s did not work (%s): the previous version was put back", pending.get("from"), pending.get("to"), reason)
    return True


def boot_guard() -> bool:
    """Called first thing at start. True = a bad update was just undone and the script must restart."""
    pending = _jread("update_pending.json")
    if not pending:
        return False
    pending["starts"] = int(pending.get("starts", 0)) + 1
    if pending["starts"] > 3 or pending.get("to") != VERSION:
        return rollback_update("the new version kept stopping" if pending["starts"] > 3 else "version mismatch")
    _jwrite("update_pending.json", pending)
    return False


MODBUS_SERIAL = ("Modbus RTU", "Modbus ASCII")
MODBUS_NETWORK = ("Modbus TCP", "Modbus RTU over TCP", "Modbus UDP", "Modbus TLS")
MODBUS_ALL = MODBUS_NETWORK + MODBUS_SERIAL

WORDS = {"int16": 1, "uint16": 1, "bool": 1, "int32": 2, "uint32": 2, "int64": 4, "uint64": 4, "float32": 2, "float64": 4}
STRUCT = {"int16": ">h", "uint16": ">H", "int32": ">i", "uint32": ">I", "int64": ">q", "uint64": ">Q", "float32": ">f", "float64": ">d"}

log = logging.getLogger("gateway")
CLOCK_OFFSET = 0.0        # seconds to ADD to this PC's clock to get the cloud's time (a wrong PC clock must not lose data)


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.handlers.RotatingFileHandler(os.path.join(HERE, "gateway.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[sh, fh])
    logging.getLogger("pymodbus").setLevel(logging.CRITICAL)  # the script reports device problems itself, once, in plain words


def now_iso() -> str:
    t = datetime.now(timezone.utc) + timedelta(seconds=CLOCK_OFFSET)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------- signing (same recipe as the cloud)
def sign(secret: str, topic: str, ts: str, value) -> str:
    return hmac.new(secret.encode(), f"{topic}|{ts}|{value}".encode(), hashlib.sha256).hexdigest()


def reading_payload(secret: str, topic: str, value: float, ts: str) -> str:
    return json.dumps({"value": value, "timestamp": ts, "sig": sign(secret, topic, ts, float(value).__repr__())}, separators=(",", ":"))


def resign(secret: str, topic: str, payload: str) -> str:
    """A reading waiting in the outbox was signed with the key of its time. If the key was replaced meanwhile (reset in the app, then a new
    setup code), the cloud would refuse the old signature: sign again with the CURRENT key, at the moment of sending."""
    try:
        body = json.loads(payload)
        if isinstance(body, dict) and "value" in body and "timestamp" in body and "sig" in body:
            return reading_payload(secret, topic, body["value"], body["timestamp"])
    except (ValueError, TypeError):
        pass
    return payload


def status_payload(secret: str, topic: str, state: str, ts: str, info: dict | None = None, dev: str | None = None) -> str:
    body = {"state": state, "ts": ts, "sig": sign(secret, topic, ts, state)}
    if info:
        body["info"] = info  # small health details shown on the app's Gateway page (version, devices answering, waiting readings)
    if dev is not None:      # "12:1,13:0" = device 12 answers, device 13 does not (signed, so it cannot be forged on the way)
        body["dev"] = dev
        body["dsig"] = sign(secret, topic, ts, "dev|" + dev)
    return json.dumps(body, separators=(",", ":"))


def lock_instance(path: str):
    """Only one copy of the gateway per folder: a second copy (for example AUTOSTART + START.bat) would knock the first off.
    Returns the open lock file, or None when another copy holds it. If the check itself cannot be done, the script just runs."""
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, 36):  # the other copy holds the lock
            f.close()
            return None
        logging.getLogger("gateway").warning("could not check for a second copy (%s) - continuing", exc)
    except Exception as exc:
        logging.getLogger("gateway").warning("could not check for a second copy (%s) - continuing", type(exc).__name__)
    return f


def write_json_atomic(path: str, data) -> None:
    """Write, then rename: a power cut can never leave a half-written file behind."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------- reading Modbus
class LinkError(Exception):
    """The connection itself is down (cable / port / network): every device behind it is unreachable."""


class CommError(Exception):
    """This one device did not answer (timeout, broken reply). The link may be fine."""


def decode_registers(words, data_type: str, word_order: str = "big", byte_order: str = "big", bit: int | None = None) -> float:
    """16-bit register words -> number.
    word_order 'little': the 16-bit words of a 32 / 64-bit value come low word first.
    byte_order 'little': the two bytes inside every word are swapped.
    bit: read ONE bit (0-15) of the first word (status words)."""
    if data_type == "bool":
        return float((words[0] >> bit) & 1) if bit is not None else (1.0 if words[0] else 0.0)
    if word_order == "little" and len(words) > 1:
        words = list(reversed(words))
    raw = b"".join(struct.pack("<H" if byte_order == "little" else ">H", w & 0xFFFF) for w in words)
    return float(struct.unpack(STRUCT[data_type], raw)[0])


def encode_registers(value: float, data_type: str, word_order: str = "big", byte_order: str = "big") -> list[int]:
    """Number -> 16-bit register words (the exact reverse of decode_registers). Integers must be whole numbers."""
    if data_type not in STRUCT:
        raise ValueError(f"data type {data_type} cannot be written")
    if data_type.startswith(("int", "uint")):
        if value != int(value):
            raise ValueError("this sensor takes whole numbers only")
        value = int(value)
    try:
        raw = struct.pack(STRUCT[data_type], value)
    except struct.error:
        raise ValueError("the value does not fit the data type")
    words = [struct.unpack(">H", raw[i:i + 2])[0] for i in range(0, len(raw), 2)]
    if byte_order == "little":
        words = [((w & 0xFF) << 8) | (w >> 8) for w in words]
    if word_order == "little" and len(words) > 1:
        words = list(reversed(words))
    return words


def link_key(dev: dict) -> tuple:
    """Devices that share one cable or one converter / gateway address are read one after the other on ONE connection."""
    if dev["protocol"] in MODBUS_SERIAL:
        return ("serial", dev["host"])              # one serial port can be opened by one program only
    return (dev["protocol"], dev["host"], int(dev["port"]))


class ModbusLink:
    """One Modbus connection (a serial port, or one IP address and port) with every device behind it."""

    def __init__(self, devices: list[dict], defaults: dict):
        self.devices, self.defaults = devices, defaults
        first = devices[0]
        self.protocol, self.host, self.port = first["protocol"], first["host"], int(first["port"])
        self.serial = self.protocol in MODBUS_SERIAL
        self.timeout = max((d.get("timeout_ms") or (1000 if self.serial else 1500)) for d in devices) / 1000.0
        self.retries = max((d["retries"] if d.get("retries") is not None else (2 if self.serial else 1)) for d in devices)
        self.cfg = first
        if self.serial:
            for d in devices[1:]:
                if (d.get("baud"), d.get("parity"), d.get("databits"), d.get("stopbits")) != (first.get("baud"), first.get("parity"), first.get("databits"), first.get("stopbits")):
                    log.warning("devices on %s must use the same baud rate / parity / data bits / stop bits; using the settings of '%s'",
                                self.host, first["name"])
                    break
        self.client = None
        self.refused: set[tuple[int, int]] = set()   # (device id, sensor id) the device refused - logged once
        self.single_mode: set[tuple[int, str, int]] = set()   # (device id, type, first address) of groups the device refuses: read one by one

    # ---- connection
    def _tls_context(self) -> ssl.SSLContext:
        folder = os.path.join(HERE, "modbus_tls")
        ca = os.path.join(folder, "ca.pem")
        if not os.path.isfile(ca):
            raise LinkError("Modbus TLS needs the device's CA certificate in the folder 'modbus_tls' (file ca.pem)")
        ctx = ssl.create_default_context(cafile=ca)
        ctx.check_hostname = False        # devices are reached by IP address; the certificate chain is still verified
        cert, key = os.path.join(folder, "client.pem"), os.path.join(folder, "client.key")
        if os.path.isfile(cert) and os.path.isfile(key):
            ctx.load_cert_chain(cert, key)
        return ctx

    def _make_client(self):
        from pymodbus import FramerType
        from pymodbus.client import ModbusSerialClient, ModbusTcpClient, ModbusTlsClient, ModbusUdpClient
        kw = {"timeout": self.timeout, "retries": self.retries}
        p = self.protocol
        if p == "Modbus TCP":
            return ModbusTcpClient(self.host, port=self.port, framer=FramerType.SOCKET, **kw)
        if p == "Modbus RTU over TCP":
            return ModbusTcpClient(self.host, port=self.port, framer=FramerType.RTU, **kw)
        if p == "Modbus UDP":
            return ModbusUdpClient(self.host, port=self.port, framer=FramerType.SOCKET, **kw)
        if p == "Modbus TLS":
            return ModbusTlsClient(self.host, port=self.port, sslctx=self._tls_context(), **kw)
        try:
            import serial  # noqa: F401  (pyserial)
        except ImportError:
            raise LinkError("the serial library (pyserial) is not installed - close this window and double-click START.bat again")
        c = self.cfg
        return ModbusSerialClient(
            self.host, framer=FramerType.ASCII if p == "Modbus ASCII" else FramerType.RTU,
            baudrate=int(c.get("baud") or 9600), bytesize=int(c.get("databits") or (7 if p == "Modbus ASCII" else 8)),
            parity=c.get("parity") or ("E" if p == "Modbus ASCII" else "N"), stopbits=int(c.get("stopbits") or 1), **kw)

    def connect(self) -> None:
        if self.client is not None and self.client.connected:
            return
        self.close()
        try:
            client = self._make_client()
            ok = client.connect()
        except LinkError:
            raise
        except Exception as exc:
            raise LinkError(f"{type(exc).__name__}: {exc}")
        if not ok:
            what = f"serial port {self.host} cannot be opened (wrong port name, or another program is using it)" if self.serial \
                else f"cannot connect to {self.host}:{self.port}"
            raise LinkError(what)
        self.client = client

    def close(self) -> None:
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        self.client = None

    # ---- reading
    def _fetch(self, rtype: str, address: int, count: int, unit: int):
        fn = {"holding": self.client.read_holding_registers, "input": self.client.read_input_registers,
              "coil": self.client.read_coils, "discrete": self.client.read_discrete_inputs}[rtype]
        try:
            return fn(address, count=count, device_id=unit)
        except Exception as exc:  # no reply / broken reply / port or socket error
            if isinstance(exc, (OSError, ConnectionError)) and not isinstance(exc, TimeoutError):
                self.close()
            raise CommError(type(exc).__name__)

    @staticmethod
    def _blocks(items: list[tuple]) -> list[tuple]:
        """Neighbouring values are read with ONE request instead of one each. items = (register, type, address, count, ...).
        A block never holds more than BLOCK_REGS registers (BLOCK_BITS bits) and never bridges a gap wider than BLOCK_GAP (BLOCK_BIT_GAP)."""
        out, by_type = [], {}
        for it in items:
            by_type.setdefault(it[1], []).append(it)
        for rtype, group in by_type.items():
            bits = rtype in ("coil", "discrete")
            limit, gap = (BLOCK_BITS, BLOCK_BIT_GAP) if bits else (BLOCK_REGS, BLOCK_GAP)
            group.sort(key=lambda i: (i[2], i[3]))
            cur = None
            for it in group:
                end_it = it[2] + it[3]
                if cur is not None and it[2] - cur["end"] <= gap and max(cur["end"], end_it) - cur["start"] <= limit:
                    cur["end"] = max(cur["end"], end_it)
                    cur["items"].append(it)
                else:
                    cur = {"type": rtype, "start": it[2], "end": end_it, "items": [it]}
                    out.append(cur)
        return [(b["type"], b["start"], b["end"] - b["start"], b["items"]) for b in out]

    def read_device(self, dev: dict) -> list[tuple[dict, float]]:
        """Reads every configured value of the device. Values that lie close together are fetched with one request (a PLC with 200 values
        needs a handful of requests, not 200). Nothing is ever skipped: if the device refuses a block, that block is read value by value
        (and remembered, so the refusal is not repeated every second), and a refused single value is reported and the rest still read."""
        self.connect()
        unit = dev["unit_id"] if dev.get("unit_id") is not None else int(self.defaults.get("unit_id", 1))
        word_order = dev.get("word_order") or self.defaults.get("word_order", "big")
        byte_order = dev.get("byte_order") or "big"
        base = int(dev.get("address_base") or 0)
        items, answered = [], False
        for r in dev["registers"]:
            rtype = r.get("register_type") or "holding"
            if r.get("register_address") is None:
                continue
            address = int(r["register_address"]) - base
            if address < 0:
                self._refuse(dev, r, "its address is below the address base of the device")
                continue
            if rtype not in ("holding", "input", "coil", "discrete"):
                continue
            bit_type = rtype in ("coil", "discrete")
            dt = "bool" if bit_type else (r.get("data_type") if r.get("data_type") in WORDS else "float32")
            items.append((r, rtype, address, 1 if bit_type else WORDS[dt], dt, bit_type))
        got: dict[int, float] = {}                      # sensor id -> value

        def take(it, regs_or_bits):
            nonlocal answered
            r, rtype, _, _, dt, bit_type = it
            answered = True
            self.refused.discard((dev["id"], r["id"]))
            if bit_type:
                got[r["id"]] = 1.0 if regs_or_bits[0] else 0.0
            else:
                got[r["id"]] = decode_registers(list(regs_or_bits), dt, word_order, byte_order, r.get("bit_index"))

        def single(it):
            nonlocal answered
            r, rtype, address, count = it[0], it[1], it[2], it[3]
            resp = self._fetch(rtype, address, count, unit)
            if resp.isError():
                if getattr(resp, "exception_code", None) is not None:   # the device answered "no": wrong address / not allowed
                    answered = True
                    self._refuse(dev, r, f"the device refused it (Modbus exception {resp.exception_code}) - check the address, register type and slave id")
                    return
                raise CommError("no valid reply")
            take(it, resp.bits if it[5] else resp.registers)
            if self.serial:
                time.sleep(SERIAL_GAP)

        for rtype, start, count, members in self._blocks(items):
            key = (dev["id"], rtype, start)
            if len(members) > 1 and key not in self.single_mode:
                resp = self._fetch(rtype, start, count, unit)
                if not resp.isError():
                    seq = resp.bits if rtype in ("coil", "discrete") else resp.registers
                    for it in members:
                        off = it[2] - start
                        take(it, seq[off:off + it[3]])
                    if self.serial:
                        time.sleep(SERIAL_GAP)
                    continue
                if getattr(resp, "exception_code", None) is None:
                    raise CommError("no valid reply")
                self.single_mode.add(key)               # the device refuses this block: value by value from now on, nothing is lost
                log.info("device %s: a group of %d values (%s %d-%d) is refused by the device - reading them one by one", dev["name"], len(members), rtype, start, start + count - 1)
            for it in members:
                single(it)
        out = [(r, got[r["id"]]) for r in dev["registers"] if r["id"] in got]
        if not out and dev["registers"]:
            raise CommError("every sensor was refused - check addresses, register types and slave id" if answered else "nothing could be read")
        return out

    def write(self, dev: dict, reg: dict, raw: float) -> float:
        """Writes one point and returns what the PLC reports afterwards (read back). Raises CommError with a plain reason."""
        self.connect()
        unit = dev["unit_id"] if dev.get("unit_id") is not None else int(self.defaults.get("unit_id", 1))
        word_order = dev.get("word_order") or self.defaults.get("word_order", "big")
        byte_order = dev.get("byte_order") or "big"
        rtype = reg.get("register_type") or "holding"
        address = int(reg["register_address"]) - int(dev.get("address_base") or 0)
        if address < 0:
            raise CommError("the address is below the address base of the device")
        bit = reg.get("bit_index")
        dt = "bool" if rtype == "coil" else (reg.get("data_type") if reg.get("data_type") in WORDS else "float32")

        def ok(resp, what):
            if resp.isError():
                code = getattr(resp, "exception_code", None)
                raise CommError(f"the PLC refused the {what} (Modbus exception {code})" if code is not None else f"no valid reply to the {what}")
            return resp
        try:
            if rtype == "coil":
                ok(self.client.write_coil(address, bool(raw), device_id=unit), "write")
                return 1.0 if ok(self.client.read_coils(address, count=1, device_id=unit), "read-back").bits[0] else 0.0
            if rtype != "holding":
                raise CommError("only coils and holding registers can be written")
            if dt == "bool":      # one bit of a word: read the word, change that bit only, write it back
                if bit is None:
                    raise CommError("no bit number is set for this point")
                word = ok(self.client.read_holding_registers(address, count=1, device_id=unit), "read").registers[0]
                word = (word | (1 << bit)) if raw else (word & ~(1 << bit))
                ok(self.client.write_register(address, word & 0xFFFF, device_id=unit), "write")
                back = ok(self.client.read_holding_registers(address, count=1, device_id=unit), "read-back").registers
                return decode_registers(list(back), "bool", bit=bit)
            words = encode_registers(raw, dt, word_order, byte_order)
            ok(self.client.write_registers(address, words, device_id=unit), "write")
            back = ok(self.client.read_holding_registers(address, count=len(words), device_id=unit), "read-back").registers
            return decode_registers(list(back), dt, word_order, byte_order)
        except CommError:
            raise
        except ValueError as exc:
            raise CommError(str(exc))
        except Exception as exc:
            if isinstance(exc, (OSError, ConnectionError)) and not isinstance(exc, TimeoutError):
                self.close()
            raise CommError(f"no answer from the PLC ({type(exc).__name__})")

    def _refuse(self, dev: dict, reg: dict, why: str) -> None:
        key = (dev["id"], reg["id"])
        if key not in self.refused:
            self.refused.add(key)
            log.warning("device %s, sensor %s is not read: %s", dev["name"], reg["metric_name"], why)


class OpcuaReader:
    def __init__(self, dev: dict):
        self.dev = dev

    async def _read(self):
        from asyncua import Client
        out = []
        async with Client(url=f"opc.tcp://{self.dev['host']}:{self.dev['port']}", timeout=3) as client:
            for r in self.dev["registers"]:
                if r.get("node_id"):
                    out.append((r, float(await client.get_node(r["node_id"]).read_value())))
        return out

    def read(self):
        return asyncio.run(asyncio.wait_for(self._read(), 8))

    async def _write(self, reg: dict, raw: float) -> float:
        from asyncua import Client, ua
        async with Client(url=f"opc.tcp://{self.dev['host']}:{self.dev['port']}", timeout=3) as client:
            node = client.get_node(reg["node_id"])
            vt = await node.read_data_type_as_variant_type()
            if vt == ua.VariantType.Boolean:
                value = bool(raw)
            elif vt in (ua.VariantType.Float, ua.VariantType.Double):
                value = float(raw)
            else:
                value = int(raw)
            await node.write_value(ua.DataValue(ua.Variant(value, vt)))
            return float(await node.read_value())

    def write(self, reg: dict, raw: float) -> float:
        if not reg.get("node_id"):
            raise CommError("this point has no OPC-UA node id")
        try:
            return asyncio.run(asyncio.wait_for(self._write(reg, raw), 8))
        except CommError:
            raise
        except Exception as exc:
            raise CommError(f"the OPC-UA server did not accept the write ({type(exc).__name__})")

    def close(self):
        pass


# ---------------------------------------------------------------- outbox (nothing is lost while offline)
KEY_CANCELLED_TEXT = ("KEY CANCELLED: the cloud no longer accepts this gateway's key (it was reset in the app, the gateway was removed, or a possible misuse was found). "
                      "Check with your administrator, then get a new key: connect the box again with a NEW setup code (or download the script again for a PC). "
                      "Readings already stored stay on this gateway.")
OUTBOX_MAX_ROWS = 500_000      # how many unsent readings are kept while the cloud cannot be reached (link.json "outbox_max_rows" changes it)
LIVE_LANE_AT = 2000            # with more readings than this waiting, the NEWEST ones are sent alongside the old backlog so the app stays live
LIVE_LANE_ROWS = 200           # how many of the newest readings go out in each round while the backlog is being sent


class Outbox:
    def __init__(self, path: str, max_rows: int = OUTBOX_MAX_ROWS):
        self.path = path
        self.db = self._open()
        self.lock, self.max_rows, self.added = threading.Lock(), max_rows, 0
        self.wake = threading.Event()
        self.last_error = 0.0

    def _open(self):
        try:
            return self._connect()
        except sqlite3.DatabaseError:  # damaged file (power cut, full disk): keep it for inspection and start a fresh one
            bad = f"{self.path}.damaged-{int(time.time())}"
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            log.error("the outbox file was damaged; it was moved to %s and a new one started", os.path.basename(bad))
            return self._connect()

    def _connect(self):
        db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT, payload TEXT)")
        db.commit()
        db.execute("SELECT COUNT(*) FROM outbox").fetchone()  # raises on a damaged file
        return db

    def _report(self, exc: Exception) -> None:
        if time.monotonic() - self.last_error > 60:
            self.last_error = time.monotonic()
            log.error("outbox problem (%s) - readings are dropped until it recovers; is the disk full?", type(exc).__name__)

    def add(self, topic: str, payload: str) -> None:
        try:
            with self.lock:
                self.db.execute("INSERT INTO outbox (topic, payload) VALUES (?, ?)", (topic, payload))
                self.added += 1
                if self.added % 500 == 0:
                    # cheap even with millions of rows (COUNT(*) would read the whole file): the span of row numbers, like backlog()
                    lo, hi = self.db.execute("SELECT MIN(id), MAX(id) FROM outbox").fetchone()
                    if lo is not None and hi - lo + 1 > self.max_rows:
                        self.db.execute("DELETE FROM outbox WHERE id <= ?", (hi - self.max_rows,))
                self.db.commit()
        except sqlite3.Error as exc:
            self._report(exc)
            return
        self.wake.set()

    def batch(self, skip: set, limit: int):
        try:
            with self.lock:
                rows = self.db.execute("SELECT id, topic, payload FROM outbox ORDER BY id LIMIT ?", (limit + len(skip),)).fetchall()
        except sqlite3.Error as exc:
            self._report(exc)
            return []
        return [r for r in rows if r[0] not in skip][:limit]

    def newest(self, skip: set, limit: int):
        """The newest waiting readings (the live ones), for the live lane."""
        try:
            with self.lock:
                rows = self.db.execute("SELECT id, topic, payload FROM outbox ORDER BY id DESC LIMIT ?", (limit + len(skip),)).fetchall()
        except sqlite3.Error as exc:
            self._report(exc)
            return []
        return [r for r in rows if r[0] not in skip][:limit]

    def backlog(self) -> int:
        """Readings still waiting to be sent (approximate, and cheap even when there are many)."""
        with self.lock:
            lo, hi = self.db.execute("SELECT MIN(id), MAX(id) FROM outbox").fetchone()
        return 0 if lo is None else int(hi - lo + 1)

    def remove(self, row_id: int) -> None:
        try:
            with self.lock:
                self.db.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
                self.db.commit()
        except sqlite3.Error as exc:
            self._report(exc)


# ---------------------------------------------------------------- the gateway
class Gateway:
    def __init__(self, link: dict):
        self.link = link
        self.company, self.secret = link["company"], link["secret"]
        self.api = link["api"].rstrip("/")
        # link.json from older downloads has no gateway_id: it is the company-wide 'main' gateway with the old topics
        self.legacy = "gateway_id" not in link
        self.gid = link.get("gateway_id", "main")
        self.name = link.get("gateway_name") or self.gid
        self.poll = min(max(float(link.get("poll_seconds", POLL_SECONDS)), 0.25), 60.0)
        base = f"stech/{self.company}/gw/{self.gid}"
        self.status_topic = f"{base}/status"
        self.hb_topic = f"{base}/hb"
        self.cfg_topics = [f"{base}/config", f"stech/{self.company}/gw/all/config"]
        self.cmd_topic, self.ack_topic = f"{base}/cmd", f"{base}/ack"
        self.specs: dict[tuple[int, int], tuple[dict, dict, tuple]] = {}   # (device id, sensor id) -> (device, sensor, connection key)
        self.cmdq: dict[tuple, queue.Queue] = {}                           # one queue per connection: its own thread does the writing
        self.seen: OrderedDict[str, float] = OrderedDict()                 # command ids already handled (a repeated command is never run twice)
        self.health: dict[int, bool] = {}   # device id -> answered on its last read
        self.hb_now = threading.Event()     # a device just changed between answering / not answering: tell the cloud at once
        self.dev_total = 0
        try:
            rows = int(link.get("outbox_max_rows", OUTBOX_MAX_ROWS))
        except (TypeError, ValueError):
            rows = OUTBOX_MAX_ROWS
        self.outbox = Outbox(os.path.join(HERE, "outbox.db"), min(max(rows, 10_000), 50_000_000))
        self.stop = threading.Event()
        self.restart_code = 0
        self.reload = threading.Event()
        self.connected = threading.Event()
        self.key_refused = False
        # inflight / sent belong to the sender thread ONLY. The MQTT thread just drops its news into these two lightweight
        # channels and never takes a lock the sender holds (holding a lock around client.publish() while the MQTT thread
        # waits for the same lock froze the whole connection - no heartbeat, no reconnect, no log line).
        self.inflight: dict[int, int] = {}
        self.sent: set[int] = set()
        self.acked: queue.SimpleQueue = queue.SimpleQueue()
        self.link_reset = threading.Event()
        self.cfg_hash = None
        self.workers: dict[tuple, tuple[threading.Event, threading.Thread]] = {}
        self.client = None

    # ---- MQTT
    def start_mqtt(self) -> None:
        import paho.mqtt.client as mqtt
        m = self.link["mqtt"]
        ws = m.get("transport") == "websockets"
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"gw-{self.company}-{self.gid}", protocol=mqtt.MQTTv311,
                                  transport="websockets" if ws else "tcp")
        if ws:
            self.client.ws_set_options(path=m.get("ws_path", "/mqtt"))
        self.client.username_pw_set(m["username"], m["password"])
        if m.get("tls", True):
            ca = None
            if m.get("ca_pem"):
                ca = os.path.join(HERE, "broker_ca.pem")
                with open(ca, "w", encoding="utf-8") as f:
                    f.write(m["ca_pem"])
            self.client.tls_set(ca_certs=ca, cert_reqs=ssl.CERT_REQUIRED)
        self.client.will_set(self.status_topic, status_payload(self.secret, self.status_topic, "offline", "0"), qos=1, retain=True)
        self.client.max_inflight_messages_set(100)
        self.client.reconnect_delay_set(1, 10)
        self.client.on_connect, self.client.on_disconnect = self._on_connect, self._on_disconnect
        self.client.on_publish, self.client.on_message = self._on_publish, self._on_message
        self.client.connect_async(m["host"], int(m["port"]), keepalive=KEEPALIVE)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc.is_failure:
            if getattr(rc, "value", 0) in (134, 135, 4, 5):   # bad user name / password / not authorized: the key itself is refused
                if not self.key_refused:
                    self.key_refused = True
                    log.warning("cloud refused the connection (%s).", rc)
                    log.error(KEY_CANCELLED_TEXT)           # said ONCE: the log (and the box page) must not drown in repeats
                client.reconnect_delay_set(30, 120)          # a cancelled key does not fix itself: ask slowly, do not flood the cloud
            else:
                log.warning("cloud refused the connection (%s).", rc)
            return
        if self.key_refused:
            self.key_refused = False
            client.reconnect_delay_set(1, 10)
        log.info("connected to the cloud")
        for topic in self.cfg_topics + [self.cmd_topic]:
            client.subscribe(topic, qos=1)
        client.publish(self.status_topic, status_payload(self.secret, self.status_topic, "online", now_iso(), self.info()), qos=1, retain=True)
        self.connected.set()
        self.reload.set()  # (re)connected: check the settings right away
        self.outbox.wake.set()

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        if self.connected.is_set():
            log.warning("lost the cloud connection - readings are kept and sent when it returns")
        self.connected.clear()
        self.link_reset.set()          # the sender thread forgets what was in flight (the rows stay in the store and are sent again)
        self.outbox.wake.set()

    def _on_publish(self, client, userdata, mid, rc=None, properties=None):
        self.acked.put(mid)
        self.outbox.wake.set()

    def _on_message(self, client, userdata, msg):
        if msg.topic == self.cmd_topic:
            self.handle_command(msg.payload)
        elif msg.topic in self.cfg_topics:  # the app changed something: reload right now
            log.info("settings changed in the app - reloading")
            self.reload.set()

    # ---- writing (commands from the app)
    def ack(self, uid: str, stage: str, readback=None, error: str = "") -> None:
        """Tells the cloud how the command went. Signed. If the connection is down the cloud closes the command by itself (timed out)."""
        ts = now_iso()
        text = f"ack|{uid}|{stage}|{(float(readback) if readback is not None else None)!r}|{error[:200]}"
        body = {"uid": uid, "stage": stage, "readback": readback, "error": error[:200], "ts": ts, "sig": sign(self.secret, self.ack_topic, ts, text)}
        try:
            self.client.publish(self.ack_topic, json.dumps(body, separators=(",", ":")), qos=1)
        except Exception as exc:
            log.warning("could not report command %s (%s)", uid, type(exc).__name__)

    def handle_command(self, payload: bytes) -> None:
        """Checks a command: signature, expiry, not seen before, sensor really writable, value inside the limits. Then queues it."""
        try:
            b = json.loads(payload.decode())
            uid, dev_id, reg_id, raw, exp, ts, sig = str(b["uid"]), int(b["dev"]), int(b["reg"]), float(b["raw"]), str(b["exp"]), str(b["ts"]), str(b["sig"])
        except (ValueError, KeyError, TypeError):
            return
        text = f"cmd|{uid}|{dev_id}|{reg_id}|{raw!r}|{exp}"
        if not hmac.compare_digest(sign(self.secret, self.cmd_topic, ts, text), sig):
            log.warning("a command with a wrong signature was ignored")
            return
        if uid in self.seen:
            return
        self.seen[uid] = time.monotonic()
        while len(self.seen) > 500:
            self.seen.popitem(last=False)
        try:
            expires = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        except ValueError:
            return
        if datetime.now(timezone.utc) + timedelta(seconds=CLOCK_OFFSET) > expires:
            log.warning("command %s arrived after its expiry and was NOT run", uid)
            self.ack(uid, "failed", None, "The command arrived too late and was not run.")
            return
        spec = self.specs.get((dev_id, reg_id))
        if not spec:
            self.ack(uid, "failed", None, "This plant PC does not know that point (settings not loaded yet?).")
            return
        dev, reg, key = spec
        scale = float(reg.get("scale_factor") or 1.0)
        is_bit = reg.get("register_type") == "coil" or reg.get("data_type") == "bool"
        if not reg.get("writable"):
            self.ack(uid, "failed", None, "This point is not marked writable.")
            return
        if is_bit and raw not in (0.0, 1.0):
            self.ack(uid, "failed", None, "On / off points accept 0 or 1 only.")
            return
        if not is_bit:
            lo, hi = reg.get("write_min"), reg.get("write_max")
            if lo is None or hi is None or not (lo - 1e-9 <= raw * scale <= hi + 1e-9):
                self.ack(uid, "failed", None, "The value is outside the limits set for this point.")
                return
        q = self.cmdq.get(key)
        if q is None:
            self.ack(uid, "failed", None, "The connection to this device is not active.")
            return
        q.put((uid, dev, reg, raw, expires))
        self.ack(uid, "delivered")

    def run_commands(self, key: tuple, execute) -> None:
        """Called by the thread that owns the connection, before it reads: runs the waiting writes one by one."""
        q = self.cmdq.get(key)
        while q is not None and not q.empty():
            try:
                uid, dev, reg, raw, expires = q.get_nowait()
            except queue.Empty:
                return
            if datetime.now(timezone.utc) + timedelta(seconds=CLOCK_OFFSET) > expires:
                self.ack(uid, "failed", None, "The command waited too long and was not run.")
                continue
            try:
                back = execute(dev, reg, raw)
                log.info("wrote %s / %s = %s (PLC reports %s)", dev["name"], reg["metric_name"], raw, back)
                self.ack(uid, "written", back)
            except CommError as exc:
                log.warning("write to %s / %s failed: %s", dev["name"], reg["metric_name"], exc)
                self.ack(uid, "failed", None, str(exc))
            except Exception as exc:
                log.error("write to %s / %s failed (%s: %s)", dev["name"], reg["metric_name"], type(exc).__name__, exc)
                self.ack(uid, "failed", None, "Unexpected problem on the plant PC while writing.")

    def _settle(self) -> None:
        """Sender thread only: apply the cloud's receipts, or forget everything in flight after a lost connection."""
        if self.link_reset.is_set():
            self.link_reset.clear()
            self.inflight.clear()
            self.sent.clear()
            while True:
                try:
                    self.acked.get_nowait()
                except queue.Empty:
                    break
            return
        while True:
            try:
                mid = self.acked.get_nowait()
            except queue.Empty:
                return
            row = self.inflight.pop(mid, None)
            if row is not None:
                self.sent.discard(row)
                self.outbox.remove(row)

    def pump_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.outbox.wake.wait(1.0)
                self.outbox.wake.clear()
                self._settle()
                if not self.connected.is_set():
                    continue
                skip = set(self.sent)
                rows = []
                if self.outbox.backlog() > LIVE_LANE_AT:      # a long outage: the live readings must not wait behind hours of old ones
                    rows = self.outbox.newest(skip, LIVE_LANE_ROWS)
                    skip |= {r[0] for r in rows}
                rows += self.outbox.batch(skip, 200)
                for row_id, topic, payload in rows:
                    info = self.client.publish(topic, resign(self.secret, topic, payload), qos=1)
                    if info.rc != 0:
                        break
                    self.inflight[info.mid] = row_id
                    self.sent.add(row_id)
                self._settle()
            except Exception as exc:  # never let the sender thread die
                log.warning("sender problem (%s) - continuing", type(exc).__name__)
                time.sleep(1)

    def set_health(self, dev_id: int, ok: bool) -> None:
        if self.health.get(dev_id) is not ok:
            self.health[dev_id] = ok
            self.hb_now.set()

    def health_text(self) -> str:
        return ",".join(f"{i}:{1 if ok else 0}" for i, ok in sorted(dict(self.health).items()))

    def heartbeat_loop(self) -> None:
        while not self.stop.is_set():
            self.hb_now.wait(HEARTBEAT)
            self.hb_now.clear()
            if self.stop.is_set():
                break
            try:
                if self.connected.is_set():
                    self.client.publish(self.hb_topic, status_payload(self.secret, self.hb_topic, "online", now_iso(), self.info(), self.health_text()), qos=0)
            except Exception:
                pass
            time.sleep(0.2)   # a device that flaps never makes this loop spin

    def info(self) -> dict:
        try:
            ok = sum(1 for v in self.health.values() if v)
            return {"ver": VERSION, "ok": ok, "total": self.dev_total, "backlog": self.outbox.backlog()}
        except Exception:
            return {"ver": VERSION}

    # ---- settings from the cloud
    def fetch(self):
        global CLOCK_OFFSET
        who = f"{self.company}:{self.secret}" if self.legacy else f"{self.company}:{self.gid}:{self.secret}"
        req = urllib.request.Request(self.api + "/api/v1/gateway/devices", headers={"Authorization": f"Gateway {who}"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=15) as resp:
            cfg = json.loads(resp.read().decode())
        t1 = time.time()
        server = cfg.pop("server_time", None)  # taken out so it never counts as a settings change
        if server:
            try:
                srv = datetime.fromisoformat(str(server).replace("Z", "+00:00")).timestamp()
                offset = srv - (t0 + t1) / 2
                if abs(offset - CLOCK_OFFSET) > 1.0:
                    if abs(offset) > 5 and abs(CLOCK_OFFSET) <= 5:
                        log.warning("this PC's clock is %.0f seconds %s the cloud's time - readings are stamped with the corrected time",
                                    abs(offset), "behind" if offset > 0 else "ahead of")
                    CLOCK_OFFSET = offset
            except ValueError:
                pass
        return cfg

    def apply(self, cfg: dict) -> None:
        h = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
        if h == self.cfg_hash:
            return
        self.cfg_hash = h
        self.poll = min(max(float(cfg.get("poll_seconds") or self.poll), 0.25), 60.0)
        wanted = [d for d in cfg["devices"] if d["registers"]]
        for key in list(self.workers):  # stop every old reader, start the new set (a reload takes well under a second)
            self.workers.pop(key)[0].set()
        self.health.clear()
        old = self.cmdq
        self.cmdq, self.specs = {}, {}
        for q in old.values():     # a settings reload must not leave a waiting command unanswered
            while not q.empty():
                try:
                    self.ack(q.get_nowait()[0], "failed", None, "The settings were reloaded before the command could run. Send it again.")
                except queue.Empty:
                    break
        self.dev_total = len(wanted)
        modbus = cfg.get("modbus", {})
        groups: dict[tuple, list[dict]] = {}
        for dev in wanted:
            if dev["protocol"] in MODBUS_ALL:
                groups.setdefault(link_key(dev), []).append(dev)
            elif dev["protocol"] == "OPC-UA":
                groups[("opcua", dev["id"])] = [dev]
        for key, devs in groups.items():
            self.cmdq[key] = queue.Queue()
            for d in devs:
                for r in d["registers"]:
                    self.specs[(d["id"], r["id"])] = (d, r, key)
            ev = threading.Event()
            target = self.opcua_loop if key[0] == "opcua" else self.modbus_loop
            t = threading.Thread(target=target, args=(key, devs, modbus, ev), daemon=True, name="-".join(str(k) for k in key))
            t.start()
            self.workers[key] = (ev, t)
        log.info("reading %d device(s) on %d connection(s), %d sensor(s)", len(wanted), len(groups), sum(len(d["registers"]) for d in wanted))

    def _emit(self, last: dict, readings: list[tuple[dict, float]]) -> None:
        for reg, value in readings:
            prev = last.get(reg["id"])
            if prev is None or prev[0] != value or time.monotonic() - prev[1] >= RESEND_UNCHANGED:
                topic = f"stech/{self.company}/r{reg['id']}" if self.legacy else f"stech/{self.company}/gw/{self.gid}/r{reg['id']}"
                self.outbox.add(topic, reading_payload(self.secret, topic, value, now_iso()))
                last[reg["id"]] = (value, time.monotonic())

    def modbus_loop(self, key: tuple, devices: list[dict], modbus: dict, stop: threading.Event) -> None:
        """One thread per cable / converter. Its devices are read one after the other; a silent device is retried with
        growing pauses so it never slows down the others, and a dead link is reconnected with growing pauses."""
        link = ModbusLink(devices, modbus)
        last: dict[int, tuple[float, float]] = {}
        dev_fail = {d["id"]: 0 for d in devices}      # consecutive failures per device
        dev_next = {d["id"]: 0.0 for d in devices}    # do not ask this device before this time
        link_fail, link_next, link_down = 0, 0.0, False
        while not (stop.is_set() or self.stop.is_set()):
            started = time.monotonic()
            try:
                if started >= link_next:
                    try:
                        link.connect()
                        if link_down:
                            log.info("connection %s is back", link.host if link.serial else f"{link.host}:{link.port}")
                            link_down = False
                        link_fail = 0
                    except LinkError as exc:
                        link_fail += 1
                        link_next = time.monotonic() + min(2 ** (link_fail - 1), MAX_BACKOFF)
                        if not link_down:
                            log.warning("connection %s is down: %s", link.host if link.serial else f"{link.host}:{link.port}", exc)
                            link_down = True
                        for d in devices:
                            self.set_health(d["id"], False)
                    if not link_down:
                        self.run_commands(key, lambda dev, reg, raw: link.write(dev, reg, raw))
                        for dev in devices:
                            if time.monotonic() < dev_next[dev["id"]]:
                                continue
                            try:
                                self._emit(last, link.read_device(dev))
                                self.set_health(dev["id"], True)
                                if dev_fail[dev["id"]]:
                                    log.info("device %s is answering again", dev["name"])
                                dev_fail[dev["id"]] = 0
                            except CommError as exc:
                                n = dev_fail[dev["id"]] = dev_fail[dev["id"]] + 1
                                dev_next[dev["id"]] = time.monotonic() + min(2 ** (n - 1), MAX_BACKOFF)
                                self.set_health(dev["id"], False)
                                if n == 1:
                                    where = f"slave {dev.get('unit_id') if dev.get('unit_id') is not None else modbus.get('unit_id', 1)} on {dev['host']}" \
                                        if link.serial else f"{dev['host']}:{dev['port']}"
                                    log.warning("device %s (%s) is not answering: %s", dev["name"], where, exc)
                                if not link.serial:
                                    link.close()
                            except LinkError as exc:   # the port / socket broke in the middle of a read
                                link.close()
                                link_next, link_down = time.monotonic() + 1, True
                                log.warning("connection %s was lost: %s", link.host, exc)
                                break
            except Exception as exc:  # a bug must never stop reading: log it and go on
                log.error("reader problem (%s: %s) - continuing", type(exc).__name__, exc)
                link.close()
                time.sleep(1)
            stop.wait(max(0.05, self.poll - (time.monotonic() - started)))
        link.close()

    def opcua_loop(self, key: tuple, devices: list[dict], modbus: dict, stop: threading.Event) -> None:
        dev = devices[0]
        reader, last, fails = OpcuaReader(dev), {}, 0
        next_try = 0.0
        while not (stop.is_set() or self.stop.is_set()):
            started = time.monotonic()
            self.run_commands(key, lambda dev, reg, raw: reader.write(reg, raw))
            if started >= next_try:
                try:
                    self._emit(last, reader.read())
                    self.set_health(dev["id"], True)
                    if fails:
                        log.info("device %s is answering again", dev["name"])
                    fails = 0
                except Exception as exc:
                    fails += 1
                    next_try = time.monotonic() + min(2 ** (fails - 1), MAX_BACKOFF)
                    self.set_health(dev["id"], False)
                    if fails == 1:
                        log.warning("device %s (%s:%s) is not answering: %s", dev["name"], dev["host"], dev["port"], type(exc).__name__)
            stop.wait(max(0.05, self.poll - (time.monotonic() - started)))
        reader.close()

    # ---- signed self-update
    def update_who(self) -> str:
        return f"{self.company}:{self.secret}" if self.legacy else f"{self.company}:{self.gid}:{self.secret}"

    def check_update(self) -> None:
        key_b64 = self.link.get("update_key")
        if not key_b64 or self.link.get("auto_update") is False:
            return
        q = "current=" + VERSION
        if under_launcher():
            q += "&launcher=" + os.environ["STECH_LAUNCHER"] + "&platform=" + my_platform()
        report = _jread("update_report.json")
        if report:
            q += "&" + urllib.parse.urlencode({k: report.get(k, "") for k in ("event", "from", "to", "reason")})
        req = urllib.request.Request(self.api + "/api/v1/gateway/update?" + q, headers={"Authorization": f"Gateway {self.update_who()}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        if report and data.get("reported"):
            _jdel("update_report.json")
        if data.get("program") and under_launcher() and self.update_program(data["program"], key_b64):
            return
        if not data.get("update"):
            return
        version, sha, sig = str(data["version"]), str(data["sha256"]), str(data["signature"])
        blocked = _jread("update_blocked.json") or {}
        if vtuple(version) <= vtuple(VERSION):
            return                                            # never an older or the same version
        if blocked.get("version") and vtuple(version) <= vtuple(blocked["version"]):
            return                                            # this version already failed here
        body = base64.b64decode(data["script"])
        if hashlib.sha256(body).hexdigest() != sha:
            log.error("update %s refused: the file does not match its checksum", version)
            return
        if not ed25519_verify(base64.b64decode(key_b64), release_message(version, sha), base64.b64decode(sig)):
            log.error("update %s refused: the signature is not valid", version)
            return
        try:
            compile(body, "plant_gateway.py", "exec")
        except SyntaxError:
            log.error("update %s refused: it does not compile", version)
            return
        need = re.search(rb"^NEEDS_LAUNCHER = (\d+)", body, re.M)
        if need and int(need.group(1)) > int(os.environ.get("STECH_LAUNCHER", "99")):
            log.warning("update %s needs a newer gateway program than this one: download the gateway once more from the app", version)
            return
        if not re.search(rb'^VERSION = "' + re.escape(version.encode()) + rb'"', body, re.M):
            log.error("update %s refused: the version inside the file differs", version)
            return
        cur = script_file()
        new, bak = cur + ".new", cur + ".bak"
        with open(new, "wb") as f:
            f.write(body)
        try:
            test = subprocess.run([sys.executable, new, "--selftest"], capture_output=True, timeout=60, cwd=HERE)
            if test.returncode != 0:
                raise RuntimeError("self-test exit %s" % test.returncode)
        except Exception as exc:
            log.error("update %s refused: the self-test failed (%s)", version, exc)
            os.remove(new)
            return
        with open(cur, "rb") as f, open(bak, "wb") as g:
            g.write(f.read())
        _jwrite("update_pending.json", {"from": VERSION, "to": version, "starts": 0, "at": time.time()})
        os.replace(new, cur)
        log.warning("UPDATE: signature verified, installing version %s (was %s). Restarting now.", version, VERSION)
        self.restart_code = EXIT_UPDATED
        self.stop.set()
        self.reload.set()

    def update_program(self, prog: dict, key_b64: str) -> bool:
        """A newer SIGNED ready-made program exists: download it next to the running one, check it, ask the supervisor to swap it in."""
        version, sha, sig = str(prog["version"]), str(prog["sha256"]), str(prog["signature"])
        mine = os.environ["STECH_LAUNCHER"]
        blocked = _jread("program_blocked.json") or {}
        if vtuple(version) <= vtuple(mine) or (blocked.get("version") and vtuple(version) <= vtuple(blocked["version"])):
            return False
        if not ed25519_verify(base64.b64decode(key_b64), program_message(my_platform(), version, sha), base64.b64decode(sig)):
            log.error("program %s refused: the signature is not valid", version)
            return False
        exe = sys.executable
        new = exe[:-4] + ".new.exe" if exe.lower().endswith(".exe") else exe + ".new"   # same name rule as the launcher
        size = int(prog.get("size") or 0)
        if not 1_000_000 < size < 400_000_000:
            log.error("program %s refused: unexpected size", version)
            return False
        req = urllib.request.Request(self.api + "/api/v1/gateway/update/program?platform=" + my_platform(), headers={"Authorization": f"Gateway {self.update_who()}"})
        h = hashlib.sha256()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, open(new, "wb") as out:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
                    out.write(chunk)
                    if out.tell() > size + 1024:
                        raise RuntimeError("larger than announced")
            if h.hexdigest() != sha:
                raise RuntimeError("checksum differs")
            if os.name != "nt":
                os.chmod(new, 0o755)
            test = subprocess.run([new, script_file(), "--selftest"], capture_output=True, timeout=120, cwd=HERE)
            if test.returncode != 0:
                raise RuntimeError("self-test exit %s" % test.returncode)
        except Exception as exc:
            log.error("program %s refused: %s", version, exc)
            try:
                os.remove(new)
            except OSError:
                pass
            return False
        _jwrite("program_pending.json", {"from": mine, "to": version, "starts": 0})
        log.warning("UPDATE: new gateway program %s verified (was %s). Switching now.", version, mine)
        self.restart_code = EXIT_PROGRAM_SWAP
        self.stop.set()
        self.reload.set()
        return True

    def update_loop(self) -> None:
        t_next = time.monotonic() + UPDATE_FIRST
        pending = _jread("update_pending.json")
        deadline = time.monotonic() + CONFIRM_WITHIN if pending else None
        pprog = _jread("program_pending.json") if under_launcher() else None
        pdeadline = time.monotonic() + CONFIRM_WITHIN if pprog else None
        while not self.stop.is_set():
            if pdeadline is not None:
                if self.connected.is_set() and self.cfg_hash:
                    _jdel("program_pending.json")
                    _jwrite("update_report.json", {"event": "program_updated", "from": pprog.get("from", ""), "to": os.environ.get("STECH_LAUNCHER", "")})
                    log.info("program update confirmed: program %s is running and connected", os.environ.get("STECH_LAUNCHER"))
                    pdeadline = None
                    t_next = time.monotonic() + 5
                elif time.monotonic() > pdeadline:
                    log.error("the new gateway program could not connect to the cloud: the old program is put back")
                    self.restart_code = EXIT_PROGRAM_BACK
                    self.stop.set()
                    self.reload.set()
                    return
            if deadline is not None:
                if self.connected.is_set() and self.cfg_hash:
                    _jdel("update_pending.json")
                    _jwrite("update_report.json", {"event": "updated", "from": pending.get("from", ""), "to": VERSION})
                    log.info("update confirmed: version %s is running and connected", VERSION)
                    deadline = None
                    t_next = time.monotonic() + 5
                elif time.monotonic() > deadline:
                    rollback_update("it could not connect to the cloud")
                    self.restart_code = EXIT_UPDATED
                    self.stop.set()
                    self.reload.set()
                    return
            elif time.monotonic() >= t_next:
                t_next = time.monotonic() + UPDATE_EVERY
                try:
                    self.check_update()
                except Exception as exc:
                    log.warning("update check failed (%s)", type(exc).__name__)
            self.stop.wait(2)

    def cache_path(self) -> str:
        return os.path.join(HERE, "devices_cache.json")

    def _dead_workers(self) -> bool:
        return any(not t.is_alive() for _, t in self.workers.values())

    def run(self) -> None:
        self.start_mqtt()
        threading.Thread(target=self.pump_loop, daemon=True).start()
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        threading.Thread(target=self.update_loop, daemon=True, name="update").start()
        log.info("STECH iVIEW plant gateway %s started: company '%s', gateway '%s' (%s)", VERSION, self.company, self.name, self.gid)
        last_fetch, warned, rejected, wait_s = -REFRESH_SECONDS, False, False, REFRESH_SECONDS
        while not self.stop.is_set():
            if self._dead_workers():  # a reader thread ended by itself (should never happen): start all readers again
                log.error("a reader stopped unexpectedly - restarting all readers")
                self.cfg_hash = None
                self.reload.set()
            if self.reload.is_set() or time.monotonic() - last_fetch >= wait_s:
                self.reload.clear()
                last_fetch = time.monotonic()
                wait_s = REFRESH_SECONDS
                try:
                    cfg = self.fetch()
                    write_json_atomic(self.cache_path(), cfg)
                    self.apply(cfg)
                    warned = rejected = False
                except urllib.error.HTTPError as exc:
                    if exc.code == 401:
                        wait_s = 30  # a rejected key does not fix itself: ask again only every 30 seconds
                        if not rejected:
                            rejected = True
                            log.error(KEY_CANCELLED_TEXT + " Reading is paused; this script keeps asking every 30 seconds.")
                        if self.workers:  # stop reading: the cloud would throw the readings away anyway
                            for ev, _ in self.workers.values():
                                ev.set()
                            self.workers.clear()
                            self.health.clear()
                            self.cfg_hash = None
                    else:
                        wait_s = 15
                        log.warning("cannot get settings from the cloud (HTTP %s)", exc.code)
                except Exception as exc:
                    wait_s = 10
                    if not warned:
                        log.warning("cannot reach the cloud for settings (%s) - using the last known settings", type(exc).__name__)
                        warned = True
                    if not self.cfg_hash and os.path.isfile(self.cache_path()):
                        try:
                            with open(self.cache_path(), encoding="utf-8") as f:
                                self.apply(json.load(f))
                        except Exception:
                            pass
            self.reload.wait(0.5)
        for ev, _ in self.workers.values():
            ev.set()
        try:
            self.client.publish(self.status_topic, status_payload(self.secret, self.status_topic, "offline", "0"), qos=1, retain=True).wait_for_publish(2)
        except Exception:
            pass
        self.client.loop_stop()
        self.client.disconnect()


def main() -> int:
    if "--selftest" in sys.argv:  # used by the updater: the new file must at least load with every module it needs
        return 0
    setup_logging()
    if boot_guard():
        return EXIT_UPDATED
    path = os.path.join(HERE, "link.json")
    if not os.path.isfile(path):
        log.error("link.json is missing. In the app open the Gateway page and download the script again.")
        return 1
    with open(path, encoding="utf-8") as f:
        link = json.load(f)
    held = lock_instance(os.path.join(HERE, "gateway.lock"))  # keep this handle open for as long as the script runs
    if held is None:
        log.error("another copy of this gateway is already running on this PC - this one stops.")
        return EXIT_ALREADY_RUNNING
    gw = Gateway(link)
    try:
        gw.run()
    except KeyboardInterrupt:
        gw.stop.set()
        log.info("stopped")
    return gw.restart_code


if __name__ == "__main__":
    sys.exit(main())
