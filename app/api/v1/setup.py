"""Quick setup: industry templates, CSV tag import and a connection test for devices that were just added."""
import csv
import io
import json
import os

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.v1.devices import _device_out, _merge_seen, _statuses, apply_device_create
from app.core.errors import AppError
from app.core.timeutil import as_utc, utcnow
from app.db.session import get_master_db
from app.deps import get_company_db, in_scope, scope_devices
from app.models.master import User
from app.models.tenant import Device, Telemetry
from app.schemas.device import DeviceCreate, DeviceOut
from app.services import perms

router = APIRouter(prefix="/setup", tags=["setup"])
WORDS = {"int16": 1, "uint16": 1, "bool": 1, "int32": 2, "uint32": 2, "int64": 4, "uint64": 4, "float32": 2, "float64": 4}
MAX_CSV_BYTES, MAX_CSV_ROWS = 1_000_000, 3000
_TEMPLATES: dict | None = None


def templates() -> dict:
    global _TEMPLATES
    if _TEMPLATES is None:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "industry_templates.json"), encoding="utf-8") as f:
            _TEMPLATES = json.load(f)
    return _TEMPLATES


@router.get("/templates")
def list_templates(user: User = Depends(perms.require("config", 1))):
    return [{"key": k, "name": v["name"], "description": v["description"], "sensors": v["sensors"]} for k, v in templates().items()]


class TemplateApply(BaseModel):
    device_name: str = Field(..., min_length=1, max_length=120)
    protocol: str
    ip_address: str
    port: int = 502
    unit_id: int | None = None
    state: str | None = None
    region: str | None = None
    plant_name: str | None = None
    address_start: int = Field(0, ge=0, le=60000)
    leave_out: list[str] = Field(default_factory=list)


def _template_device(t: dict, b: TemplateApply) -> DeviceCreate:
    regs, addr = [], b.address_start
    for s in t["sensors"]:
        if s["metric_name"] in b.leave_out:
            continue
        regs.append({**{k: v for k, v in s.items() if v is not None}, "register_address": addr})
        addr += WORDS.get(s["data_type"], 2)
    return DeviceCreate(device_name=b.device_name, protocol=b.protocol, ip_address=b.ip_address, port=b.port, unit_id=b.unit_id,
                        state=b.state, region=b.region, plant_name=b.plant_name, registers=regs)


@router.post("/templates/{key}/apply", response_model=DeviceOut, status_code=201)
def apply_template(key: str, body: TemplateApply, request: Request, user: User = Depends(perms.require("config", 2)), db: Session = Depends(get_company_db)):
    """Creates a device with the sensors of the template (addresses run on from `address_start`; change any of them afterwards)."""
    t = templates().get(key)
    if not t:
        raise AppError(404, "TEMPLATE_NOT_FOUND", "There is no such template.")
    try:
        dev = _template_device(t, body)
    except ValidationError as exc:
        raise AppError(422, "INVALID_DEVICE", str(exc.errors()[0]["msg"]).removeprefix("Value error, "))
    if not in_scope(user, dev.state or user.state, dev.region or user.region):
        raise AppError(403, "OUTSIDE_YOUR_REGION", "You can add devices only in your own State and Region.")
    return _device_out(apply_device_create(db, dev, user, request))


# ------------------------------------------------------------------ CSV import
COLUMNS = ["device_name", "protocol", "ip_address", "port", "unit_id", "plant_name", "state", "region", "metric_name", "unit", "data_type",
           "register_type", "register_address", "bit_index", "scale_factor", "low_limit", "high_limit", "alarm_severity", "node_id", "sensor_type"]
DEVICE_COLS = ("device_name", "protocol", "ip_address", "port", "unit_id", "plant_name", "state", "region")
NUMERIC = {"port": int, "unit_id": int, "register_address": int, "bit_index": int, "scale_factor": float, "low_limit": float, "high_limit": float}


@router.get("/import/template.csv")
def csv_example(user: User = Depends(perms.require("config", 1))):
    from fastapi.responses import PlainTextResponse
    rows = [COLUMNS,
            ["Pump house 1", "Modbus TCP", "192.168.1.10", "502", "1", "STP-1", "", "", "tank_level", "%", "float32", "holding", "0", "", "1", "15", "90", "major", ""],
            ["Pump house 1", "Modbus TCP", "192.168.1.10", "502", "1", "STP-1", "", "", "pump_run", "state", "uint16", "holding", "2", "", "1", "", "", "advisory", ""]]
    out = io.StringIO()
    csv.writer(out).writerows([r + [""] * (len(COLUMNS) - len(r)) for r in rows])
    return PlainTextResponse(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="stech_tags_example.csv"'})


def parse_csv(raw: bytes) -> tuple[list[DeviceCreate], list[dict]]:
    """CSV -> devices with sensors (one row per sensor; rows of the same device_name + address + port belong together) and a list of problems."""
    if len(raw) > MAX_CSV_BYTES:
        raise AppError(413, "CSV_TOO_LARGE", "The file is too large (1 MB at most).")
    try:
        text_ = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AppError(422, "CSV_ENCODING", "Save the file as CSV UTF-8 and try again.")
    reader = csv.DictReader(io.StringIO(text_))
    missing = [c for c in ("device_name", "protocol", "ip_address", "metric_name") if c not in (reader.fieldnames or [])]
    if missing:
        raise AppError(422, "CSV_COLUMNS", "Missing column(s): " + ", ".join(missing) + ". Download the example file to see the columns.")
    groups: dict[tuple, dict] = {}
    problems: list[dict] = []
    for n, row in enumerate(reader, start=2):
        if n - 1 > MAX_CSV_ROWS:
            raise AppError(413, "CSV_TOO_MANY_ROWS", f"At most {MAX_CSV_ROWS} rows per file.")
        row = {k: (v or "").strip() for k, v in row.items() if k}
        if not any(row.values()):
            continue
        try:
            for k, cast in NUMERIC.items():
                if row.get(k, "") != "":
                    row[k] = cast(row[k])
                else:
                    row.pop(k, None)
        except ValueError:
            problems.append({"row": n, "message": "A number column contains text."})
            continue
        key = (row.get("device_name"), row.get("ip_address"), row.get("port", 502))
        g = groups.setdefault(key, {"row": n, "device": {c: row[c] for c in DEVICE_COLS if row.get(c) not in (None, "")}, "registers": []})
        g["registers"].append({c: row[c] for c in COLUMNS if c not in DEVICE_COLS and row.get(c) not in (None, "")})
    devices = []
    for g in groups.values():
        try:
            devices.append(DeviceCreate(**g["device"], registers=g["registers"]))
        except ValidationError as exc:
            e = exc.errors()[0]
            problems.append({"row": g["row"], "message": f"{g['device'].get('device_name', '?')}: {'.'.join(str(x) for x in e['loc'])} - {str(e['msg']).removeprefix('Value error, ')}"})
    return devices, problems


@router.post("/import")
async def import_csv(request: Request, file: UploadFile = File(...), dry_run: bool = Query(True), user: User = Depends(perms.require("config", 2)),
                     db: Session = Depends(get_company_db)):
    """dry_run=true (default) only checks the file and says what would be created. dry_run=false creates the devices (nothing is created if any row is wrong)."""
    from app.api.v1.devices import _check_plant_pc, _validate_device_create
    devices, problems = parse_csv(await file.read(MAX_CSV_BYTES + 1))
    for d in devices:
        try:
            _validate_device_create(d, user.company_name)
            if not in_scope(user, d.state or user.state, d.region or user.region):
                raise AppError(403, "OUTSIDE_YOUR_REGION", "outside your State / Region")
            _check_plant_pc(d.plant_pc, d.protocol, user.company_name, d.state or user.state, d.region or user.region)
        except AppError as exc:
            problems.append({"row": 0, "message": f"{d.device_name}: {exc.message}"})
    names = {n for (n,) in db.execute(select(Device.device_name))}
    for d in devices:
        if d.device_name.strip() in names:
            problems.append({"row": 0, "message": f"{d.device_name}: a device with this name already exists."})
    summary = [{"device_name": d.device_name, "protocol": d.protocol, "address": f"{d.ip_address}:{d.port}" if d.port else d.ip_address, "sensors": len(d.registers)} for d in devices]
    if problems or dry_run:
        return {"created": False, "ok": not problems, "devices": summary, "problems": problems}
    created = [apply_device_create(db, d, user, request).id for d in devices]
    return {"created": True, "ok": True, "devices": summary, "problems": [], "device_ids": created}


# ------------------------------------------------------------------ connection test
@router.get("/test-connection")
def test_connection(device_ids: str, user: User = Depends(perms.require("config", 1)), db: Session = Depends(get_company_db),
                    master: Session = Depends(get_master_db)):
    """After adding devices: do they answer, and does every sensor deliver a value? Call it every 2 s for about 15 s after the plant PC picked the devices up."""
    try:
        ids = [int(x) for x in device_ids.split(",") if x.strip()][:50]
    except ValueError:
        raise AppError(422, "BAD_IDS", "device_ids must be numbers separated by commas.")
    devices = list(db.scalars(scope_devices(select(Device).where(Device.id.in_(ids)), user))) if ids else []
    last = _merge_seen(user.company_name, dict(db.execute(select(Telemetry.device_id, func.max(Telemetry.timestamp))
                                               .where(Telemetry.device_id.in_([d.id for d in devices])).group_by(Telemetry.device_id)).all())) if devices else {}
    states = _statuses(master, user.company_name, devices, last)[0]
    seen: dict[tuple, tuple] = {}
    if devices:
        for dev, metric, ts, val in db.execute(text("""SELECT DISTINCT ON (device_id, metric_name) device_id, metric_name, "timestamp", value FROM telemetry
                                                   WHERE device_id = ANY(:ids) ORDER BY device_id, metric_name, "timestamp" DESC"""), {"ids": [d.id for d in devices]}).all():
            seen[(dev, metric)] = (ts, float(val))
    advice = {"online": "Answering.", "stale": "Answered a moment ago, now silent. Check the cable / power of the device.",
              "offline": "The plant PC cannot reach this device. Check the address, port, slave id and the cable or network.",
              "plant_offline": "The plant PC is offline. Start it and try again.", "waiting": "The plant PC has not picked this device up yet. Wait a few seconds."}
    now, out = utcnow(), []
    for d in devices:
        sensors = []
        for r in d.registers:
            hit = seen.get((d.id, r.metric_name))
            sensors.append({"metric": r.metric_name, "has_value": hit is not None, "value": hit[1] if hit else None,
                            "seconds_ago": round((now - as_utc(hit[0])).total_seconds()) if hit else None})
        st = states.get(d.id, "offline")
        got = sum(1 for s in sensors if s["has_value"])
        out.append({"device_id": d.id, "device_name": d.device_name, "status": st, "message": advice.get(st, ""), "sensors_total": len(sensors),
                    "sensors_with_value": got, "ok": st == "online" and got == len(sensors), "sensors": sensors})
    return out
