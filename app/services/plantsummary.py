"""Plant health panel, alarm response report and the snapshot attached to a shift handover - one calculation for all three."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import as_utc, utcnow
from app.models.tenant import Alarm, AlarmShelf, Command, Device
from app.services import comm, csettings, gwcontrol

STUCK_SUFFIX = " [stuck]"


def _overlap_seconds(a: Alarm, start: datetime, end: datetime) -> float:
    lo, hi = max(as_utc(a.raised_at), start), min(as_utc(a.cleared_at) if a.cleared_at else end, end)
    return max(0.0, (hi - lo).total_seconds())


def health(db: Session, company: str, devices: list[Device], states: dict[int, str]) -> dict:
    """`devices` are the ones the caller may see (State + Region); `states` their live status. Devices in maintenance are left out of the score."""
    now = utcnow()
    real = [d for d in devices if d.protocol != "Gateway"]
    in_maint = {d.id for d in real if d.maintenance_until and as_utc(d.maintenance_until) > now}
    counted = [d for d in real if d.id not in in_maint]
    ids = [d.id for d in devices]
    online = sum(1 for d in counted if states.get(d.id) == "online")

    open_alarms = list(db.scalars(select(Alarm).where(Alarm.device_id.in_(ids), Alarm.cleared_at.is_(None)))) if ids else []
    shelved = set(db.execute(select(AlarmShelf.device_id, AlarmShelf.metric_name).where(AlarmShelf.until > now)).all())
    by_sev = {"critical": 0, "major": 0, "minor": 0, "advisory": 0}
    stuck = stale = 0
    for a in open_alarms:
        by_sev[a.severity] = by_sev.get(a.severity, 0) + 1
        if (a.metric_name or "").endswith(STUCK_SUFFIX):
            stuck += 1
        if not a.acknowledged_by and now - as_utc(a.raised_at) > timedelta(hours=csettings.get(company, "stale_alarm_hours")):
            stale += 1

    day_ago, hour_ago = now - timedelta(hours=24), now - timedelta(hours=1)
    recent = list(db.scalars(select(Alarm).where(Alarm.device_id.in_(ids), Alarm.raised_at >= day_ago))) if ids else []
    per_hour_now = sum(1 for a in recent if as_utc(a.raised_at) >= hour_ago)
    per_hour_avg = round(len(recent) / 24.0, 1)
    acked = [(as_utc(a.acknowledged_at) - as_utc(a.raised_at)).total_seconds() for a in recent if a.acknowledged_at]
    avg_ack = round(sum(acked) / len(acked)) if acked else None

    # uptime: time a device was silent (comm alarm) or its plant PC was offline (gateway alarm of that State + Region), over the last 24 h
    down = 0.0
    if counted:
        pseudo_ids = [d.id for d in devices if d.protocol == "Gateway"]
        comm_alarms = list(db.scalars(select(Alarm).where(Alarm.device_id.in_([d.id for d in counted]), Alarm.metric_name == comm.METRIC,
                                                          (Alarm.cleared_at.is_(None)) | (Alarm.cleared_at >= day_ago))))
        for a in comm_alarms:
            down += _overlap_seconds(a, day_ago, now)
        if pseudo_ids:
            for a in db.scalars(select(Alarm).where(Alarm.device_id.in_(pseudo_ids), Alarm.metric_name == gwcontrol.GATEWAY_METRIC,
                                                    (Alarm.cleared_at.is_(None)) | (Alarm.cleared_at >= day_ago))):
                pd = next(d for d in devices if d.id == a.device_id)
                n = sum(1 for d in counted if (d.state, d.region) == (pd.state, pd.region))
                down += _overlap_seconds(a, day_ago, now) * n
        uptime = max(0.0, 100.0 * (1 - down / (86400.0 * len(counted))))
    else:
        uptime = 100.0

    online_pct = 100.0 * online / len(counted) if counted else 100.0
    target, limit = csettings.get(company, "alarms_per_hour_target"), csettings.get(company, "alarms_per_hour_limit")
    load_pct = 100.0 if per_hour_now <= target else 0.0 if per_hour_now >= limit else 100.0 * (limit - per_hour_now) / (limit - target)
    ack_pct = 100.0 - min(100.0, stale * 25.0)
    score = round(0.4 * online_pct + 0.3 * uptime + 0.2 * load_pct + 0.1 * ack_pct)
    return {
        "score": score, "devices_total": len(counted), "devices_online": online, "devices_in_maintenance": len(in_maint),
        "active_alarms": len(open_alarms), "active_by_severity": by_sev, "shelved_alarms": len(shelved),
        "alarms_last_hour": per_hour_now, "alarms_per_hour_avg_24h": per_hour_avg,
        "alarms_per_hour_target": target, "alarms_per_hour_limit": limit,
        "alarm_load": "ok" if per_hour_now <= target else "high" if per_hour_now < limit else "over_limit",
        "stale_alarms": stale, "stale_alarm_hours": csettings.get(company, "stale_alarm_hours"),
        "uptime_24h_pct": round(uptime, 1), "avg_ack_seconds": avg_ack, "stuck_sensors": stuck,
        "generated_at": now.isoformat(),
    }


def alarm_report(db: Session, devices: list[Device], days: int) -> dict:
    """Alarm response report: how many alarms, how fast people answered, which devices and sensors alarm the most."""
    now = utcnow()
    since = now - timedelta(days=days)
    ids = [d.id for d in devices]
    names = {d.id: d.device_name for d in devices}
    rows = list(db.scalars(select(Alarm).where(Alarm.device_id.in_(ids), Alarm.raised_at >= since))) if ids else []
    total, acked = len(rows), [a for a in rows if a.acknowledged_at]
    secs = sorted((as_utc(a.acknowledged_at) - as_utc(a.raised_at)).total_seconds() for a in acked)
    pick = lambda p: round(secs[min(len(secs) - 1, int(p * len(secs)))]) if secs else None  # noqa: E731
    top: dict[tuple, int] = {}
    for a in rows:
        k = (a.device_id, a.metric_name or "")
        top[k] = top.get(k, 0) + 1
    worst = sorted(top.items(), key=lambda kv: -kv[1])[:10]
    sev: dict[str, int] = {}
    for a in rows:
        sev[a.severity] = sev.get(a.severity, 0) + 1
    return {
        "days": days, "since": since.isoformat(), "total": total, "acknowledged": len(acked), "unacknowledged": total - len(acked),
        "still_open": sum(1 for a in rows if a.cleared_at is None), "by_severity": sev,
        "ack_seconds_avg": round(sum(secs) / len(secs)) if secs else None, "ack_seconds_median": pick(0.5), "ack_seconds_p90": pick(0.9),
        "alarms_per_hour_avg": round(total / (days * 24.0), 2),
        "worst_actors": [{"device_id": d, "device_name": names.get(d), "metric": m, "count": n} for (d, m), n in worst],
    }


def commands_since(db: Session, since: datetime, ids: list[int]) -> list[dict]:
    rows = db.scalars(select(Command).where(Command.device_id.in_(ids), Command.created_at >= since).order_by(Command.id.desc()).limit(30)) if ids else []
    return [{"device": c.device_name, "metric": c.metric_name, "value": c.value, "state": c.state, "by": c.requested_by, "at": c.created_at.isoformat()} for c in rows]
