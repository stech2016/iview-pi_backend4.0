"""On-call roster per State + Region. Step 1 is told as soon as an alarm is not acknowledged, then step 2 after step 1's wait, and so on.
Called every minute from the escalation check. Each person is told once per alarm (a 'oncall<step>' row in notifications)."""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.timeutil import utcnow
from app.models.master import OnCallStep
from app.models.tenant import Alarm, Device, Notification
from app.services import notify

log = get_logger("oncall")


def run(master: Session, db: Session, company: str, open_alarms: list[Alarm]) -> None:
    steps_by_region: dict[tuple[str, str], list[OnCallStep]] = {}
    for s in master.scalars(select(OnCallStep).where(OnCallStep.company_name == company).order_by(OnCallStep.step_no)):
        steps_by_region.setdefault((s.state, s.region), []).append(s)
    if not steps_by_region:
        return
    now = utcnow()
    for a in open_alarms:
        dev = db.get(Device, a.device_id)
        steps = steps_by_region.get((dev.state, dev.region)) if dev else None
        if not steps:
            continue
        due_at = a.raised_at
        for s in steps:
            if due_at > now:
                break
            channel = f"oncall{s.step_no}"
            if not db.scalar(select(Notification.id).where(Notification.alarm_id == a.id, Notification.channel == channel).limit(1)):
                db.add(Notification(user_id=s.user_id, alarm_id=a.id, channel=channel, sent_at=now))
                db.commit()
                notify.tell(master, company, [s.user_id], f"ON-CALL step {s.step_no} - {a.severity.upper()} alarm",
                            f"{dev.device_name}: {a.message}", {"alarm_id": a.id, "severity": a.severity}, a.severity)
                log.info("oncall_notified", company=company, alarm_id=a.id, step=s.step_no)
            due_at = due_at + timedelta(minutes=s.wait_minutes)
