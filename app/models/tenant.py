from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.timeutil import utcnow
from app.db.base import TenantBase


class Device(TenantBase):
    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("company_name", "device_name", "ip_address", name="uq_device_company_name_ip"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_name: Mapped[str] = mapped_column(String(120))
    protocol: Mapped[str] = mapped_column(String(20))  # Modbus TCP / RTU / ASCII / RTU over TCP / UDP / TLS | MQTT | OPC-UA
    ip_address: Mapped[str] = mapped_column(String(255))  # host, or the serial port name (COM3, /dev/ttyUSB0) for Modbus RTU / ASCII
    port: Mapped[int] = mapped_column(Integer)            # 0 for serial devices
    # ---- Modbus connection details (NULL = the company default)
    unit_id: Mapped[int | None] = mapped_column(Integer, nullable=True)           # Modbus slave / unit id
    serial_baud: Mapped[int | None] = mapped_column(Integer, nullable=True)
    serial_parity: Mapped[str | None] = mapped_column(String(1), nullable=True)   # N | E | O
    serial_databits: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 7 | 8
    serial_stopbits: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 1 | 2
    byte_order: Mapped[str | None] = mapped_column(String(6), nullable=True)      # big | little (bytes inside one 16-bit register)
    word_order: Mapped[str | None] = mapped_column(String(6), nullable=True)      # big | little (16-bit words of 32/64-bit values)
    address_base: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # 0 = addresses start at 0, 1 = manual counts from 1
    timeout_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plant_pc: Mapped[str | None] = mapped_column(String(32), nullable=True)   # which plant PC (gateway id) reads this device; NULL = the first plant PC of its State + Region
    company_name: Mapped[str] = mapped_column(String(63))
    state: Mapped[str] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(10))
    city: Mapped[str | None] = mapped_column(String(80), nullable=True)          # information only (access is by State + Region)
    plant_name: Mapped[str | None] = mapped_column(String(120), nullable=True)   # e.g. "Plant 2 Boiler House"
    country: Mapped[str] = mapped_column(String(64), default="India", server_default="India")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # v1.1.5: maintenance mode (alarms muted, shown as "in maintenance") and the position on the plant map (both optional)
    maintenance_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    maintenance_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    maintenance_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    registers: Mapped[list["DeviceRegister"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="DeviceRegister.id"
    )


class DeviceRegister(TenantBase):
    """One mapped data point. Protocol decides which address fields are used:
    Modbus TCP -> register_type + register_address, OPC-UA -> node_id, MQTT -> topic (+ json_field)."""

    __tablename__ = "device_registers"
    __table_args__ = (
        # several single-bit sensors may share one register (different bit_index): NULL counts as "no bit"
        Index("uq_register_type_address", "device_id", "register_type", "register_address", text("COALESCE(bit_index, -1)"), unique=True),
        UniqueConstraint("device_id", "node_id", name="uq_register_node"),
        UniqueConstraint("device_id", "topic", "json_field", name="uq_register_topic"),
        UniqueConstraint("device_id", "metric_name", name="uq_register_metric"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    sensor_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    metric_name: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    data_type: Mapped[str] = mapped_column(String(16), default="float32")
    scale_factor: Mapped[float] = mapped_column(Float, default=1.0, server_default="1")
    register_type: Mapped[str | None] = mapped_column(String(12), nullable=True)  # coil|discrete|input|holding
    register_address: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bit_index: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-15: one bit of a holding / input register
    node_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    json_field: Mapped[str | None] = mapped_column(String(64), nullable=True)
    low_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    high_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    alarm_severity: Mapped[str] = mapped_column(String(12), default="major")
    is_main: Mapped[bool] = mapped_column(default=False, server_default="false")   # shown big on the dashboard tile (max 2 per device)
    # v1.1.5: write to the plant. A sensor can be written only when the Super admin / admin ticked "writable" and gave the limits.
    writable: Mapped[bool] = mapped_column(default=False, server_default="false")
    write_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    write_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    stuck_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)      # own "stuck sensor" time (NULL = the company setting, 0 = never)


class Telemetry(TenantBase):
    """TimescaleDB hypertable (partitioned on `timestamp`, UTC)."""

    __tablename__ = "telemetry"
    __table_args__ = (
        UniqueConstraint("device_id", "timestamp", "metric_name", name="uq_device_telemetry"),
        Index("ix_telemetry_dev_metric_ts", "device_id", "metric_name", "timestamp"),
    )

    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    metric_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[float] = mapped_column(Float)


class Alarm(TenantBase):
    __tablename__ = "alarms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    metric_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    severity: Mapped[str] = mapped_column(String(12))
    message: Mapped[str] = mapped_column(Text)
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Notification(TenantBase):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)  # references master users.id
    alarm_id: Mapped[int] = mapped_column(ForeignKey("alarms.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(16), default="in_app")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FcmToken(TenantBase):
    __tablename__ = "fcm_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    token: Mapped[str] = mapped_column(String(512), unique=True)
    device_info: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(TenantBase):
    """Tamper-evident hash chain: row_hash = HMAC-SHA256(key, fields + previous_hash)."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    company_name: Mapped[str] = mapped_column(String(63))
    action: Mapped[str] = mapped_column(String(64))
    resource: Mapped[str] = mapped_column(String(255))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_hash: Mapped[str] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64))


class AlertRule(TenantBase):
    """v1.1 alert rule: "if <metric> <op> <threshold> for <for_seconds>, raise an alarm". Works next to the sensor's own low / high limits."""

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    metric_name: Mapped[str] = mapped_column(String(64))
    op: Mapped[str] = mapped_column(String(2))            # > | < | >= | <=
    threshold: Mapped[float] = mapped_column(Float)
    for_seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    severity: Mapped[str] = mapped_column(String(12), default="major")
    message: Mapped[str | None] = mapped_column(String(200), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # v1.1.5 rule kinds (one small file each in app/services/alertrules.py): level | rate | deadband | no_data
    kind: Mapped[str] = mapped_column(String(10), default="level", server_default="level")
    deadband: Mapped[float] = mapped_column(Float, default=0, server_default="0")          # level: clears only after value moves this far back
    window_seconds: Mapped[int] = mapped_column(Integer, default=60, server_default="60")  # rate: change measured over this time; no_data: silence limit
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AlarmComment(TenantBase):
    """A note a person adds to an alarm ("inlet valve closed by hand")."""

    __tablename__ = "alarm_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alarm_id: Mapped[int] = mapped_column(ForeignKey("alarms.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(Integer)
    username: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AlarmShelf(TenantBase):
    """Shelving: one sensor's alarm is put aside until a time, with a reason. It is not notified while shelved (and listed as Shelved)."""

    __tablename__ = "alarm_shelves"
    __table_args__ = (UniqueConstraint("device_id", "metric_name", name="uq_shelf_device_metric"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    metric_name: Mapped[str] = mapped_column(String(64))
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(200))
    shelved_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Command(TenantBase):
    """One write to the plant. States: sent, delivered, written, confirmed, failed, timed_out, expired. Never replayed after it expired."""

    __tablename__ = "commands"
    __table_args__ = (Index("ix_commands_register_state", "register_id", "state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    device_id: Mapped[int] = mapped_column(Integer, index=True)          # no foreign key: the history stays if a device is deleted
    device_name: Mapped[str] = mapped_column(String(120))
    register_id: Mapped[int] = mapped_column(Integer)
    metric_name: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    value: Mapped[float] = mapped_column(Float)                           # what the person asked for (engineering units)
    raw_value: Mapped[float] = mapped_column(Float)                       # what is written to the PLC (value / scale factor)
    previous_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    readback: Mapped[float | None] = mapped_column(Float, nullable=True)  # what the PLC reported after the write (engineering units)
    state: Mapped[str] = mapped_column(String(12), default="sent", index=True)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    requested_by_id: Mapped[int] = mapped_column(Integer)
    requested_by: Mapped[str] = mapped_column(String(64))
    gateway_id: Mapped[str] = mapped_column(String(32), default="main")
    state_name: Mapped[str] = mapped_column(String(64), default="")      # device State / Region at the time (for the live filter)
    region: Mapped[str] = mapped_column(String(10), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)      # why the person made this change (shown in the log)
    pulse: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")   # fault-reset style: 1, then back to 0 after 1 s
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)  # second person, when company setting write_approval is on


class DowntimeEvent(TenantBase):
    """A stop of a machine. Opened automatically (run bit 0, or device offline); the operator picks the reason."""

    __tablename__ = "downtime_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(10), default="runbit")           # runbit | offline | manual
    reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    set_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Batch(TenantBase):
    """A production batch on one device. The summary (min / avg / max per sensor, alarms, writes) is stored as JSON when it ends."""

    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    batch_no: Mapped[str] = mapped_column(String(64))
    product: Mapped[str | None] = mapped_column(String(120), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_by: Mapped[str] = mapped_column(String(64))
    ended_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class Screen(TenantBase):
    """Saved screens kept as data: kind 'process' = animation rules of a device (device_id NULL = company default),
    kind 'hmi' = a drag-and-drop HMI screen. config is JSON."""

    __tablename__ = "screens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=True)
    name: Mapped[str] = mapped_column(String(80))
    config: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_screens_kind_device", "kind", "device_id"),)


class DetectedTag(TenantBase):
    """V2.0: a data point a customer gateway sent that nobody has chosen yet. It is only LISTED (last value as a sample): no history is
    stored and nothing shows in the app until a Super admin / Regional admin ticks it. Dismissed ones stay hidden."""

    __tablename__ = "detected_tags"
    __table_args__ = (UniqueConstraint("plant_id", "ext_device", "tag", name="uq_detected_tag"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plant_id: Mapped[str] = mapped_column(String(16), index=True)
    ext_device: Mapped[str] = mapped_column(String(120))      # the device / edge node name the gateway used
    tag: Mapped[str] = mapped_column(String(64))
    sample: Mapped[str | None] = mapped_column(String(40), nullable=True)
    kind: Mapped[str] = mapped_column(String(8), default="number", server_default="number")   # number | bool
    count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    dismissed: Mapped[bool] = mapped_column(default=False, server_default="false")
