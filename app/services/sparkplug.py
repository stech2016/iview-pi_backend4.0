"""Sparkplug B (the Eclipse / Cirrus Link payload format that most industrial gateways speak) - written from the public specification.

Only what the cloud needs is here: DECODE the messages a gateway publishes (NBIRTH / NDATA / DBIRTH / DDATA / NDEATH ...) and ENCODE the few
messages the cloud sends (a write command DCMD, a "please send your birth again" NCMD). Plain protobuf wire format, no library.

  Payload : timestamp = 1 (uint64 ms), metrics = 2 (repeated Metric), seq = 3 (uint64)
  Metric  : name = 1, alias = 2, timestamp = 3, datatype = 4, is_historical = 6, is_null = 7,
            int_value = 10, long_value = 11, float_value = 12, double_value = 13, boolean_value = 14, string_value = 15
  Topic   : spBv1.0/<group id>/<NBIRTH|NDATA|NDEATH|NCMD|DBIRTH|DDATA|DDEATH|DCMD>/<edge node id>[/<device id>]
"""
import struct
from dataclasses import dataclass
from datetime import datetime, timezone

# datatype numbers of the specification
INT8, INT16, INT32, INT64, UINT8, UINT16, UINT32, UINT64, FLOAT, DOUBLE, BOOLEAN, STRING = range(1, 13)
SIGNED_BITS = {INT8: 8, INT16: 16, INT32: 32, INT64: 64}
UNSIGNED = {UINT8, UINT16, UINT32, UINT64}
NUMERIC = set(SIGNED_BITS) | UNSIGNED | {FLOAT, DOUBLE}

MAX_METRICS = 2000          # one message never carries more than this (a bigger one is cut, not trusted)
NAMESPACE = "spBv1.0"
MESSAGE_TYPES = ("NBIRTH", "NDEATH", "DBIRTH", "DDEATH", "NDATA", "DDATA", "NCMD", "DCMD", "STATE")


class SparkplugError(ValueError):
    pass


@dataclass
class Metric:
    name: str | None = None
    alias: int | None = None
    timestamp: int | None = None      # ms since 1970
    datatype: int | None = None
    is_null: bool = False
    historical: bool = False
    value: object = None              # int | float | bool | str | None

    def as_number(self) -> float | None:
        """The value as a number for the readings table: numbers and booleans (1 / 0). Text and nulls give None."""
        if self.is_null or self.value is None or isinstance(self.value, str):
            return None
        return float(self.value)


@dataclass
class Payload:
    timestamp: int | None = None
    seq: int | None = None
    metrics: list[Metric] | None = None


# ----------------------------------------------------------------------------------------------------------- wire reading
def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(buf) or shift > 63:
            raise SparkplugError("bad varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _fields(buf: bytes):
    """Yields (field number, wire type, value). value = int for varint / fixed, bytes for length-delimited."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        num, wire = key >> 3, key & 7
        if num == 0:
            raise SparkplugError("field 0")
        if wire == 0:
            val, i = _varint(buf, i)
        elif wire == 1:
            if i + 8 > n:
                raise SparkplugError("truncated")
            val, i = buf[i:i + 8], i + 8
        elif wire == 2:
            ln, i = _varint(buf, i)
            if i + ln > n:
                raise SparkplugError("truncated")
            val, i = buf[i:i + ln], i + ln
        elif wire == 5:
            if i + 4 > n:
                raise SparkplugError("truncated")
            val, i = buf[i:i + 4], i + 4
        else:
            raise SparkplugError("unsupported wire type")
        yield num, wire, val


def _signed(value: int, bits: int) -> int:
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


def _decode_metric(buf: bytes) -> Metric:
    m = Metric()
    raw = {}
    for num, wire, val in _fields(buf):
        if num == 1 and wire == 2:
            m.name = val.decode("utf-8", "replace")
        elif num == 2 and wire == 0:
            m.alias = val
        elif num == 3 and wire == 0:
            m.timestamp = val
        elif num == 4 and wire == 0:
            m.datatype = val
        elif num == 6 and wire == 0:
            m.historical = bool(val)
        elif num == 7 and wire == 0:
            m.is_null = bool(val)
        elif num in (10, 11, 14) and wire == 0:
            raw[num] = val
        elif num == 12 and wire == 5:
            raw[num] = struct.unpack("<f", val)[0]
        elif num == 13 and wire == 1:
            raw[num] = struct.unpack("<d", val)[0]
        elif num == 15 and wire == 2:
            raw[num] = val.decode("utf-8", "replace")
    dt = m.datatype
    if 14 in raw:
        m.value = bool(raw[14])
    elif 15 in raw:
        m.value = raw[15]
    elif 13 in raw:
        m.value = raw[13]
    elif 12 in raw:
        m.value = raw[12]
    elif 11 in raw:
        m.value = _signed(raw[11], 64) if dt in SIGNED_BITS or dt is None else raw[11]
    elif 10 in raw:
        m.value = _signed(raw[10], SIGNED_BITS[dt]) if dt in SIGNED_BITS and SIGNED_BITS[dt] <= 32 else raw[10]
    if dt == BOOLEAN and m.value is not None and not isinstance(m.value, bool):
        m.value = bool(m.value)
    return m


def decode(payload: bytes) -> Payload:
    """Sparkplug B payload -> Payload. Raises SparkplugError for anything that is not a well-formed payload."""
    if not payload:
        raise SparkplugError("empty")
    out = Payload(metrics=[])
    for num, wire, val in _fields(payload):
        if num == 1 and wire == 0:
            out.timestamp = val
        elif num == 3 and wire == 0:
            out.seq = val
        elif num == 2 and wire == 2:
            if len(out.metrics) >= MAX_METRICS:
                continue
            out.metrics.append(_decode_metric(val))
    return out


# ----------------------------------------------------------------------------------------------------------- wire writing
def _w_varint(n: int) -> bytes:
    n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _w_key(num: int, wire: int) -> bytes:
    return _w_varint((num << 3) | wire)


def _w_bytes(num: int, data: bytes) -> bytes:
    return _w_key(num, 2) + _w_varint(len(data)) + data


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def encode_metric(m: Metric) -> bytes:
    out = b""
    if m.name is not None:
        out += _w_bytes(1, m.name.encode())
    if m.alias is not None:
        out += _w_key(2, 0) + _w_varint(m.alias)
    out += _w_key(3, 0) + _w_varint(m.timestamp if m.timestamp is not None else now_ms())
    dt = m.datatype
    if dt is None:
        dt = BOOLEAN if isinstance(m.value, bool) else STRING if isinstance(m.value, str) else DOUBLE
    out += _w_key(4, 0) + _w_varint(dt)
    v = m.value
    if v is None:
        return out + _w_key(7, 0) + _w_varint(1)
    if dt == BOOLEAN:
        out += _w_key(14, 0) + _w_varint(1 if v else 0)
    elif dt == STRING:
        out += _w_bytes(15, str(v).encode())
    elif dt == FLOAT:
        out += _w_key(12, 5) + struct.pack("<f", float(v))
    elif dt == DOUBLE:
        out += _w_key(13, 1) + struct.pack("<d", float(v))
    elif dt in (INT64, UINT64):
        out += _w_key(11, 0) + _w_varint(int(v))
    else:   # Int8/16/32, UInt8/16/32: int_value carries the two's complement of the value as 32 bits
        out += _w_key(10, 0) + _w_varint(int(v) & 0xFFFFFFFF)
    return out


def encode(metrics: list[Metric], seq: int = 0, timestamp: int | None = None) -> bytes:
    out = _w_key(1, 0) + _w_varint(timestamp if timestamp is not None else now_ms())
    for m in metrics:
        out += _w_bytes(2, encode_metric(m))
    return out + _w_key(3, 0) + _w_varint(seq)


# ----------------------------------------------------------------------------------------------------------- topics
@dataclass
class Topic:
    group: str
    kind: str
    edge: str
    device: str | None = None


def parse_topic(topic: str) -> Topic | None:
    p = topic.split("/")
    if len(p) not in (4, 5) or p[0] != NAMESPACE or p[2] not in MESSAGE_TYPES or not all(p[1:]):
        return None
    if (len(p) == 5) != p[2].startswith("D"):
        return None
    return Topic(p[1], p[2], p[3], p[4] if len(p) == 5 else None)


def topic_for(group: str, kind: str, edge: str, device: str | None = None) -> str:
    return "/".join([NAMESPACE, group, kind, edge] + ([device] if device else []))


def datatype_for_register(data_type: str | None) -> int:
    """The Sparkplug datatype used when the cloud writes a value to a point of the app (by the point's data type)."""
    return {"bool": BOOLEAN, "int16": INT16, "uint16": UINT16, "int32": INT32, "uint32": UINT32, "int64": INT64, "uint64": UINT64,
            "float32": FLOAT}.get((data_type or "").lower(), DOUBLE)
