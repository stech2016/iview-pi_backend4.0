"""Background jobs, started once by the ingest worker. Every minute:
  - sensors that have shown the same value for too long get a "stuck sensor" warning,
  - scheduled reports that are due are e-mailed,
  - at PURGE_HOUR each company's old readings are deleted (only if the company chose a 'keep for' time).
Add another timed job = one more call inside tick()."""
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger
from app.core.timeutil import utcnow
from app.db.session import MasterSession, tenant_session
from app.models.master import CompanyStorage, ReportSchedule
from app.models.tenant import Alarm, Device, Telemetry
from app.services import alertrules, downtime, erp, mail, retention, stuck, tablefile
from app.services.export import dashboard_rows, build_dashboard_export

log = get_logger("jobs")


def _local_now() -> datetime:
    return utcnow() + timedelta(minutes=settings.PLANT_UTC_OFFSET_MINUTES)


def _is_due(s: ReportSchedule, local: datetime) -> bool:
    if s.hour != local.hour or (s.frequency == "weekly" and s.weekday != local.weekday()):
        return False
    return not (s.last_sent_at and s.last_sent_at > utcnow() - timedelta(hours=20))


def _build(s: ReportSchedule, db):
    devices = list(db.scalars(select(Device).where(Device.protocol != "Gateway").order_by(Device.device_name)))
    if s.kind == "erp":
        end = utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(minutes=settings.PLANT_UTC_OFFSET_MINUTES)
        end = end if end < utcnow() else end - timedelta(days=1)
        start = end - timedelta(days=1 if s.frequency == "daily" else 7)
        rows = erp.rows(db, devices, start, end, settings.PLANT_UTC_OFFSET_MINUTES)
        return tablefile.build(erp.title(s.company_name), erp.HEADERS, rows, s.fmt, f"{s.company_name}_erp", group=8)
    from sqlalchemy import func
    last = dict(db.execute(select(Telemetry.device_id, func.max(Telemetry.timestamp)).where(
        Telemetry.timestamp > utcnow() - timedelta(days=365)).group_by(Telemetry.device_id)).all())
    active = dict(db.execute(select(Alarm.device_id, func.count()).where(Alarm.cleared_at.is_(None)).group_by(Alarm.device_id)).all())
    return build_dashboard_export(dashboard_rows(db, devices, active, last), s.fmt, s.company_name)


def send_report(master, s: ReportSchedule) -> None:
    with tenant_session(master, s.company_name) as db:
        tmp, filename, media = _build(s, db)
    data = tmp.read()
    for to in [x.strip() for x in s.recipients.split(",") if x.strip()]:
        mail.send_now(to, f"STECH iVIEW - {s.name}", f"<p>{s.name}: the report is attached.</p>", (filename, data, media))
    s.last_sent_at = utcnow()
    master.commit()


def tick() -> None:
    local = _local_now()
    with MasterSession() as master:
        stuck_sensors(master)
        if mail.configured():
            for s in master.scalars(select(ReportSchedule)):
                if _is_due(s, local):
                    try:
                        send_report(master, s)
                        log.info("report_sent", company=s.company_name, schedule=s.id)
                    except Exception as exc:
                        master.rollback()
                        log.warning("report_failed", company=s.company_name, schedule=s.id, error=type(exc).__name__)
        for st in master.scalars(select(CompanyStorage).where(CompanyStorage.db_name.is_not(None))):
            try:
                with tenant_session(master, st.company_name) as db:
                    retention.nightly(master, st.company_name, db, local.hour)
            except Exception as exc:
                master.rollback()
                log.warning("purge_failed", company=st.company_name, error=type(exc).__name__)


def stuck_sensors(master) -> None:
    for st in master.scalars(select(CompanyStorage).where(CompanyStorage.db_name.is_not(None))):
        try:
            with tenant_session(master, st.company_name) as db:
                stuck.check_company(master, st.company_name, db)
                alertrules.check_silence(master, st.company_name, db)     # no_data alert rules
                downtime.check_company(st.company_name, db)               # downtime log (auto stop records)
        except Exception as exc:
            master.rollback()
            log.warning("stuck_check_failed", company=st.company_name, error=type(exc).__name__)


def loop() -> None:
    while True:
        try:
            tick()
        except Exception as exc:
            log.warning("jobs_tick_failed", error=type(exc).__name__)
        time.sleep(60 - datetime.now(timezone.utc).second)   # wake at the start of each minute
