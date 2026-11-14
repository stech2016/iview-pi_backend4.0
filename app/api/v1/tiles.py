"""Dashboard tiles: one tile per State + Region the caller may see, with plants, devices online and open alarms."""
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.db.session import get_master_db
from app.deps import get_company_db, get_current_user, scope_devices
from app.api.v1.devices import _merge_seen, _statuses
from app.models.master import User
from app.models.tenant import Alarm, Device, Telemetry

router = APIRouter(prefix="/tiles", tags=["dashboard"])


@router.get("")
def tiles(user: User = Depends(get_current_user), db: Session = Depends(get_company_db), master: Session = Depends(get_master_db)):
    devices = list(db.scalars(scope_devices(select(Device).where(Device.protocol != "Gateway"), user)))
    last = _merge_seen(user.company_name, dict(db.execute(select(Telemetry.device_id, func.max(Telemetry.timestamp))
                                       .where(Telemetry.timestamp > utcnow() - timedelta(days=365)).group_by(Telemetry.device_id)).all()))
    states, _ = _statuses(master, user.company_name, devices, last)
    open_alarms = dict(db.execute(select(Alarm.device_id, func.count()).where(Alarm.cleared_at.is_(None)).group_by(Alarm.device_id)).all())
    out: dict[tuple[str, str], dict] = {}
    for d in devices:
        t = out.setdefault((d.state, d.region), {"state": d.state, "region": d.region, "plants": set(), "devices": 0, "online": 0, "alarms": 0})
        t["devices"] += 1
        t["online"] += states.get(d.id) == "online"
        t["alarms"] += open_alarms.get(d.id, 0)
        if d.plant_name:
            t["plants"].add(d.plant_name)
    return [{**t, "plants": sorted(t["plants"])} for t in sorted(out.values(), key=lambda x: (x["state"], x["region"]))]
