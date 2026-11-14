import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from app.core import geo, normalize
from app.core.constants import MODBUS_ALL, MODBUS_SERIAL, REGIONS

Protocol = Annotated[Literal["Modbus TCP", "Modbus RTU over TCP", "Modbus UDP", "Modbus TLS", "Modbus RTU", "Modbus ASCII", "MQTT", "OPC-UA"], BeforeValidator(normalize.protocol)]
Severity = Annotated[Literal["critical", "major", "minor", "advisory"], BeforeValidator(normalize.severity)]
Parity = Annotated[Literal["N", "E", "O"], BeforeValidator(normalize.parity)]
Order = Annotated[Literal["big", "little"], BeforeValidator(normalize.order)]
_HOST = re.compile(r"^[A-Za-z0-9._:/\-]{1,255}$")
_SERIAL_PORT = re.compile(r"^(COM\d{1,3}|/dev/[A-Za-z0-9_./\-]{2,60})$")   # COM3, /dev/ttyUSB0, /dev/ttyS1
_METRIC = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


DATA_TYPES = {"bool", "int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64"}
_NODE = re.compile(r"^ns=\d+;[sigb]=\S.*$")          # OPC-UA e.g. ns=2;s=Boiler.Temperature
_TOPIC = re.compile(r"^([A-Za-z0-9_\-./]{3,255}|ext:pl[0-9a-f]{10}:[^\x00-\x1f\x7f]{1,120})$")     # MQTT topic, no wildcards  (or: a point of a customer gateway, made by the cloud)
_JSON_FIELD = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")     # a field of the message (V2.0: a gateway's own tag name may contain spaces or /)


class RegisterIn(BaseModel):
    sensor_type: str | None = Field(default=None, max_length=32)
    metric_name: str
    unit: str | None = Field(default=None, max_length=16)
    data_type: str = "float32"
    scale_factor: float = 1.0
    register_type: Annotated[Literal["coil", "discrete", "input", "holding"], BeforeValidator(normalize.register_type)] | None = None
    register_address: int | None = Field(default=None, ge=0, le=65535)
    bit_index: int | None = Field(default=None, ge=0, le=15)   # Modbus holding / input register: read ONE bit of the register
    node_id: str | None = Field(default=None, max_length=255)
    topic: str | None = Field(default=None, max_length=255)
    json_field: str | None = Field(default=None, max_length=64)
    low_limit: float | None = None
    high_limit: float | None = None
    alarm_severity: Severity = "major"

    @field_validator("metric_name")
    @classmethod
    def _m(cls, v):
        if not _METRIC.match(v):
            raise ValueError("Metric name may only contain letters, numbers, dot, dash, underscore (max 64)")
        return v

    @field_validator("data_type", mode="before")
    @classmethod
    def _dt_case(cls, v):
        return normalize.data_type(v)

    @field_validator("data_type")
    @classmethod
    def _dt(cls, v):
        if v not in DATA_TYPES:
            raise ValueError("Data type must be one of: " + ", ".join(sorted(DATA_TYPES)))
        return v

    @field_validator("scale_factor")
    @classmethod
    def _scale(cls, v):
        if v == 0 or abs(v) > 1e9:
            raise ValueError("Scale factor must be non-zero (for example 1, 0.1, 0.01)")
        return v

    @field_validator("node_id")
    @classmethod
    def _node(cls, v):
        if v is not None and not _NODE.match(v):
            raise ValueError("OPC-UA Node ID must look like ns=2;s=Boiler.Temperature (or ns=2;i=1234)")
        return v

    @field_validator("topic")
    @classmethod
    def _topic(cls, v):
        if v is not None and not _TOPIC.match(v):
            raise ValueError("MQTT topic may contain letters, numbers and / _ - . only (no + or # wildcards)")
        return v

    @field_validator("json_field")
    @classmethod
    def _jf(cls, v):
        if v is not None and not _JSON_FIELD.match(v):
            raise ValueError("JSON field may contain letters, numbers and . _ - only")
        return v


def check_register_for_protocol(protocol: str, r: "RegisterIn", company: str | None = None) -> str | None:
    """Human-readable problem, or None if the register fits the device protocol."""
    if protocol in MODBUS_ALL:
        if r.register_type is None or r.register_address is None:
            return f"'{r.metric_name}': Modbus needs a register type and an address."
        if r.node_id or r.topic:
            return f"'{r.metric_name}': remove the OPC-UA node / MQTT topic; this device uses Modbus."
        if r.bit_index is not None and r.register_type not in ("holding", "input"):
            return f"'{r.metric_name}': a bit number can only be used with a holding or input register (coils and discrete inputs are already single bits)."
        if r.bit_index is not None and r.data_type != "bool":
            return f"'{r.metric_name}': to read one bit of a register, set the data type to bool."
    elif protocol == "OPC-UA":
        if not r.node_id:
            return f"'{r.metric_name}': OPC-UA needs a Node ID."
        if r.register_address is not None or r.topic or r.bit_index is not None:
            return f"'{r.metric_name}': remove the Modbus address / bit / MQTT topic; this device uses OPC-UA."
    elif protocol == "MQTT":
        if not r.topic:
            return f"'{r.metric_name}': MQTT needs a topic."
        if r.topic.startswith("ext:"):     # V2.0: a point chosen from a customer gateway's Detected list (made by the cloud, not typed by a person)
            return None if r.json_field else f"'{r.metric_name}': a customer-gateway sensor needs its tag name."
        if company and not r.topic.startswith(f"stech/{company}/"):
            return f"'{r.metric_name}': topic must start with stech/{company}/ so the broker keeps your data isolated."
        if r.register_address is not None or r.node_id or r.bit_index is not None:
            return f"'{r.metric_name}': remove the Modbus address / bit / OPC-UA node; this device uses MQTT."
    return None


class RegisterOut(RegisterIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_id: int
    is_main: bool = False
    writable: bool = False            # v1.1.5: may a person send a value to this sensor? (set by an administrator with limits)
    write_min: float | None = None
    write_max: float | None = None
    stuck_minutes: int | None = None


class DeviceCreate(BaseModel):
    device_name: str = Field(min_length=1, max_length=120)
    protocol: Protocol
    ip_address: str
    port: int = Field(ge=0, le=65535)   # 0 for serial devices (the cable has no port number)
    country: str | None = Field(default=None, max_length=64)
    state: str | None = None
    region: str | None = None
    city: str | None = Field(default=None, max_length=80)
    plant_name: str | None = Field(default=None, max_length=120)
    # ---- Modbus connection details (leave empty for the company default)
    unit_id: int | None = Field(default=None, ge=0, le=255)             # slave / unit id (serial: 1-247)
    serial_baud: int | None = Field(default=None, ge=300, le=1_000_000)
    serial_parity: Parity | None = None
    serial_databits: Literal[7, 8] | None = None
    serial_stopbits: Literal[1, 2] | None = None
    byte_order: Order | None = None
    word_order: Order | None = None
    address_base: Literal[0, 1] = 0
    timeout_ms: int | None = Field(default=None, ge=200, le=30000)
    retries: int | None = Field(default=None, ge=0, le=5)
    plant_pc: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")   # which plant PC reads it (empty = the first one of its State + Region)
    registers: list[RegisterIn] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def _modbus_details(self):
        serial_fields = (self.serial_baud, self.serial_parity, self.serial_databits, self.serial_stopbits)
        if self.protocol in MODBUS_SERIAL:
            if not _SERIAL_PORT.match(self.ip_address):
                raise ValueError("Serial port must look like COM3 (Windows) or /dev/ttyUSB0 (Linux)")
            self.port = 0
            ascii_ = self.protocol == "Modbus ASCII"
            self.serial_baud = self.serial_baud or 9600
            self.serial_parity = self.serial_parity or ("E" if ascii_ else "N")
            self.serial_databits = self.serial_databits or (7 if ascii_ else 8)
            self.serial_stopbits = self.serial_stopbits or 1
            if self.unit_id is not None and not 1 <= self.unit_id <= 247:
                raise ValueError("A serial Modbus slave address must be between 1 and 247")
            if self.unit_id is None:
                raise ValueError("Enter the slave address (1-247) of this serial device")
        else:
            if "/" in self.ip_address:
                raise ValueError("Enter a valid IP address or hostname")
            if any(v is not None for v in serial_fields):
                raise ValueError("Baud rate, parity, data bits and stop bits belong to serial (RTU / ASCII) devices only")
            if self.port < 1:
                raise ValueError("Enter the port number (Modbus TCP is usually 502)")
        if self.protocol not in MODBUS_ALL and (self.unit_id is not None or self.byte_order or self.word_order or self.address_base or self.timeout_ms is not None or self.retries is not None):
            raise ValueError("Slave id, byte / word order, address base, timeout and retries are Modbus settings")
        return self

    @field_validator("ip_address")
    @classmethod
    def _ip(cls, v):
        if not _HOST.match(v):
            raise ValueError("Enter a valid IP address or hostname")
        return v

    @field_validator("country")
    @classmethod
    def _country(cls, v):
        if v is not None and not geo.is_country(v):
            raise ValueError("Select a valid country")
        return v

    @field_validator("state")
    @classmethod
    def _state(cls, v):
        if v is not None and not geo.is_state(v):
            raise ValueError("Select a valid State / province")
        return v

    @field_validator("state", mode="before")
    @classmethod
    def _state_case(cls, v):
        return normalize.state(v) if v is not None else v

    @field_validator("region", mode="before")
    @classmethod
    def _region_case(cls, v):
        return normalize.region(v) if v is not None else v

    @field_validator("region")
    @classmethod
    def _region(cls, v):
        if v is not None and v not in REGIONS:
            raise ValueError("Region must be one of North, South, East, West")
        return v


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    device_name: str
    protocol: str
    ip_address: str
    port: int
    company_name: str
    country: str = "India"
    state: str
    region: str
    city: str | None = None
    plant_name: str | None = None
    unit_id: int | None = None
    serial_baud: int | None = None
    serial_parity: str | None = None
    serial_databits: int | None = None
    serial_stopbits: int | None = None
    byte_order: str | None = None
    word_order: str | None = None
    address_base: int = 0
    timeout_ms: int | None = None
    retries: int | None = None
    created_at: datetime
    registers: list[RegisterOut] = []
    last_seen: datetime | None = None
    active_alarms: int = 0
    online: bool = False
    status: str = "offline"          # online | stale (data late) | offline (PLC does not answer) | plant_offline (plant PC silent) | waiting (no plant PC yet)
    plant_pc: str | None = None       # the plant PC (gateway id) that reads this device; None for MQTT devices
    plant_pc_name: str | None = None
    maintenance_until: datetime | None = None
    maintenance_note: str | None = None
    in_maintenance: bool = False
    latitude: float | None = None
    longitude: float | None = None


class ReadingIn(BaseModel):
    metric_name: str = Field(min_length=1, max_length=64)
    value: float
    timestamp: datetime | None = None


class TelemetryBatchIn(BaseModel):
    readings: list[ReadingIn] = Field(min_length=1, max_length=5000)


class TelemetryIngestOut(BaseModel):
    received: int
    inserted: int
    duplicates: int


class AlarmOut(BaseModel):
    id: int
    device_id: int
    device_name: str | None = None
    place: str | None = None      # "Coimbatore - Plant 2 Boiler House" (City + Plant name), when set
    metric_name: str | None = None
    severity: str
    message: str
    raised_at: datetime
    cleared_at: datetime | None = None
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    muted: str | None = None             # 'shelved' | 'maintenance' | None
    shelved_until: datetime | None = None
    shelved_by: str | None = None
    shelve_reason: str | None = None
    comment_count: int = 0


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    alarm_id: int
    channel: str
    sent_at: datetime
    read_at: datetime | None = None


class FcmIn(BaseModel):
    token: str = Field(min_length=10, max_length=512)
    device_info: str | None = Field(default=None, max_length=255)


class DeviceUpdate(BaseModel):
    """Details of an existing device. Send only what changes; everything else stays as it is."""
    device_name: str | None = Field(default=None, min_length=1, max_length=120)
    protocol: Protocol | None = None
    ip_address: str | None = None
    port: int | None = Field(default=None, ge=0, le=65535)
    city: str | None = Field(default=None, max_length=80)
    plant_name: str | None = Field(default=None, max_length=120)
    unit_id: int | None = Field(default=None, ge=0, le=255)
    serial_baud: int | None = Field(default=None, ge=300, le=1_000_000)
    serial_parity: Parity | None = None
    serial_databits: Literal[7, 8] | None = None
    serial_stopbits: Literal[1, 2] | None = None
    byte_order: Order | None = None
    word_order: Order | None = None
    address_base: Literal[0, 1] | None = None
    timeout_ms: int | None = Field(default=None, ge=200, le=30000)
    retries: int | None = Field(default=None, ge=0, le=5)
    plant_pc: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")


class PlacePatch(BaseModel):
    city: str | None = Field(default=None, max_length=80)
    plant_name: str | None = Field(default=None, max_length=120)


class PlantPcIn(BaseModel):
    plant_pc: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")
