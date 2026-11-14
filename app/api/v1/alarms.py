from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.timeutil import utcnow
from app.db.session import get_master_db
from app.deps import client_ip, get_company_db, get_current_user, require_roles, scope_devices
from app.models.master import AlarmSettings, User
from app.models.tenant import Alarm, AlarmComment, AlarmShelf, Device, FcmToken, Notification
from app.schemas.device import AlarmOut, FcmIn, NotificationOut
from app.schemas.auth import MessageOut
from app.services import alarms as alarm_svc, audit, csettings, live, perms, sync

router = APIRouter(tags=["alarms"])


def _decorate(db: Session, rows: list[tuple]) -> list[AlarmOut]:
    """rows = [(alarm, device)]: adds 'shelved' / 'maintenance' and the number of comments to every alarm."""
    ids = [a.id for a, _ in rows]
    counts = dict(db.execute(select(AlarmComment.alarm_id, func.count()).where(AlarmComment.alarm_id.in_(ids)).group_by(AlarmComment.alarm_id)).all()) if ids else {}
    shelves: dict[tuple, AlarmShelf] = {}
    dev_ids = {d.id for _, d in rows}
    if dev_ids:
        for sh in db.scalars(select(AlarmShelf).where(AlarmShelf.device_id.in_(dev_ids), AlarmShelf.until > utcnow())):
            shelves[(sh.device_id, sh.metric_name)] = sh
    out, now = [], utcnow()
    for a, d in rows:
        extra: dict = {"comment_count": counts.get(a.id, 0)}
        if a.cleared_at is None:
            sh = shelves.get((a.device_id, a.metric_name or ""))
            if sh:
                extra.update(muted="shelved", shelved_until=sh.until.isoformat(), shelved_by=sh.shelved_by, shelve_reason=sh.reason)
            elif d.maintenance_until and d.maintenance_until > now:
                extra["muted"] = "maintenance"
        out.append(AlarmOut(**alarm_svc.alarm_dict(a, d.device_name, alarm_svc.place_text(d.city, d.plant_name), **extra)))
    return out


@router.get("/alarms", response_model=list[AlarmOut])
def list_alarms(active_only: bool = False, limit: int = 200, user: User = Depends(perms.require("alarms", 1)), db: Session = Depends(get_company_db)):
    stmt = scope_devices(
        select(Alarm, Device).join(Device, Device.id == Alarm.device_id), user
    ).order_by(Alarm.raised_at.desc()).limit(min(max(limit, 1), 500))
    if active_only:
        stmt = stmt.where(Alarm.cleared_at.is_(None))
    return _decorate(db, [(a, d) for a, d in db.execute(stmt).all()])


@router.post("/alarms/{alarm_id}/acknowledge", response_model=AlarmOut)
def acknowledge(alarm_id: int, request: Request, user: User = Depends(perms.require("alarms", 2)), db: Session = Depends(get_company_db)):
    row = db.execute(scope_devices(
        select(Alarm, Device).join(Device, Device.id == Alarm.device_id).where(Alarm.id == alarm_id), user)).first()
    if not row:
        raise AppError(404, "ALARM_NOT_FOUND", "Alarm not found or outside your assigned state/region.")
    alarm, device = row
    if alarm.acknowledged_by:
        raise AppError(409, "ALREADY_ACKNOWLEDGED", f"This alarm was already acknowledged by {alarm.acknowledged_by}.")
    alarm.acknowledged_by, alarm.acknowledged_at = user.username, utcnow()
    db.commit()
    sync.mirror(db, "alarms", [alarm])
    audit.append(db, user.id, user.company_name, "alarm.acknowledge", f"alarm:{alarm.id}", client_ip(request))
    live.publish(user.company_name, {"type": "alarm", "event": "acknowledged",
                                     "alarm": alarm_svc.alarm_dict(alarm, device.device_name, alarm_svc.place_text(device.city, device.plant_name)),
                                     "state": device.state, "region": device.region})
    return _decorate(db, [(alarm, device)])[0]


def _alarm_for(db: Session, user: User, alarm_id: int) -> tuple[Alarm, Device]:
    row = db.execute(scope_devices(select(Alarm, Device).join(Device, Device.id == Alarm.device_id).where(Alarm.id == alarm_id), user)).first()
    if not row:
        raise AppError(404, "ALARM_NOT_FOUND", "Alarm not found or outside your assigned state/region.")
    return row[0], row[1]


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


def _comment_out(c: AlarmComment) -> dict:
    return {"id": c.id, "alarm_id": c.alarm_id, "username": c.username, "text": c.text, "created_at": c.created_at.isoformat()}


@router.get("/alarms/{alarm_id}/comments")
def alarm_comments(alarm_id: int, user: User = Depends(perms.require("alarms", 1)), db: Session = Depends(get_company_db)):
    alarm, _ = _alarm_for(db, user, alarm_id)
    return [_comment_out(c) for c in db.scalars(select(AlarmComment).where(AlarmComment.alarm_id == alarm.id).order_by(AlarmComment.id))]


@router.post("/alarms/{alarm_id}/comments", status_code=201)
def add_alarm_comment(alarm_id: int, body: CommentIn, request: Request, user: User = Depends(perms.require("alarms", 2)),
                      db: Session = Depends(get_company_db)):
    """A note on an alarm ("inlet valve closed by hand"). It stays with the alarm for ever."""
    alarm, device = _alarm_for(db, user, alarm_id)
    c = AlarmComment(alarm_id=alarm.id, user_id=user.id, username=user.username, text=body.text.strip())
    db.add(c)
    db.commit()
    audit.append(db, user.id, user.company_name, "alarm.comment", f"alarm:{alarm.id}", client_ip(request))
    live.publish(user.company_name, {"type": "alarm", "event": "commented", "alarm": _decorate(db, [(alarm, device)])[0].model_dump(mode="json"),
                                     "state": device.state, "region": device.region})
    return _comment_out(c)


class ShelveIn(BaseModel):
    minutes: int = Field(ge=5, le=10080)
    reason: str = Field(min_length=3, max_length=200)


@router.post("/alarms/{alarm_id}/shelve", response_model=AlarmOut)
def shelve(alarm_id: int, body: ShelveIn, request: Request, user: User = Depends(perms.require("alarms", 2)), db: Session = Depends(get_company_db)):
    """Put the alarm of this sensor aside until a time, with a reason. Nobody is told about it meanwhile; it ends by itself."""
    alarm, device = _alarm_for(db, user, alarm_id)
    limit = csettings.get(user.company_name, "shelve_max_minutes")
    if body.minutes > limit:
        raise AppError(422, "SHELVE_TOO_LONG", f"An alarm can be shelved for at most {limit} minutes (a Super admin can change this in Settings).")
    metric = alarm.metric_name or ""
    until = utcnow() + timedelta(minutes=body.minutes)
    sh = db.scalar(select(AlarmShelf).where(AlarmShelf.device_id == device.id, AlarmShelf.metric_name == metric))
    if sh:
        sh.until, sh.reason, sh.shelved_by, sh.created_at = until, body.reason.strip(), user.username, utcnow()
    else:
        db.add(AlarmShelf(device_id=device.id, metric_name=metric, until=until, reason=body.reason.strip(), shelved_by=user.username))
    db.commit()
    audit.append(db, user.id, user.company_name, "alarm.shelve", f"alarm:{alarm.id}:{body.minutes}min:{body.reason.strip()[:60]}", client_ip(request))
    out = _decorate(db, [(alarm, device)])[0]
    live.publish(user.company_name, {"type": "alarm", "event": "shelved", "alarm": out.model_dump(mode="json"), "state": device.state, "region": device.region})
    return out


@router.delete("/alarms/{alarm_id}/shelve", response_model=AlarmOut)
def unshelve(alarm_id: int, request: Request, user: User = Depends(perms.require("alarms", 2)), db: Session = Depends(get_company_db)):
    alarm, device = _alarm_for(db, user, alarm_id)
    sh = db.scalar(select(AlarmShelf).where(AlarmShelf.device_id == device.id, AlarmShelf.metric_name == (alarm.metric_name or "")))
    if sh:
        db.delete(sh)
        db.commit()
        audit.append(db, user.id, user.company_name, "alarm.unshelve", f"alarm:{alarm.id}", client_ip(request))
    out = _decorate(db, [(alarm, device)])[0]
    live.publish(user.company_name, {"type": "alarm", "event": "unshelved", "alarm": out.model_dump(mode="json"), "state": device.state, "region": device.region})
    return out


class EscalationIn(BaseModel):
    """"Not acknowledged in time": minutes a critical / major alarm may wait before the admins are told again.
    0 = never escalate. Minor and advisory alarms are never escalated (the fields stay only for older apps)."""
    critical: int = Field(ge=0, le=1440)
    major: int = Field(ge=0, le=1440)
    minor: int = Field(default=0, ge=0, le=0)
    advisory: int = Field(default=0, ge=0, le=0)


@router.get("/alarm-settings", response_model=EscalationIn)
def get_alarm_settings(user: User = Depends(get_current_user), master: Session = Depends(get_master_db)):
    return EscalationIn(**alarm_svc.escalation_minutes(master, user.company_name))


@router.put("/alarm-settings", response_model=EscalationIn)
def save_alarm_settings(body: EscalationIn, request: Request, user: User = Depends(require_roles("admin")),
                        master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    row = master.scalar(select(AlarmSettings).where(AlarmSettings.company_name == user.company_name))
    if not row:
        row = AlarmSettings(company_name=user.company_name)
        master.add(row)
    row.critical, row.major, row.minor, row.advisory = body.critical, body.major, 0, 0
    row.updated_by = user.username
    master.commit()
    audit.append(db, user.id, user.company_name, "alarm_settings.update",
                 f"not_acknowledged_in_time:critical{body.critical}/major{body.major}", client_ip(request))
    return body


@router.get("/notifications", response_model=list[NotificationOut])
def notifications(unread_only: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_company_db)):
    stmt = select(Notification).where(Notification.user_id == user.id).order_by(Notification.sent_at.desc()).limit(100)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    return list(db.scalars(stmt))


@router.post("/notifications/{notification_id}/read", response_model=MessageOut)
def mark_read(notification_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_company_db)):
    n = db.scalar(select(Notification).where(Notification.id == notification_id, Notification.user_id == user.id))
    if not n:
        raise AppError(404, "NOTIFICATION_NOT_FOUND", "Notification not found.")
    n.read_at = utcnow()
    db.commit()
    return MessageOut(message="Marked as read.")


@router.post("/notifications/fcm-token", response_model=MessageOut)
def register_fcm(body: FcmIn, user: User = Depends(get_current_user), db: Session = Depends(get_company_db)):
    row = db.scalar(select(FcmToken).where(FcmToken.token == body.token))
    if row:
        row.user_id, row.device_info, row.updated_at = user.id, body.device_info, utcnow()
    else:
        db.add(FcmToken(user_id=user.id, token=body.token, device_info=body.device_info))
    db.commit()
    return MessageOut(message="Push token registered.")


@router.delete("/notifications/fcm-token", response_model=MessageOut)
def forget_fcm(token: str, user: User = Depends(get_current_user), db: Session = Depends(get_company_db)):
    """Called when the person logs out on a phone, so that phone stops receiving this account's alarms."""
    row = db.scalar(select(FcmToken).where(FcmToken.token == token, FcmToken.user_id == user.id))
    if row:
        db.delete(row)
        db.commit()
    return MessageOut(message="Push token removed.")
