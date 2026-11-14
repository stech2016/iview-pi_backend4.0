"""Write to the plant: send one value (or on / off) to a PLC point and follow it until the PLC confirms it.

The cloud checks everything it can (permission, company write lock, sensor limits, plant PC online, fresh data, no other
command waiting). The plant PC checks again before it writes, and the PLC keeps its own interlocks - this is never a safety system.
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.security import verify_password
from app.core.timeutil import as_utc, utcnow
from app.db.session import get_master_db
from app.deps import client_ip, get_company_db, get_scoped_device, scope_devices
from app.models.master import Gateway, PlantLink, User
from app.models.tenant import Command, Device, DeviceRegister, Telemetry
from app.services import audit, commands, csettings, extgw, gwcontrol, perms, twofactor, status as status_svc
from app.services import gateway as gw

router = APIRouter(prefix="/control", tags=["control"])


class CommandIn(BaseModel):
    device_id: int
    register_id: int
    value: float
    reason: str = Field(min_length=3, max_length=200, description="Why the change is made. Stored in the log and the audit trail.")
    pulse: bool = Field(False, description="On / off points only: write 1, then 0 again after 1 second (fault reset).")
    password: str | None = Field(None, max_length=200, description="Step-up: the person's password, or the 6-digit authenticator code when 2-step login is on.")
    confirm: bool = Field(False, description="The app sets this only after the person pressed the confirm button.")


class CommandOut(BaseModel):
    id: int
    uid: str
    device_id: int
    device_name: str
    register_id: int
    metric_name: str
    unit: str | None = None
    value: float
    previous_value: float | None = None
    readback: float | None = None
    state: str
    error: str | None = None
    requested_by: str
    reason: str | None = None
    pulse: bool = False
    approved_by: str | None = None
    customer_gateway: bool = False
    created_at: str
    expires_at: str
    delivered_at: str | None = None
    written_at: str | None = None
    finished_at: str | None = None


def _step_up(user: User, body_password: str | None, request: Request, db: Session) -> None:
    """Password (or authenticator code when 2-step login is on) typed again for each write. Wrong attempts are written to the audit trail."""
    if not csettings.get(user.company_name, "write_step_up"):
        return
    ok = False
    if body_password:
        ok = twofactor.check(user, body_password) if user.totp_enabled else verify_password(body_password, user.password_hash)
    if not ok:
        audit.append(db, user.id, user.company_name, "control.stepup_failed", "wrong or missing password / code", client_ip(request))
        raise AppError(403, "STEPUP_FAILED", "Type your password" + (" (or the 6-digit authenticator code)" if user.totp_enabled else "") + " again to send this change.")


def _remote_enable_ok(db: Session, device: Device) -> None:
    """If the device has a point with sensor_type 'remote_enable' (a PLC bit that says 'remote control allowed'), it must be 1."""
    reg = db.scalar(select(DeviceRegister).where(DeviceRegister.device_id == device.id, DeviceRegister.sensor_type == "remote_enable"))
    if reg is None:
        return
    v = db.scalar(select(Telemetry.value).where(Telemetry.device_id == device.id, Telemetry.metric_name == reg.metric_name).order_by(Telemetry.timestamp.desc()).limit(1))
    if not v:
        raise AppError(409, "REMOTE_DISABLED", "The PLC says remote control is switched off (local mode). Switch the machine to remote at the panel first.")


def _dispatch(db, master, company: str, cmd: Command, device: Device, reg: DeviceRegister) -> None:
    try:
        commands.publish(company, cmd.gateway_id, cmd, device, reg)
    except AppError as exc:
        commands._finish(db, company, cmd, "failed", exc.message)
        raise
    commands.track(company, cmd)
    commands._announce(company, cmd)


@router.get("/readiness")
def readiness(device_id: int, user: User = Depends(perms.require("control", 1)),
              db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    """Plain-language checklist of everything that must be true before a value can be written to this device.
    Each item: {ok, title, fix}. Changes nothing. The Control tab shows it so nobody has to guess why a write is refused."""
    company = user.company_name
    device = get_scoped_device(db, user, device_id)
    items: list[dict] = []

    def add(ok: bool, title: str, fix: str) -> None:
        items.append({"ok": bool(ok), "title": title, "fix": "" if ok else fix})

    add(perms.level(user, "control") >= 2, "Your job title may send values",
        "Ask the Super admin to give you Control = Read and Write (Users screen).")
    add(csettings.get(company, "write_enabled"), "Writing is switched on for the company",
        "The Super admin switches it on in Settings > Remote control.")
    writable = db.scalars(select(DeviceRegister).where(DeviceRegister.device_id == device.id, DeviceRegister.writable.is_(True))).all()
    add(bool(writable), "At least one sensor of this device is marked writable",
        "Open the device settings, choose the sensor and tick Writable. Enter the lowest and highest value allowed.")
    ext = next((r for r in writable if extgw.is_ext_register(r)), None) or next((r for r in device.registers if extgw.is_ext_register(r)), None)
    if ext is not None:       # V2.0: a device that comes from a customer gateway
        plant = extgw.split_reg_topic(ext.topic)[0]
        link = master.scalar(select(PlantLink).where(PlantLink.plant_id == plant, PlantLink.company_name == company))
        add(link is not None and link.enabled, "The device's gateway is registered and switched on", "Ask an administrator to check the plant connection (Gateway page).")
        add(extgw.is_online(link), "The customer gateway is connected", "Check the gateway's power, internet and MQTT settings. The Gateway page shows it green when it is connected.")
        return {"ready": all(i["ok"] for i in items), "items": items, "via": "customer_gateway"}
    add(device.protocol in gw.GATEWAY_PROTOCOLS, "This device is read by a plant PC",
        "Only devices read through the plant script can be written to.")
    infos = gw.company_gateways(company)
    gid = gw.responsible_gid(infos, device.state, device.region, device.plant_pc)
    row = master.scalar(select(Gateway).where(Gateway.company_name == company, Gateway.gateway_id == gid))
    online = gwcontrol.is_online(row)
    add(online, "The plant PC is online", "Start the plant script on the plant PC (START.bat). The Gateway page shows it green when it runs.")
    ver = (gwcontrol.load_info(company, gid) or {}).get("ver") if online else None
    if online and ver:
        from app.services.selfupdate import vtuple
        add(vtuple(ver) >= vtuple("3.2"), f"The plant script is new enough to accept commands (running {ver})",
            "Open the Gateway page and download the script once more. After that it updates itself.")
    last = {device.id: db.scalar(select(func.max(Telemetry.timestamp)).where(Telemetry.device_id == device.id))}
    live = online and status_svc.device_states(company, [device], last, infos, {gid: row} if row else {})[device.id] == "online"
    add(live, "The device is answering (live data)", "Check the cable / network between the plant PC and the PLC.")
    remote = db.scalar(select(DeviceRegister).where(DeviceRegister.device_id == device.id, DeviceRegister.sensor_type == "remote_enable"))
    if remote is not None:
        v = db.scalar(select(Telemetry.value).where(Telemetry.device_id == device.id, Telemetry.metric_name == remote.metric_name)
                      .order_by(Telemetry.timestamp.desc()).limit(1))
        add(bool(v), "The PLC allows remote control (the remote bit is on)", "Switch the machine from local to remote at its panel.")
    return {"ready": all(i["ok"] for i in items), "items": items}


@router.post("/commands", response_model=CommandOut, status_code=201)
def send_command(body: CommandIn, request: Request, user: User = Depends(perms.require("control", 2)),
                 db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    company = user.company_name
    if not body.confirm:
        raise AppError(422, "CONFIRM_REQUIRED", "Confirm the change first.")
    if not csettings.get(company, "write_enabled"):
        raise AppError(403, "WRITE_LOCKED", "Writing to the plant is switched off for this company. The Super admin can switch it on in Settings.")
    device = get_scoped_device(db, user, body.device_id)
    reg = db.scalar(select(DeviceRegister).where(DeviceRegister.id == body.register_id, DeviceRegister.device_id == device.id))
    if not reg:
        raise AppError(404, "REGISTER_NOT_FOUND", "Sensor not found on this device.")
    if not reg.writable:
        raise AppError(403, "NOT_WRITABLE", "This sensor is not marked as writable.")
    ext = extgw.is_ext_register(reg)
    if not ext and device.protocol not in gw.GATEWAY_PROTOCOLS:
        raise AppError(409, "PROTOCOL_NOT_WRITABLE", "Only devices read by a plant PC or by a customer gateway can be written to.")
    if body.pulse and not (reg.data_type == "bool" or reg.register_type == "coil"):
        raise AppError(422, "PULSE_NEEDS_BIT", "A pulse (fault reset) works on on / off points only.")
    value = 1.0 if body.pulse else body.value
    raw = commands.to_raw(reg, value)
    _step_up(user, body.password, request, db)
    _remote_enable_ok(db, device)

    if ext:
        plant = extgw.split_reg_topic(reg.topic)[0]
        link = master.scalar(select(PlantLink).where(PlantLink.plant_id == plant, PlantLink.company_name == company))
        if link is None or not link.enabled:
            raise AppError(409, "GATEWAY_NOT_FOUND", "The gateway of this device was not found or is switched off.")
        if not extgw.is_online(link):
            raise AppError(409, "PLANT_OFFLINE", "The customer gateway of this device is offline. Nothing was sent.")
        gid = extgw.EXT_PREFIX + plant
    else:
        infos = gw.company_gateways(company)
        gid = gw.responsible_gid(infos, device.state, device.region, device.plant_pc)
        row = master.scalar(select(Gateway).where(Gateway.company_name == company, Gateway.gateway_id == gid))
        if not gwcontrol.is_online(row):
            raise AppError(409, "PLANT_OFFLINE", "The plant PC of this device is offline. Nothing was sent.")
        last = {device.id: db.scalar(select(func.max(Telemetry.timestamp)).where(Telemetry.device_id == device.id))}
        state = status_svc.device_states(company, [device], last, infos, {gid: row} if row else {})[device.id]
        if state != "online":
            raise AppError(409, "DEVICE_NOT_LIVE", "This device is not live right now (grey or offline), so the write was refused. Check the connection first.")

    busy = db.scalar(select(Command.id).where(Command.register_id == reg.id, Command.state.in_(("awaiting", "sent", "delivered", "written"))))
    if busy:
        raise AppError(409, "COMMAND_PENDING", "Another change to this sensor is still in progress. Wait for its result.")

    previous = db.scalar(select(Telemetry.value).where(Telemetry.device_id == device.id, Telemetry.metric_name == reg.metric_name)
                         .order_by(Telemetry.timestamp.desc()).limit(1))
    now = utcnow()
    needs_approval = csettings.get(company, "write_approval")
    cmd = Command(uid=commands.new_uid(), device_id=device.id, device_name=device.device_name, register_id=reg.id,
                  metric_name=reg.metric_name, unit=reg.unit, value=value, raw_value=raw, previous_value=previous,
                  state="awaiting" if needs_approval else "sent", requested_by_id=user.id, requested_by=user.username, gateway_id=gid,
                  state_name=device.state, region=device.region, created_at=now, reason=body.reason.strip(), pulse=body.pulse,
                  expires_at=now + timedelta(seconds=csettings.get(company, "command_expiry_seconds") * (10 if needs_approval else 1)))
    db.add(cmd)
    db.commit()
    db.refresh(cmd)
    audit.append(db, user.id, company, "control.request",
                 f"device:{device.id}/{reg.metric_name}: {previous if previous is not None else '?'} -> {value:g}"
                 f"{' (pulse)' if body.pulse else ''} reason: {cmd.reason[:80]}", client_ip(request))
    if needs_approval:
        commands._announce(company, cmd)
        return commands.command_dict(cmd)
    _dispatch(db, master, company, cmd, device, reg)
    return commands.command_dict(cmd)


def _awaiting(db: Session, user: User, command_id: int) -> tuple[Command, Device, DeviceRegister]:
    c = db.get(Command, command_id)
    row = db.execute(scope_devices(select(Device).where(Device.id == c.device_id), user)).first() if c else None
    if not c or not row:
        raise AppError(404, "COMMAND_NOT_FOUND", "Command not found or outside your assigned state/region.")
    if c.state != "awaiting":
        raise AppError(409, "NOT_AWAITING", "This change is not waiting for approval.")
    return c, row[0], db.get(DeviceRegister, c.register_id)


@router.post("/commands/{command_id}/approve", response_model=CommandOut)
def approve(command_id: int, request: Request, user: User = Depends(perms.require("control", 2)), db: Session = Depends(get_company_db),
            master: Session = Depends(get_master_db)):
    """Second-person approval (company setting write_approval). The person who asked can never approve their own change."""
    c, device, reg = _awaiting(db, user, command_id)
    if c.requested_by_id == user.id:
        raise AppError(403, "SELF_APPROVAL", "Another person must approve this change.")
    if as_utc(c.expires_at) < utcnow():
        commands._finish(db, user.company_name, c, "expired", "Nobody approved it in time.")
        raise AppError(409, "APPROVAL_EXPIRED", "This change was not approved in time and was cancelled.")
    now = utcnow()
    c.state, c.approved_by, c.expires_at = "sent", user.username, now + timedelta(seconds=csettings.get(user.company_name, "command_expiry_seconds"))
    db.commit()
    audit.append(db, user.id, user.company_name, "control.approve", f"command:{c.id}", client_ip(request))
    _dispatch(db, master, user.company_name, c, device, reg)
    return commands.command_dict(c)


@router.post("/commands/{command_id}/reject", response_model=CommandOut)
def reject(command_id: int, request: Request, user: User = Depends(perms.require("control", 2)), db: Session = Depends(get_company_db)):
    c, _, _ = _awaiting(db, user, command_id)
    commands._finish(db, user.company_name, c, "rejected", f"Rejected by {user.username}.")
    return commands.command_dict(c)


@router.get("/commands", response_model=list[CommandOut])
def list_commands(device_id: int | None = None, limit: int = 50, user: User = Depends(perms.require("control", 1)),
                  db: Session = Depends(get_company_db)):
    stmt = scope_devices(select(Command, Device).join(Device, Device.id == Command.device_id), user).order_by(Command.id.desc()).limit(min(max(limit, 1), 200))
    if device_id:
        stmt = stmt.where(Command.device_id == device_id)
    rows = [c for c, _ in db.execute(stmt).all()]
    commands.resolve_late(db, user.company_name, rows)
    return [commands.command_dict(c) for c in rows]


@router.get("/commands/{command_id}", response_model=CommandOut)
def get_command(command_id: int, user: User = Depends(perms.require("control", 1)), db: Session = Depends(get_company_db)):
    row = db.execute(scope_devices(select(Command, Device).join(Device, Device.id == Command.device_id).where(Command.id == command_id), user)).first()
    if not row:
        raise AppError(404, "COMMAND_NOT_FOUND", "Command not found or outside your assigned state/region.")
    commands.resolve_late(db, user.company_name, [row[0]])
    return commands.command_dict(row[0])
