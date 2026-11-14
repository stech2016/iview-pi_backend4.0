"""ERP export: one flat row per plant, device, sensor and day (min / average / max / last), ready to import into an ERP or Excel."""
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.tenant import Device
from app.services.export import tz_label

HEADERS = ["Company", "Date", "State", "Region", "City", "Plant", "Device ID", "Device", "Sensor", "Unit", "Minimum", "Average", "Maximum", "Last value", "Readings"]


def rows(db: Session, devices: list[Device], date_from: datetime, date_to: datetime, offset_minutes: int) -> list[list]:
    if not devices:
        return []
    by_id = {d.id: d for d in devices}
    units = {(r.device_id, r.metric_name): r.unit or "" for d in devices for r in d.registers}
    out = []
    for dev_id, day, metric, lo, avg, hi, last, n in db.execute(text("""
            SELECT device_id, (("timestamp" AT TIME ZONE 'UTC') + make_interval(mins => :off))::date AS day, metric_name,
                   min(value), avg(value), max(value), (array_agg(value ORDER BY "timestamp" DESC))[1], count(*)
            FROM telemetry WHERE device_id = ANY(:ids) AND "timestamp" >= :a AND "timestamp" < :b
            GROUP BY device_id, day, metric_name ORDER BY day, device_id, metric_name"""),
            {"ids": list(by_id), "a": date_from, "b": date_to, "off": offset_minutes}):
        d = by_id[dev_id]
        out.append([d.company_name, day.isoformat(), d.state, d.region, d.city or "", d.plant_name or "", d.id, d.device_name, metric,
                    units.get((dev_id, metric), ""), lo, round(avg, 4), hi, last, n])
    return out


def title(company: str = "") -> str:
    return f"STECH iVIEW - {company + ' - ' if company else ''}ERP export (days in {tz_label()})"
