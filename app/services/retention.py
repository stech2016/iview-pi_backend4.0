"""Storage: how full is the company database, the 'keep data for' time, freeing space, and the nightly clean-up.

keep_days 0 = keep until the user deletes (default). quota_gb is set by Siemas (scripts/set_quota.py); regions get a share of it
(StorageShare, set by the Super admin). Level: ok, amber from 80 %, red from 90 %."""
import time
from datetime import timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.models.master import StoragePolicy, StorageShare
from app.models.tenant import Device, Telemetry
from app.services import audit

KEEP_CHOICES = (0, 30, 90, 180, 365, 730, 1095, 1825)     # days: keep until deleted, 1/3/6 months, 1/2/3/5 years
GB = 1024 ** 3
_cache: dict[str, tuple[float, dict]] = {}


def policy(master: Session, company: str, create: bool = False) -> StoragePolicy | None:
    p = master.scalar(select(StoragePolicy).where(StoragePolicy.company_name == company))
    if not p and create:
        p = StoragePolicy(company_name=company, keep_days=0)
        master.add(p)
        master.flush()
    return p


def level(percent: float | None) -> str:
    if percent is None:
        return "none"
    return "red" if percent >= 90 else "amber" if percent >= 80 else "ok"


def db_bytes(db: Session) -> int:
    return int(db.scalar(text("SELECT pg_database_size(current_database())")) or 0)


def region_rows(db: Session) -> dict[tuple[str, str], int]:
    """Stored reading count per (state, region); cached 5 minutes (counting is slow on a very large table)."""
    hit = _cache.get(str(db.get_bind().url))
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    rows = {(s, r): n for s, r, n in db.execute(
        select(Device.state, Device.region, func.count()).select_from(Telemetry)
        .join(Device, Device.id == Telemetry.device_id).group_by(Device.state, Device.region))}
    _cache[str(db.get_bind().url)] = (time.monotonic(), rows)
    return rows


def usage(master: Session, db: Session, company: str, state: str | None = None, region: str | None = None) -> dict:
    """Whole company, or one State + Region (its share of the quota and its estimated part of the used space)."""
    p = policy(master, company)
    used = db_bytes(db)
    quota = (p.quota_gb if p and p.quota_gb else None)
    out = {"keep_days": (p.keep_days or 0) if p else 0, "quota_gb": quota, "used_gb": round(used / GB, 3)}
    limit = quota
    if state and region:
        rows = region_rows(db)
        total = sum(rows.values()) or 1
        used = int(used * rows.get((state, region), 0) / total)
        share = master.scalar(select(StorageShare.share_gb).where(
            StorageShare.company_name == company, StorageShare.state == state, StorageShare.region == region))
        limit = share
        out.update(share_gb=share, used_gb=round(used / GB, 3))
    percent = round(used / (limit * GB) * 100, 1) if limit else None
    out.update(limit_gb=limit, free_gb=round(limit - used / GB, 3) if limit else None, percent=percent, level=level(percent))
    return out


def delete_older(db: Session, days: int, state: str | None = None, region: str | None = None) -> int:
    cut = utcnow() - timedelta(days=days)
    stmt = delete(Telemetry).where(Telemetry.timestamp < cut)
    if state and region:
        stmt = stmt.where(Telemetry.device_id.in_(select(Device.id).where(Device.state == state, Device.region == region)))
    n = db.execute(stmt).rowcount or 0
    db.commit()
    _cache.clear()
    return n


def nightly(master: Session, company: str, db: Session, hour_now: int) -> int:
    """Called every minute by the jobs loop; acts once per day at PURGE_HOUR for companies that chose a 'keep for' time."""
    from app.core.config import settings
    p = policy(master, company)
    if not p or not p.keep_days or hour_now != settings.PURGE_HOUR:
        return 0
    if p.last_purge_at and p.last_purge_at > utcnow() - timedelta(hours=20):
        return 0
    n = delete_older(db, p.keep_days)
    p.last_purge_at = utcnow()
    master.commit()
    audit.append(db, None, company, "storage.purge", f"older_than_days:{p.keep_days}:rows:{n}", None)
    return n
