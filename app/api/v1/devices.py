from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, ValidationError, model_validator
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.constants import MODBUS_ALL
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.timeutil import as_utc, utcnow
from app.db.session import get_master_db
from app.deps import ADMIN_ROLES, client_ip, get_company_db, get_current_user, get_scoped_device, in_scope, require_roles, scope_devices
from app.models.master import Gateway, User
from app.models.tenant import Alarm, Device, DeviceRegister, Telemetry
from app.schemas.device import (
    DeviceCreate, DeviceOut, DeviceUpdate, PlacePatch, PlantPcIn, RegisterIn, check_register_for_protocol, RegisterOut, TelemetryBatchIn, TelemetryIngestOut,
)
from app.services import audit, lastseen, live, perms, sync
from app.services import gateway as gw
from app.services import status as status_svc
from app.services.export import build_dashboard_export, build_export, dashboard_rows, iter_file
from app.services.ingest import ingest_readings

router = APIRouter(prefix="/devices", tags=["devices"])
log = get_logger("devices")

RANGES = {"1h": timedelta(hours=1), "8h": timedelta(hours=8), "24h": timedelta(hours=24), "1d": timedelta(days=1), "7d": timedelta(days=7),
          "30d": timedelta(days=30), "90d": timedelta(days=90), "1yr": timedelta(days=365), "5yr": timedelta(days=1825)}


def _merge_seen(company: str, last: dict) -> dict:
    """Last time each device answered: the newer of 'last stored reading' and 'last successful poll'
    (unchanged readings are not stored, so the stored time alone would make a steady device look offline)."""
    out = dict(last)
    for dev_id, ts in lastseen.get_all(company).items():
        if dev_id not in out or ts > out[dev_id]:
            out[dev_id] = ts
    return out


def _statuses(master: Session, company: str, devices: list[Device], last: dict) -> tuple[dict, dict]:
    """(device id -> online | offline | plant_offline | waiting, gateway id -> GwInfo). One rule for every screen."""
    infos = gw.company_gateways(company)
    rows = {r.gateway_id: r for r in master.scalars(select(Gateway).where(Gateway.company_name == company))}
    return status_svc.device_states(company, devices, last, infos, rows), infos


def _device_out(d: Device, last_seen=None, active=0, state: str | None = None, infos: dict | None = None) -> DeviceOut:
    out = DeviceOut.model_validate(d)
    out.last_seen, out.active_alarms = last_seen, active
    out.in_maintenance = bool(d.maintenance_until and d.maintenance_until > utcnow())
    if state:
        out.status, out.online = state, state == "online"
    if infos is not None and d.protocol in gw.GATEWAY_PROTOCOLS:
        gid = gw.responsible_gid(infos, d.state, d.region, d.plant_pc)
        out.plant_pc = gid
        out.plant_pc_name = infos[gid].name if gid in infos else gid
    return out


def _check_plant_pc(plant_pc: str | None, protocol: str, company: str, state: str, region: str) -> str | None:
    """A device may only name a plant PC of its own State + Region. MQTT devices are not read by a plant PC."""
    if not plant_pc or protocol not in gw.GATEWAY_PROTOCOLS:
        return None
    info = gw.get_info(company, plant_pc)
    if not info or not info.site_state or info.site_state != state or info.site_region != region:
        raise AppError(422, "PLANT_PC_INVALID", "Choose a plant PC of this device's State and Region.")
    return plant_pc


def _constraint_error(exc: IntegrityError) -> AppError:
    msg = str(exc.orig)
    if "uq_register_type_address" in msg:
        return AppError(409, "REGISTER_ADDRESS_DUPLICATE", "That register type and address is already mapped on this device. Use a different address.")
    if "uq_register_node" in msg:
        return AppError(409, "REGISTER_NODE_DUPLICATE", "That OPC-UA Node ID is already mapped on this device. Use a different node.")
    if "uq_register_topic" in msg:
        return AppError(409, "REGISTER_TOPIC_DUPLICATE", "That MQTT topic and JSON field is already mapped on this device. Use a different topic or field.")
    if "uq_register_metric" in msg:
        return AppError(409, "REGISTER_METRIC_DUPLICATE", "That metric name is already mapped on this device. Use a different metric name.")
    if "uq_device_company_name_ip" in msg:
        return AppError(409, "DEVICE_DUPLICATE", "A device with this name and IP address already exists for your company. Change the device name or IP address.")
    return AppError(409, "CONFLICT", "This change conflicts with existing data.")


@router.get("", response_model=list[DeviceOut])
def list_devices(user: User = Depends(get_current_user), db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    devices = list(db.scalars(scope_devices(select(Device).options(selectinload(Device.registers))
                                            .where(Device.protocol != "Gateway").order_by(Device.device_name), user)))
    # "last answered" comes from the fast live memory; the database (a year of readings) is asked only for devices that memory does not know
    # (right after a restart). Before, EVERY call scanned a year of readings of the whole company = slow on a big plant.
    live_seen = lastseen.get_all(user.company_name)
    missing = [d.id for d in devices if d.id not in live_seen]
    stored = {}
    if missing:
        stored = dict(db.execute(select(Telemetry.device_id, func.max(Telemetry.timestamp))
                                 .where(Telemetry.timestamp > utcnow() - timedelta(days=365), Telemetry.device_id.in_(missing))
                                 .group_by(Telemetry.device_id)).all())
    last = _merge_seen(user.company_name, stored)
    act = dict(db.execute(select(Alarm.device_id, func.count()).where(Alarm.cleared_at.is_(None))
                          .group_by(Alarm.device_id)).all())
    states, infos = _statuses(master, user.company_name, devices, last)
    return [_device_out(d, last.get(d.id), act.get(d.id, 0), states.get(d.id), infos) for d in devices]


@router.get("/status")
def live_status(user: User = Depends(get_current_user), db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    """Small and fast: the live state of every device the caller may see. The app asks for it every second."""
    devices = list(db.scalars(scope_devices(select(Device).where(Device.protocol != "Gateway"), user)))
    last = lastseen.get_all(user.company_name)
    states, _ = _statuses(master, user.company_name, devices, last)
    return [{"id": d.id, "status": states.get(d.id, "offline"), "online": states.get(d.id) == "online",
             "last_seen": last[d.id].isoformat() if d.id in last else None} for d in devices]


SPARK_POINTS = 24


def _main_registers(d: Device) -> list[DeviceRegister]:
    """The (up to) two readings a dashboard tile shows big: the ones marked as main, else the first two sensors."""
    marked = [r for r in d.registers if r.is_main]
    return (marked or sorted(d.registers, key=lambda r: r.id))[:2]


def _resample(changes: list[tuple], prior: float | None, since, now, n: int) -> list[float | None]:
    """Value in force at n evenly spaced TIMES of the window (only changes are stored, so each value is held until the next change).
    A quiet sensor therefore draws a flat line, and the width of the line is time, not the number of stored rows."""
    out: list[float | None] = []
    i, cur = 0, prior
    span = (now - since).total_seconds()
    for k in range(n):
        t = since + timedelta(seconds=span * (k + 1) / n)
        while i < len(changes) and changes[i][0] <= t:
            cur = changes[i][1]
            i += 1
        out.append(cur)
    return out


@router.get("/values")
def tile_values(user: User = Depends(perms.require("monitor", 1)), db: Session = Depends(get_company_db)):
    """Only the newest value of the (up to) two main sensors of every tile: {device id: {register id: value}}. Tiny and quick on purpose,
    so the app can ask for it every second or two on a mobile network (the small lines come from /sparks, much less often)."""
    devices = list(db.scalars(scope_devices(select(Device).options(selectinload(Device.registers)).where(Device.protocol != "Gateway"), user)))
    pairs = sorted({(d.id, r.metric_name, r.id) for d in devices for r in _main_registers(d)})
    if not pairs:
        return {}
    q = text("""SELECT w.d, w.m, l.value FROM unnest(CAST(:ds AS int[]), CAST(:ms AS text[])) AS w(d, m)
                CROSS JOIN LATERAL (SELECT t.value FROM telemetry t WHERE t.device_id = w.d AND t.metric_name = w.m
                                    AND t."timestamp" > :back ORDER BY t."timestamp" DESC LIMIT 1) l""")
    got = {(r[0], r[1]): float(r[2]) for r in db.execute(q, {"ds": [p[0] for p in pairs], "ms": [p[1] for p in pairs],
                                                             "back": utcnow() - timedelta(days=7)}).all()}
    out: dict[str, dict[str, float]] = {}
    for dev, metric, rid in pairs:
        if (dev, metric) in got:
            out.setdefault(str(dev), {})[str(rid)] = got[(dev, metric)]
    return out


@router.get("/sparks")
def sparks(device_id: int | None = None, user: User = Depends(perms.require("monitor", 1)), db: Session = Depends(get_company_db)):
    """For the dashboard tiles: per device its two main readings with the latest value and a small TIME-based line of the last minutes
    (company setting 'spark_minutes'). Small and cheap on purpose: it only reads the main sensors, never the whole history."""
    from app.services import csettings
    stmt = scope_devices(select(Device).options(selectinload(Device.registers)).where(Device.protocol != "Gateway"), user)
    if device_id is not None:      # the plant view: ALL sensors of one device (up to 8), not only the two main ones
        stmt = stmt.where(Device.id == device_id)
    devices = list(db.scalars(stmt))
    shown = (lambda d: sorted(d.registers, key=lambda r: (not r.is_main, r.id))[:8]) if device_id is not None else _main_registers
    wanted = {(d.id, r.metric_name) for d in devices for r in shown(d)}
    ids = sorted({i for i, _ in wanted})
    if not ids:
        return {}
    now = utcnow()
    since = now - timedelta(minutes=max(1, min(csettings.get(user.company_name, "spark_minutes"), 240)))
    # Only the sensors that are shown (2 per tile) are read, and the newest value of each is looked up directly in the index.
    # (Before: every sensor of every device was read and filtered afterwards = 4x the data and slow on a big plant.)
    pairs = sorted(wanted)
    dev_arr, met_arr = [p[0] for p in pairs], [p[1] for p in pairs]
    rows = db.execute(text("""SELECT t.device_id, t.metric_name, t."timestamp", t.value
                              FROM unnest(CAST(:ds AS int[]), CAST(:ms AS text[])) AS w(d, m)
                              JOIN telemetry t ON t.device_id = w.d AND t.metric_name = w.m
                              WHERE t."timestamp" > :since ORDER BY t."timestamp" """),
                      {"ds": dev_arr, "ms": met_arr, "since": since}).all()
    series: dict[tuple, list[tuple]] = {}
    for dev, metric, ts, val in rows:
        series.setdefault((dev, metric), []).append((ts, float(val)))

    def newest(before=None, back_days=7):
        extra = 'AND t."timestamp" <= :before' if before is not None else ""
        par = {"ds": dev_arr, "ms": met_arr, "back": now - timedelta(days=back_days)}
        if before is not None:
            par["before"] = before
        q = text(f"""SELECT w.d, w.m, l.value FROM unnest(CAST(:ds AS int[]), CAST(:ms AS text[])) AS w(d, m)
                     CROSS JOIN LATERAL (SELECT t.value FROM telemetry t WHERE t.device_id = w.d AND t.metric_name = w.m
                                         AND t."timestamp" > :back {extra} ORDER BY t."timestamp" DESC LIMIT 1) l""")
        return {(r[0], r[1]): float(r[2]) for r in db.execute(q, par).all()}
    prior = newest(before=since)
    latest = newest()
    out: dict[str, list] = {}
    for d in devices:
        items = []
        for r in shown(d):
            k = (d.id, r.metric_name)
            pts = _resample(series.get(k, []), prior.get(k), since, now, SPARK_POINTS)
            items.append({"register_id": r.id, "metric": r.metric_name, "unit": r.unit, "value": latest.get(k),
                          "points": [p for p in pts if p is not None], "low": r.low_limit, "high": r.high_limit,
                          "main": bool(r.is_main), "window_minutes": round((now - since).total_seconds() / 60)})
        out[str(d.id)] = items
    return out


class MainSensorsIn(BaseModel):
    register_ids: list[int] = Field(default_factory=list, max_length=2)


@router.put("/{device_id}/main-sensors", response_model=DeviceOut)
def set_main_sensors(device_id: int, body: MainSensorsIn, request: Request, user: User = Depends(perms.require("monitor", 2)),
                     db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    """Pick the (up to two) readings shown big on this device's dashboard tile. A display choice only, so no approval is needed."""
    d = get_scoped_device(db, user, device_id)
    mine = {r.id for r in d.registers}
    if len(set(body.register_ids)) != len(body.register_ids) or any(i not in mine for i in body.register_ids):
        raise AppError(422, "REGISTER_INVALID", "Choose up to two different sensors of this device.")
    for r in d.registers:
        r.is_main = r.id in body.register_ids
    db.commit()
    audit.append(db, user.id, user.company_name, "device.main_sensors", f"device:{d.id}", client_ip(request))
    return _device_out(d)


@router.get("/export")
def export_dashboard(request: Request, format: str = Query(pattern="^(csv|xlsx|pdf)$"),
                     user: User = Depends(perms.require("reports", 1)), db: Session = Depends(get_company_db)):
    """Everything the dashboard shows (all devices in the user's scope with latest values) as CSV / Excel / PDF."""
    devices = list(db.scalars(scope_devices(select(Device).options(selectinload(Device.registers))
                                            .where(Device.protocol != "Gateway").order_by(Device.device_name), user)))
    # "last answered" comes from the fast live memory; the database (a year of readings) is asked only for devices that memory does not know
    # (right after a restart). Before, EVERY call scanned a year of readings of the whole company = slow on a big plant.
    live_seen = lastseen.get_all(user.company_name)
    missing = [d.id for d in devices if d.id not in live_seen]
    stored = {}
    if missing:
        stored = dict(db.execute(select(Telemetry.device_id, func.max(Telemetry.timestamp))
                                 .where(Telemetry.timestamp > utcnow() - timedelta(days=365), Telemetry.device_id.in_(missing))
                                 .group_by(Telemetry.device_id)).all())
    last = _merge_seen(user.company_name, stored)
    act = dict(db.execute(select(Alarm.device_id, func.count()).where(Alarm.cleared_at.is_(None))
                          .group_by(Alarm.device_id)).all())
    tmp, filename, media = build_dashboard_export(dashboard_rows(db, devices, act, last), format, user.company_name)
    log.info("dashboard_export", format=format)
    audit.append(db, user.id, user.company_name, "dashboard.export", f"dashboard:{format}", client_ip(request))
    return StreamingResponse(iter_file(tmp), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _validate_device_create(body: DeviceCreate, company: str) -> None:
    for r in body.registers:
        problem = check_register_for_protocol(body.protocol, r, company)
        if problem:
            raise AppError(422, "REGISTER_PROTOCOL_MISMATCH", problem)
    keys = [(r.register_type, r.register_address, r.bit_index, r.node_id, r.topic, r.json_field) for r in body.registers]
    names = [r.metric_name for r in body.registers]
    if len(set(keys)) != len(keys):
        raise AppError(409, "REGISTER_ADDRESS_DUPLICATE", "Two registers use the same address, node or topic. Each must be unique per device.")
    if len(set(names)) != len(names):
        raise AppError(409, "REGISTER_METRIC_DUPLICATE", "Two registers use the same metric name. Each metric name must be unique per device.")


def _clean(v: str | None) -> str | None:
    v = " ".join((v or "").split())
    return v or None


def apply_device_create(db: Session, body: DeviceCreate, user: User, request: Request) -> Device:
    """Creates the device. Called directly for administrators and when an administrator approves an operator's request."""
    _validate_device_create(body, user.company_name)
    state, region = (body.state or user.state), (body.region or user.region)
    plant_pc = _check_plant_pc(body.plant_pc, body.protocol, user.company_name, state, region)
    device = Device(plant_pc=plant_pc, device_name=body.device_name.strip(), protocol=body.protocol, ip_address=body.ip_address,
                    port=body.port, company_name=user.company_name, state=state, region=region, country=body.country or user.country or "India",
                    city=_clean(body.city), plant_name=_clean(body.plant_name),
                    **body.model_dump(include={"unit_id", "serial_baud", "serial_parity", "serial_databits", "serial_stopbits",
                                               "byte_order", "word_order", "address_base", "timeout_ms", "retries"}))
    device.registers = [DeviceRegister(**r.model_dump()) for r in body.registers]
    db.add(device)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _constraint_error(exc)
    sync.mirror(db, "devices", [device])
    sync.mirror(db, "device_registers", device.registers)
    audit.append(db, user.id, user.company_name, "device.create", f"device:{device.id}", client_ip(request))
    return device


@router.post("", response_model=DeviceOut, status_code=201)
def create_device(body: DeviceCreate, request: Request, user: User = Depends(perms.require("config", 2)), db: Session = Depends(get_company_db)):
    if not in_scope(user, body.state or user.state, body.region or user.region):
        raise AppError(403, "OUTSIDE_YOUR_REGION", "A Regional admin can add devices only in their own State and Region.")
    return _device_out(apply_device_create(db, body, user, request))


@router.get("/{device_id}", response_model=DeviceOut)
def get_device(device_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    d = get_scoped_device(db, user, device_id)
    last = db.scalar(select(func.max(Telemetry.timestamp)).where(Telemetry.device_id == d.id, Telemetry.timestamp > utcnow() - timedelta(days=1)))
    seen = lastseen.get(user.company_name, d.id)   # same rule as the device list (the old detail page used the stored time only)
    last = seen if seen and (last is None or seen > last) else last
    active = db.scalar(select(func.count()).select_from(Alarm).where(Alarm.device_id == d.id, Alarm.cleared_at.is_(None))) or 0
    states, infos = _statuses(master, user.company_name, [d], {d.id: last} if last else {})
    return _device_out(d, last, active, states.get(d.id), infos)


_DETAIL_FIELDS = ("device_name", "protocol", "ip_address", "port", "unit_id", "serial_baud", "serial_parity", "serial_databits",
                  "serial_stopbits", "byte_order", "word_order", "address_base", "timeout_ms", "retries", "city", "plant_name", "plant_pc")
_DETAIL_LABEL = {"device_name": "name", "ip_address": "address", "unit_id": "unit id", "serial_baud": "baud", "serial_parity": "parity",
                 "serial_databits": "data bits", "serial_stopbits": "stop bits", "byte_order": "byte order", "word_order": "word order",
                 "address_base": "address base", "timeout_ms": "timeout", "plant_name": "plant name", "plant_pc": "plant PC"}


def device_changes(d: Device, body: DeviceUpdate) -> dict:
    """Only the fields that really change, as {field: new value}. Checks them with the same rules as a new device."""
    cur = {k: getattr(d, k) for k in _DETAIL_FIELDS}
    new = dict(cur)
    new.update(body.model_dump(exclude_unset=True))
    new["city"], new["plant_name"] = _clean(new["city"]), _clean(new["plant_name"])
    try:
        checked = DeviceCreate(**new, registers=[]).model_dump(include=set(_DETAIL_FIELDS))
    except ValidationError as exc:
        raise AppError(422, "INVALID_DEVICE", str(exc.errors()[0]["msg"]).removeprefix("Value error, "))
    for r in d.registers:   # the sensors that already exist must still fit the (new) protocol
        problem = check_register_for_protocol(checked["protocol"], RegisterIn.model_validate(r, from_attributes=True), d.company_name)
        if problem:
            raise AppError(422, "REGISTER_PROTOCOL_MISMATCH", problem)
    checked["plant_pc"] = _check_plant_pc(checked["plant_pc"], checked["protocol"], d.company_name, d.state, d.region)
    diff = {k: v for k, v in checked.items() if v != cur[k]}
    if not diff:
        raise AppError(422, "NOTHING_CHANGED", "Nothing was changed.")
    return diff


def apply_device_update(db: Session, d: Device, diff: dict, user: User, request: Request) -> Device:
    """Changes the device. Called directly for administrators and when an administrator approves an operator's request."""
    for k, v in diff.items():
        setattr(d, k, v)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _constraint_error(exc)
    sync.mirror(db, "devices", [d])
    audit.append(db, user.id, user.company_name, "device.update", f"device:{d.id}:{','.join(diff)}", client_ip(request))
    return d


@router.put("/{device_id}", response_model=DeviceOut)
def update_device(device_id: int, body: DeviceUpdate, request: Request, user: User = Depends(perms.require("config", 2)),
                  db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    """Modify the details of a device. An operator's change is only a request: the Regional admin of that State + Region decides."""
    d = get_scoped_device(db, user, device_id)
    diff = device_changes(d, body)
    apply_device_update(db, d, diff, user, request)
    states, infos = _statuses(master, user.company_name, [d], {})
    return _device_out(d, None, 0, states.get(d.id), infos)


@router.put("/{device_id}/plant-pc", response_model=DeviceOut)
def set_plant_pc(device_id: int, body: PlantPcIn, request: Request, user: User = Depends(require_roles(*ADMIN_ROLES)),
                 db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    """Move a device to another plant PC of its State + Region (empty = the first plant PC of the region)."""
    d = get_scoped_device(db, user, device_id)
    d.plant_pc = _check_plant_pc(body.plant_pc, d.protocol, user.company_name, d.state, d.region)
    db.commit()
    sync.mirror(db, "devices", [d])
    audit.append(db, user.id, user.company_name, "device.plant_pc", f"device:{d.id}:{d.plant_pc or 'default'}", client_ip(request))
    states, infos = _statuses(master, user.company_name, [d], {})
    return _device_out(d, None, 0, states.get(d.id), infos)


@router.put("/{device_id}/place", response_model=DeviceOut)
def set_place(device_id: int, body: PlacePatch, request: Request, user: User = Depends(require_roles(*ADMIN_ROLES)),
              db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    """Set or clear the City and Plant name of a device (information only)."""
    d = get_scoped_device(db, user, device_id)
    d.city, d.plant_name = _clean(body.city), _clean(body.plant_name)
    db.commit()
    sync.mirror(db, "devices", [d])
    audit.append(db, user.id, user.company_name, "device.place", f"device:{d.id}", client_ip(request))
    states, infos = _statuses(master, user.company_name, [d], {})
    return _device_out(d, None, 0, states.get(d.id), infos)


@router.delete("/{device_id}", status_code=204)
def delete_device(device_id: int, request: Request, user: User = Depends(require_roles(*ADMIN_ROLES)), db: Session = Depends(get_company_db)):
    d = get_scoped_device(db, user, device_id)
    db.delete(d)
    db.commit()
    sync.mirror(db, "devices", [], op="delete", rows=[{"id": device_id}])
    audit.append(db, user.id, user.company_name, "device.delete", f"device:{device_id}", client_ip(request))


def apply_register_create(db: Session, d: Device, body: RegisterIn, user: User, request: Request) -> DeviceRegister:
    problem = check_register_for_protocol(d.protocol, body, d.company_name)
    if problem:
        raise AppError(422, "REGISTER_PROTOCOL_MISMATCH", problem)
    reg = DeviceRegister(device_id=d.id, **body.model_dump())
    db.add(reg)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _constraint_error(exc)
    sync.mirror(db, "device_registers", [reg])
    audit.append(db, user.id, user.company_name, "register.create", f"device:{d.id}/register:{reg.id}", client_ip(request))
    return reg


def _find_register(db: Session, d: Device, register_id: int) -> DeviceRegister:
    reg = db.scalar(select(DeviceRegister).where(DeviceRegister.id == register_id, DeviceRegister.device_id == d.id))
    if not reg:
        raise AppError(404, "REGISTER_NOT_FOUND", "Register not found on this device.")
    return reg


def apply_register_update(db: Session, master: Session, d: Device, register_id: int, body: RegisterIn, user: User, request: Request) -> DeviceRegister:
    reg = _find_register(db, d, register_id)
    if body.metric_name != reg.metric_name:
        raise AppError(422, "METRIC_RENAME_NOT_ALLOWED", "The sensor name cannot be changed because its history and alarms are stored under it. Delete and re-add the sensor to rename it.")
    problem = check_register_for_protocol(d.protocol, body, d.company_name)
    if problem:
        raise AppError(422, "REGISTER_PROTOCOL_MISMATCH", problem)
    for key, value in body.model_dump().items():
        setattr(reg, key, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _constraint_error(exc)
    db.refresh(reg)
    sync.mirror(db, "device_registers", [reg])
    # apply the new limits / priority to this sensor's open alarm right away (re-check its newest reading)
    newest = db.execute(select(Telemetry.value, Telemetry.timestamp).where(
        Telemetry.device_id == d.id, Telemetry.metric_name == reg.metric_name).order_by(Telemetry.timestamp.desc()).limit(1)).first()
    if newest:
        from app.services import alarms as alarm_svc
        alarm_svc.evaluate(db, master, d, {reg.metric_name: (newest[0], newest[1])})
    audit.append(db, user.id, user.company_name, "register.update", f"device:{d.id}/register:{reg.id}", client_ip(request))
    return reg


def apply_register_delete(db: Session, d: Device, register_id: int, user: User, request: Request) -> None:
    reg = _find_register(db, d, register_id)
    db.delete(reg)
    db.commit()
    sync.mirror(db, "device_registers", [], op="delete", rows=[{"id": register_id}])
    audit.append(db, user.id, user.company_name, "register.delete", f"device:{d.id}/register:{register_id}", client_ip(request))


_LABELS = {"sensor_type": "type", "unit": "unit", "data_type": "data type", "scale_factor": "scaling", "register_type": "register",
           "register_address": "address", "bit_index": "bit", "low_limit": "low limit", "high_limit": "high limit", "alarm_severity": "priority",
           "node_id": "node", "topic": "topic", "json_field": "json field"}


@router.post("/{device_id}/registers", response_model=RegisterOut, status_code=201)
def add_register(device_id: int, body: RegisterIn, request: Request, user: User = Depends(perms.require("config", 2)), db: Session = Depends(get_company_db)):
    d = get_scoped_device(db, user, device_id)
    return apply_register_create(db, d, body, user, request)


@router.put("/{device_id}/registers/{register_id}", response_model=RegisterOut)
def update_register(device_id: int, register_id: int, body: RegisterIn, request: Request, user: User = Depends(perms.require("config", 2)),
                    db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    d = get_scoped_device(db, user, device_id)
    return apply_register_update(db, master, d, register_id, body, user, request)


@router.delete("/{device_id}/registers/{register_id}", status_code=204)
def delete_register(device_id: int, register_id: int, request: Request, user: User = Depends(perms.require("config", 2)), db: Session = Depends(get_company_db)):
    d = get_scoped_device(db, user, device_id)
    apply_register_delete(db, d, register_id, user, request)


@router.post("/{device_id}/telemetry", response_model=TelemetryIngestOut)
def ingest(device_id: int, body: TelemetryBatchIn, user: User = Depends(perms.require("config", 2)),
           db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    d = get_scoped_device(db, user, device_id)
    result = ingest_readings(db, master, d, [(r.metric_name, r.value, r.timestamp) for r in body.readings])
    log.info("telemetry_ingested", device_id=d.id, count=result["received"])  # values are never logged
    return result


@router.post("/{device_id}/refresh")
def refresh(device_id: int, user: User = Depends(get_current_user),
            db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    """Read the PLC right now instead of waiting for the next poll, then return the newest values."""
    from app.workers.poller import poll_now

    d = get_scoped_device(db, user, device_id)
    poll_now(db, master, d, d.company_name)
    return latest(device_id, user, db)


@router.get("/{device_id}/metrics", response_model=list[str])
def metrics(device_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_company_db)):
    d = get_scoped_device(db, user, device_id)
    return [r.metric_name for r in d.registers]


@router.get("/{device_id}/latest")
def latest(device_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_company_db)):
    d = get_scoped_device(db, user, device_id)
    since = utcnow() - timedelta(days=365)
    rows = db.execute(text("""
        SELECT DISTINCT ON (metric_name) metric_name, value, "timestamp"
        FROM telemetry WHERE device_id = :d AND "timestamp" > :since
        ORDER BY metric_name, "timestamp" DESC"""), {"d": d.id, "since": since}).all()
    seen = lastseen.get(user.company_name, d.id)  # the value is still current at the last poll even if it was stored earlier
    return [{"metric_name": r[0], "value": r[1], "timestamp": (seen if seen and seen > r[2] else r[2]).isoformat()} for r in rows]


def trend_points(db: Session, company: str, d: Device, metric: str, range: str) -> dict:
    """Chart points of one metric (average / min / max per time bucket). Used by the app."""
    if range not in RANGES:
        raise AppError(422, "INVALID_RANGE", f"Range must be one of: {', '.join(RANGES)}.")
    span = RANGES[range]
    bucket = max(1, int(span.total_seconds() / 300))  # ~300 points per chart
    start = utcnow() - span
    rows = db.execute(text("""
        SELECT to_timestamp(floor(extract(epoch FROM "timestamp") / :b) * :b) AS t,
               avg(value), min(value), max(value)
        FROM telemetry
        WHERE device_id = :d AND metric_name = :m AND "timestamp" >= :since
        GROUP BY 1 ORDER BY 1"""), {"b": bucket, "d": d.id, "m": metric, "since": start}).all()
    pts = [(r[0], r[1], r[2], r[3]) for r in rows]
    # Only changes are stored, so draw the chart as steps: the value in force at the window start, the previous value
    # held until each change, and the last value held until the device was last seen.
    gap = timedelta(seconds=bucket * 2)
    prior = db.execute(text("""SELECT value FROM telemetry WHERE device_id = :d AND metric_name = :m AND "timestamp" < :since
                               ORDER BY "timestamp" DESC LIMIT 1"""), {"d": d.id, "m": metric, "since": start}).first()
    if prior and (not pts or pts[0][0] - start > gap):
        pts.insert(0, (start, prior[0], prior[0], prior[0]))
    out: list[tuple] = []
    for p in pts:
        if out and p[0] - out[-1][0] > gap:
            out.append((p[0] - timedelta(seconds=1), out[-1][1], out[-1][1], out[-1][1]))
        out.append(p)
    end = lastseen.get(company, d.id)
    if out and end and end - out[-1][0] > gap:
        out.append((end, out[-1][1], out[-1][1], out[-1][1]))
    return {"metric": metric, "range": range, "bucket_seconds": bucket,
            "points": [{"timestamp": t.isoformat(), "avg": a, "min": lo, "max": hi} for t, a, lo, hi in out]}


@router.get("/{device_id}/telemetry")
def history(device_id: int, metric: str = Query(min_length=1, max_length=64), range: str = "1d",
            user: User = Depends(get_current_user), db: Session = Depends(get_company_db)):
    d = get_scoped_device(db, user, device_id)
    return trend_points(db, user.company_name, d, metric, range)


@router.get("/{device_id}/export")
def export(device_id: int, request: Request, format: str = Query(pattern="^(csv|xlsx|pdf)$"),
           date_from: datetime | None = None, date_to: datetime | None = None, metric: str | None = None,
           user: User = Depends(perms.require("reports", 1)), db: Session = Depends(get_company_db)):
    d = get_scoped_device(db, user, device_id)
    end = as_utc(date_to) if date_to else utcnow()
    start = as_utc(date_from) if date_from else end - timedelta(days=7)
    tmp, filename, media = build_export(db, d, format, start, end, metric)
    log.info("export", device_id=d.id, format=format)
    audit.append(db, user.id, user.company_name, "device.export", f"device:{d.id}:{format}", client_ip(request))
    return StreamingResponse(iter_file(tmp), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ------------------------------------------------------------------ v1.1.5: maintenance mode, map position, writable sensors
class MaintenanceIn(BaseModel):
    hours: float = Field(gt=0, le=720)
    note: str | None = Field(default=None, max_length=200)


@router.put("/{device_id}/maintenance", response_model=DeviceOut)
def start_maintenance(device_id: int, body: MaintenanceIn, request: Request, user: User = Depends(perms.require("monitor", 2)),
                      db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    """Maintenance mode: the alarms of this device are recorded but nobody is told, and it shows 'in maintenance'. It ends by itself."""
    from app.services import csettings
    d = get_scoped_device(db, user, device_id)
    limit = csettings.get(user.company_name, "maintenance_max_hours")
    if body.hours > limit:
        raise AppError(422, "MAINTENANCE_TOO_LONG", f"Maintenance mode can last at most {limit} hours (a Super admin can change this in Settings).")
    d.maintenance_until, d.maintenance_note, d.maintenance_by = utcnow() + timedelta(hours=body.hours), (body.note or "").strip() or None, user.username
    db.commit()
    audit.append(db, user.id, user.company_name, "device.maintenance_on", f"device:{d.id}:{body.hours:g}h", client_ip(request))
    live.publish(user.company_name, {"type": "device", "event": "maintenance", "device_id": d.id, "on": True, "state": d.state, "region": d.region})
    states, infos = _statuses(master, user.company_name, [d], {})
    return _device_out(d, None, 0, states.get(d.id), infos)


@router.delete("/{device_id}/maintenance", response_model=DeviceOut)
def end_maintenance(device_id: int, request: Request, user: User = Depends(perms.require("monitor", 2)),
                    db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    d = get_scoped_device(db, user, device_id)
    d.maintenance_until = d.maintenance_note = d.maintenance_by = None
    db.commit()
    audit.append(db, user.id, user.company_name, "device.maintenance_off", f"device:{d.id}", client_ip(request))
    live.publish(user.company_name, {"type": "device", "event": "maintenance", "device_id": d.id, "on": False, "state": d.state, "region": d.region})
    states, infos = _statuses(master, user.company_name, [d], {})
    return _device_out(d, None, 0, states.get(d.id), infos)


class PositionIn(BaseModel):
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def _both(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Give both latitude and longitude, or neither to clear the position")
        return self


@router.put("/{device_id}/position", response_model=DeviceOut)
def set_position(device_id: int, body: PositionIn, request: Request, user: User = Depends(perms.require("config", 2)),
                 db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    """Optional position of a device on the plant map. Information only."""
    d = get_scoped_device(db, user, device_id)
    d.latitude, d.longitude = body.latitude, body.longitude
    db.commit()
    audit.append(db, user.id, user.company_name, "device.position", f"device:{d.id}", client_ip(request))
    states, infos = _statuses(master, user.company_name, [d], {})
    return _device_out(d, None, 0, states.get(d.id), infos)


class WriteConfigIn(BaseModel):
    """Which sensors a person may send a value to, and between which limits. Only Modbus coils, holding registers (or one bit of one) and OPC-UA nodes can be written."""
    writable: bool
    write_min: float | None = None
    write_max: float | None = None
    stuck_minutes: int | None = Field(default=None, ge=0, le=10080)


def check_writable(d: Device, reg: DeviceRegister, body: WriteConfigIn) -> None:
    if not body.writable:
        return
    from app.services import extgw
    if d.protocol not in gw.GATEWAY_PROTOCOLS and not extgw.is_ext_register(reg):
        raise AppError(422, "NOT_WRITABLE_PROTOCOL", f"{d.protocol} devices cannot be written from the app. Modbus and OPC-UA devices, and points of a customer gateway, can.")
    if d.protocol in MODBUS_ALL and reg.register_type not in ("coil", "holding"):
        raise AppError(422, "NOT_WRITABLE_REGISTER", "Only coils and holding registers can be written, not input registers or discrete inputs.")
    is_bit = reg.data_type == "bool" or reg.register_type == "coil"
    if not is_bit:
        if body.write_min is None or body.write_max is None:
            raise AppError(422, "LIMITS_REQUIRED", "Give the lowest and the highest value a person may send. This protects the plant from a typing mistake.")
        if body.write_min > body.write_max:
            raise AppError(422, "LIMITS_BAD", "The lowest value must not be above the highest value.")


@router.put("/{device_id}/registers/{register_id}/write-config", response_model=RegisterOut)
def set_write_config(device_id: int, register_id: int, body: WriteConfigIn, request: Request, user: User = Depends(perms.require("config", 2)),
                     db: Session = Depends(get_company_db)):
    """Administrator: make a sensor writable (with limits) or not. A person with only 'Read' on Configuration cannot do this."""
    if user.role not in ADMIN_ROLES:
        raise AppError(403, "ADMIN_ONLY", "Only an Admin or the Super admin can allow a sensor to be written.")
    d = get_scoped_device(db, user, device_id)
    reg = _find_register(db, d, register_id)
    check_writable(d, reg, body)
    is_bit = reg.data_type == "bool" or reg.register_type == "coil"
    reg.writable = body.writable
    reg.write_min, reg.write_max = (None, None) if (is_bit or not body.writable) else (body.write_min, body.write_max)
    if "stuck_minutes" in body.model_fields_set:
        reg.stuck_minutes = body.stuck_minutes
    db.commit()
    sync.mirror(db, "device_registers", [reg])
    audit.append(db, user.id, user.company_name, "register.write_config",
                 f"device:{d.id}/register:{reg.id}:writable={reg.writable}:{reg.write_min}..{reg.write_max}", client_ip(request))
    return reg
