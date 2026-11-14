"""Single ingestion path used by REST (/devices/{id}/telemetry) and the MQTT subscriber."""
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError
from app.core.timeutil import as_utc, utcnow
from app.models.tenant import Device, Telemetry
from app.services import alarms, lastseen, live, sync


# Only CHANGED values are stored: an unchanged reading is not written again (the old value stays valid until a new
# one arrives). A small in-memory map makes that cheap; "last seen" (online / offline) is kept in Valkey by
# app.services.lastseen so that every process (ingest worker, API workers) sees the same value without new rows.
_LAST_STORED: dict[tuple[str, int, str], tuple[float, datetime]] = {}   # (company, device_id, metric) -> (value, timestamp) last stored
# (the company is part of the key: every company's database numbers its devices from 1)


def _load_last_stored(db: Session, company: str, device_id: int, metrics: list[str]) -> None:
    missing = [m for m in metrics if (company, device_id, m) not in _LAST_STORED]
    if not missing:
        return
    for metric, value, ts in db.execute(text("""
            SELECT DISTINCT ON (metric_name) metric_name, value, "timestamp"
            FROM telemetry WHERE device_id = :d AND metric_name = ANY(:m)
            ORDER BY metric_name, "timestamp" DESC"""), {"d": device_id, "m": missing}):
        _LAST_STORED[(company, device_id, metric)] = (value, ts)


def ingest_readings(db: Session, master: Session | None, device: Device,
                    readings: list[tuple[str, float, datetime | None]], pre: dict | None = None) -> dict:
    """readings carry the RAW value as sent by the device/gateway. The register's scaling factor
    (e.g. x0.1 for 852 -> 85.2) is applied here, once, for every source (REST, MQTT and the poller)."""
    now = utcnow()
    known = {r.metric_name for r in device.registers}
    scale = {r.metric_name: (r.scale_factor or 1.0) for r in device.registers}
    unknown = sorted({m for m, _, _ in readings if m not in known})
    if unknown:
        raise AppError(422, "UNKNOWN_METRIC",
                       f"Unmapped metric(s): {', '.join(unknown[:5])}. Add them to the device's register mapping first.")

    rows, seen, latest = [], set(), {}
    for metric, value, ts in readings:
        ts = as_utc(ts) if ts else now
        if ts > now + timedelta(minutes=5):
            raise AppError(422, "FUTURE_TIMESTAMP", "Readings cannot be timestamped in the future. Check the gateway clock (UTC).")
        key = (ts, metric)
        if key in seen:
            continue
        seen.add(key)
        value = float(value) * scale[metric]
        rows.append({"device_id": device.id, "timestamp": ts, "metric_name": metric, "value": value})
        if metric not in latest or ts > latest[metric][1]:
            latest[metric] = (value, ts)

    # keep only readings whose value differs from the last stored value of that sensor
    rows.sort(key=lambda r: r["timestamp"])
    company = device.company_name
    metrics = sorted({r["metric_name"] for r in rows})
    _load_last_stored(db, company, device.id, metrics)
    pending = {(company, device.id, m): _LAST_STORED[(company, device.id, m)] for m in metrics if (company, device.id, m) in _LAST_STORED}
    kept = []
    for r in rows:
        k = (company, device.id, r["metric_name"])
        prev = pending.get(k)
        if prev and r["timestamp"] > prev[1] and r["value"] == prev[0]:
            continue  # same value as before -> nothing new to store
        kept.append(r)
        if not prev or r["timestamp"] >= prev[1]:
            pending[k] = (r["value"], r["timestamp"])
    rows = kept

    inserted = 0
    for i in range(0, len(rows), 1000):
        stmt = pg_insert(Telemetry).values(rows[i:i + 1000]).on_conflict_do_nothing(
            index_elements=["device_id", "timestamp", "metric_name"])
        inserted += db.execute(stmt).rowcount or 0
    db.commit()
    _LAST_STORED.update(pending)
    lastseen.record(device.company_name, device.id, now)

    live.publish_many(device.company_name, [{   # the screen first (a person is waiting), the copy for hybrid storage after
        "type": "telemetry", "device_id": device.id, "metric_name": metric, "value": value,
        "timestamp": ts.isoformat(), "state": device.state, "region": device.region} for metric, (value, ts) in latest.items()])
    sync.mirror(db, "telemetry", [], rows=[{**r, "timestamp": r["timestamp"].isoformat()} for r in rows])
    # FUTURE OUTBOUND CONNECTOR (readings): add ONE call here, e.g. outbound.send("reading.new", payload), so an ERP / MES / webhook receives this.
    # Nothing is sent today. Keep it behind a company setting and a timeout so a slow receiver can never block the plant data.
    # readings that arrive LATE (gateway was cut off): written to the alarm list as closed "received late" entries, nobody is paged,
    # and they neither open nor close a live alarm; only fresh readings go through the normal alarm check
    cutoff = now - timedelta(seconds=settings.LATE_READING_SECONDS)
    alarms.record_late(db, device, rows, now)
    fresh = {m: v for m, v in latest.items() if v[1] >= cutoff}
    if fresh:
        alarms.evaluate(db, master, device, fresh, pre)
    return {"received": len(readings), "inserted": inserted, "duplicates": len(readings) - inserted}
