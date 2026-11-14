"""Telemetry export (csv / xlsx / pdf) streamed from the DB with yield_per into a spooled temp file."""
import csv
import io
import tempfile
from datetime import datetime, timedelta, timezone

from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.constants import EXPORT_LIMIT_MESSAGE, EXPORT_MAX_BYTES, EXPORT_MAX_DAYS
from app.core import tzctx
from app.core.errors import AppError
from app.models.tenant import Alarm, Device, DeviceRegister, Telemetry

BYTES_PER_ROW_ESTIMATE = 64
XLSX_MAX_ROWS = 1_000_000
PDF_MAX_ROWS = 200_000


def _offset_minutes() -> int:
    """The viewer's own offset (sent by the app with the request); without it the plant default (330 = IST, UTC+05:30)."""
    return tzctx.offset_minutes(int(getattr(settings, "PLANT_UTC_OFFSET_MINUTES", 330)))


def plant_tz() -> timezone:
    """Plant local time = UTC + PLANT_UTC_OFFSET_MINUTES (default 330 = IST). Reports show this, the database keeps UTC."""
    return timezone(timedelta(minutes=_offset_minutes()))


def tz_label() -> str:
    m = _offset_minutes()
    hours, minutes = divmod(abs(m), 60)
    return f"UTC{'+' if m >= 0 else '-'}{hours:02d}:{minutes:02d}"


def local_str(ts: datetime) -> str:
    return local_text(ts, "%Y-%m-%d %H:%M:%S")


def local_text(ts, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """A UTC moment (datetime, or an ISO text) as the viewer's local time. A time without zone is taken as UTC."""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(plant_tz()).strftime(fmt)


def too_large() -> AppError:
    return AppError(413, "EXPORT_TOO_LARGE", EXPORT_LIMIT_MESSAGE)


def build_export(db: Session, device: Device, fmt: str, date_from: datetime, date_to: datetime, metric: str | None):
    if date_from >= date_to:
        raise AppError(422, "INVALID_DATE_RANGE", "The start date must be earlier than the end date. Please adjust the date filter.")
    if date_to - date_from > timedelta(days=EXPORT_MAX_DAYS):
        raise too_large()

    stmt = select(Telemetry.timestamp, Telemetry.metric_name, Telemetry.value).where(
        Telemetry.device_id == device.id, Telemetry.timestamp >= date_from, Telemetry.timestamp <= date_to)
    if metric:
        stmt = stmt.where(Telemetry.metric_name == metric)

    count = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    row_cap = PDF_MAX_ROWS if fmt == "pdf" else XLSX_MAX_ROWS
    if count > row_cap or count * BYTES_PER_ROW_ESTIMATE > EXPORT_MAX_BYTES:
        raise too_large()

    rows = db.execute(stmt.order_by(Telemetry.timestamp).execution_options(yield_per=1000))
    tmp = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    stamp = local_text(date_to, "%Y%m%d")
    safe = "".join(c if c.isalnum() else "_" for c in device.device_name)[:40]

    if fmt == "csv":
        text_tmp = io.TextIOWrapper(tmp, encoding="utf-8-sig", newline="", write_through=True)
        w = csv.writer(text_tmp)
        w.writerow([f"Timestamp ({tz_label()})", "Metric", "Value"])
        for ts, m, v in rows:
            w.writerow([local_str(ts), m, v])
        text_tmp.detach()  # keep tmp open
        media = "text/csv"
        filename = f"{safe}_telemetry_{stamp}.csv"
    elif fmt == "xlsx":
        wb = Workbook(write_only=True)
        ws = wb.create_sheet("Telemetry")
        ws.append([f"Timestamp ({tz_label()})", "Metric", "Value"])
        for ts, m, v in rows:
            ws.append([ts.astimezone(plant_tz()).replace(tzinfo=None), m, v])
        wb.save(tmp)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"{safe}_telemetry_{stamp}.xlsx"
    else:
        c = canvas.Canvas(tmp, pagesize=A4)
        width, height = A4

        def header(page: int):
            c.setFont("Helvetica-Bold", 12)
            c.drawString(40, height - 40, f"STECH iVIEW - Telemetry report: {device.device_name}")
            c.setFont("Helvetica", 8)
            c.drawString(40, height - 54, f"{date_from.astimezone(plant_tz()):%Y-%m-%d %H:%M} to {date_to.astimezone(plant_tz()):%Y-%m-%d %H:%M} {tz_label()}"
                         + (f" | metric: {metric}" if metric else "") + f" | page {page}")
            c.setFont("Helvetica-Bold", 9)
            c.drawString(40, height - 76, f"Timestamp ({tz_label()})")
            c.drawString(210, height - 76, "Metric")
            c.drawString(400, height - 76, "Value")
            c.setFont("Helvetica", 8)
            return height - 92

        page, y = 1, header(1)
        for ts, m, v in rows:
            if y < 40:
                c.showPage()
                page += 1
                y = header(page)
            c.drawString(40, y, local_str(ts))
            c.drawString(210, y, m[:40])
            c.drawString(400, y, f"{v:.4f}")
            y -= 11
        c.save()
        media = "application/pdf"
        filename = f"{safe}_telemetry_{stamp}.pdf"

    if tmp.tell() > EXPORT_MAX_BYTES:
        tmp.close()
        raise too_large()
    tmp.seek(0)
    return tmp, filename, media


def iter_file(f, chunk: int = 64 * 1024):
    try:
        while True:
            data = f.read(chunk)
            if not data:
                break
            yield data
    finally:
        f.close()


def dash_headers() -> list[str]:
    return ["Device", "Protocol", "IP address", "Port", "State", "Region", "City", "Plant name", "Status", "Active alarms",
            f"Last seen ({tz_label()})", "Metric", "Sensor", "Unit", "Latest value", "Low limit", "High limit"]


def dashboard_rows(db: Session, devices: list[Device], active: dict, last_seen: dict) -> list[list]:
    """One flat row per sensor (devices without registers get one row).
    Status, Active alarms and Last seen are per SENSOR: only a sensor that is really in alarm shows an alarm,
    and Last seen is the time of that sensor's own latest recorded reading, in plant local time."""
    ids = [d.id for d in devices]
    latest: dict[tuple[int, str], tuple[float, datetime]] = {}
    alarms: dict[tuple[int, str], int] = {}
    if ids:
        since = datetime.now(timezone.utc) - timedelta(days=365)
        for dev_id, metric, value, ts in db.execute(text("""
                SELECT DISTINCT ON (device_id, metric_name) device_id, metric_name, value, "timestamp"
                FROM telemetry WHERE device_id = ANY(:ids) AND "timestamp" > :since
                ORDER BY device_id, metric_name, "timestamp" DESC"""), {"ids": ids, "since": since}):
            latest[(dev_id, metric)] = (value, ts)
        for dev_id, metric, n in db.execute(
                select(Alarm.device_id, Alarm.metric_name, func.count())
                .where(Alarm.device_id.in_(ids), Alarm.cleared_at.is_(None))
                .group_by(Alarm.device_id, Alarm.metric_name)):
            alarms[(dev_id, metric)] = n
    now = datetime.now(timezone.utc)
    fresh = timedelta(minutes=5)
    rows: list[list] = []
    for d in devices:
        base = [d.device_name, d.protocol, d.ip_address, d.port, d.state, d.region, d.city or "", d.plant_name or ""]
        regs = list(d.registers)
        if not regs:
            seen = last_seen.get(d.id)
            n = active.get(d.id, 0)
            status = "Alarm" if n else ("Online" if seen and now - seen < fresh else "Offline")
            rows.append(base + [status, n, local_str(seen) if seen else "", "", "", "", "", "", ""])
        for r in regs:
            value, ts = latest.get((d.id, r.metric_name), ("", None))
            seen = last_seen.get(d.id)  # unchanged readings are not stored: the device was still seen at its last poll
            if ts and seen and seen > ts:
                ts = seen
            n = alarms.get((d.id, r.metric_name), 0)
            status = "Alarm" if n else ("Online" if ts and now - ts < fresh else "Offline")
            rows.append(base + [status, n, local_str(ts) if ts else "", r.metric_name, r.sensor_type or "", r.unit or "", value,
                                "" if r.low_limit is None else r.low_limit, "" if r.high_limit is None else r.high_limit])
    return rows


def build_dashboard_export(rows: list[list], fmt: str, company: str):
    tmp = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    stamp = datetime.now(plant_tz()).strftime("%Y%m%d_%H%M")
    safe = "".join(c if c.isalnum() else "_" for c in company)[:40]
    if fmt == "csv":
        text_tmp = io.TextIOWrapper(tmp, encoding="utf-8-sig", newline="", write_through=True)
        w = csv.writer(text_tmp)
        w.writerow(dash_headers())
        w.writerows(rows)
        text_tmp.detach()
        media, filename = "text/csv", f"{safe}_dashboard_{stamp}.csv"
    elif fmt == "xlsx":
        wb = Workbook(write_only=True)
        ws = wb.create_sheet("Dashboard")
        ws.append(dash_headers())
        for r in rows:
            ws.append(r)
        wb.save(tmp)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"{safe}_dashboard_{stamp}.xlsx"
    else:
        from reportlab.lib.pagesizes import A4, landscape
        c = canvas.Canvas(tmp, pagesize=landscape(A4))
        width, height = landscape(A4)
        cols = [(30, "Device", 0, 16), (115, "State", 4, 10), (175, "City", 6, 10), (235, "Plant name", 7, 14),
                (320, "Status", 8, 7), (365, "Alarms", 9, 5), (395, f"Last seen ({tz_label()})", 10, 16),
                (480, "Metric", 11, 14), (565, "Latest", 14, 9), (625, "Low", 15, 7), (680, "High", 16, 7)]

        def header(page: int):
            c.setFont("Helvetica-Bold", 12)
            c.drawString(30, height - 34, f"STECH iVIEW - Dashboard report: {company}")
            c.setFont("Helvetica", 8)
            c.drawString(30, height - 47, f"Generated {datetime.now(plant_tz()):%Y-%m-%d %H:%M} {tz_label()} | page {page}")
            c.setFont("Helvetica-Bold", 8)
            for x, label, _, _ in cols:
                c.drawString(x, height - 66, label)
            c.setFont("Helvetica", 7.5)
            return height - 80

        page, y = 1, header(1)
        for r in rows:
            if y < 30:
                c.showPage()
                page += 1
                y = header(page)
            for x, _, i, n in cols:
                v = r[i]
                c.drawString(x, y, (f"{v:.3f}" if isinstance(v, float) else str(v))[:n])
            y -= 10.5
        c.save()
        media, filename = "application/pdf", f"{safe}_dashboard_{stamp}.pdf"
    tmp.seek(0)
    return tmp, filename, media
