"""Alert rules. Four kinds, each one small function in HANDLERS (add a kind = add one function + one line there):

  level     "temperature > 80 for 30 s"; optional deadband: the alarm clears only after the value is back by that margin.
  rate      "pressure rises by more than 2 bar within 60 s" (op / threshold apply to the CHANGE over window_seconds).
  deadband  same as level but a deadband is required (stops an alarm flickering on/off around the limit).
  no_data   the device has not answered for window_seconds (checked once a minute by check_silence, not by readings).

Checked each time a reading arrives (called from alarms.evaluate). A rule's alarm has its own key (metric_name =
'<metric> [rule <id>]') so it never mixes with the sensor's low / high limit alarm."""
import operator
from collections import deque
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.models.tenant import Alarm, AlertRule, Device

OPS = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le}
KINDS = ("level", "rate", "deadband", "no_data")
_since: dict[tuple[str, int, int], datetime] = {}                 # (company, rule id, device id) -> when the breach started (this process)
_history: dict[tuple[str, int, int], deque] = {}                  # (company, rule id, device id) -> recent (time, value) for rate rules


def key(rule: AlertRule) -> str:
    return f"{rule.metric_name} [rule {rule.id}]"[:64]


def _still_breached(rule: AlertRule, value: float) -> bool:
    """While an alarm is active, a deadband keeps it active until the value is back past threshold -/+ deadband."""
    if not rule.deadband:
        return OPS[rule.op](value, rule.threshold)
    edge = rule.threshold - rule.deadband if rule.op in (">", ">=") else rule.threshold + rule.deadband
    return not (value < edge if rule.op in (">", ">=") else value > edge)


def _level(rule, value, ts, active, k):
    breach = _still_breached(rule, value) if active else OPS[rule.op](value, rule.threshold)
    return breach, f"{rule.metric_name} {rule.op} {rule.threshold:g}", value


def _rate(rule, value, ts, active, k):
    hist = _history.setdefault(k, deque())
    hist.append((ts, value))
    while len(hist) > 1 and (ts - hist[1][0]).total_seconds() >= rule.window_seconds:
        hist.popleft()                                         # keep one point at or before the window start
    change = value - hist[0][1]
    return OPS[rule.op](change, rule.threshold), f"{rule.metric_name} changed {rule.op} {rule.threshold:g} within {rule.window_seconds} s", change


HANDLERS = {"level": _level, "deadband": _level, "rate": _rate}


def check(db: Session, device: Device, latest: dict[str, tuple[float, datetime]], open_alarms: dict | None = None, rules: list | None = None) -> tuple[list[Alarm], list[Alarm]]:
    """Returns (alarms raised now, alarms cleared now). The caller commits and notifies."""
    if rules is None:
        rules = db.scalars(select(AlertRule).where(AlertRule.device_id == device.id, AlertRule.enabled.is_(True))).all()
    raised, cleared = [], []
    for rule in rules:
        handler = HANDLERS.get(rule.kind or "level")
        if handler is None or rule.metric_name not in latest:
            continue
        value, ts = latest[rule.metric_name]
        k = (device.company_name, rule.id, device.id)   # company too: every company has its own numbering
        active = open_alarms.get(key(rule)) if open_alarms is not None else db.scalar(select(Alarm).where(Alarm.device_id == device.id, Alarm.metric_name == key(rule), Alarm.cleared_at.is_(None)))
        breach, text, shown = handler(rule, value, ts, active, k)
        if breach:
            start = _since.setdefault(k, ts)
            if not active and (ts - start).total_seconds() >= rule.for_seconds:
                a = Alarm(device_id=device.id, metric_name=key(rule), severity=rule.severity,
                          message=f"{rule.message or text} (now {shown:g})", raised_at=ts)
                db.add(a)
                db.flush()
                raised.append(a)
        else:
            _since.pop(k, None)
            if active:
                active.cleared_at = ts
                cleared.append(active)
    return raised, cleared


def check_silence(master, company: str, db: Session) -> None:
    """no_data rules: raise when the device has not answered for window_seconds, clear when it answers again. Called once a minute (jobs.py)."""
    from app.services import alarms as alarm_svc
    from app.services import lastseen, live
    rules = list(db.scalars(select(AlertRule).where(AlertRule.kind == "no_data", AlertRule.enabled.is_(True))))
    if not rules:
        return
    seen, now = lastseen.get_all(company), utcnow()
    for rule in rules:
        d = db.get(Device, rule.device_id)
        last = seen.get(rule.device_id)
        if d is None or last is None:
            continue                                           # never answered yet: the offline status already says so
        silent = (now - last).total_seconds()
        active = db.scalar(select(Alarm).where(Alarm.device_id == d.id, Alarm.metric_name == key(rule), Alarm.cleared_at.is_(None)))
        if silent >= rule.window_seconds and not active:
            a = Alarm(device_id=d.id, metric_name=key(rule), severity=rule.severity,
                      message=rule.message or f"No data from {d.device_name} for {int(silent // 60)} min", raised_at=now)
            db.add(a)
            db.flush()
            for uid in alarm_svc.recipient_ids(master, d):
                from app.models.tenant import Notification
                db.add(Notification(user_id=uid, alarm_id=a.id, channel="in_app", sent_at=now))
            db.commit()
            live.publish(company, {"type": "alarm", "event": "raised", "alarm": alarm_svc.alarm_dict(a, d.device_name, alarm_svc.place_text(d.city, d.plant_name)),
                                   "state": d.state, "region": d.region})
        elif silent < rule.window_seconds and active:
            active.cleared_at = now
            db.commit()
            live.publish(company, {"type": "alarm", "event": "cleared", "alarm": alarm_svc.alarm_dict(active, d.device_name, alarm_svc.place_text(d.city, d.plant_name)),
                                   "state": d.state, "region": d.region})
