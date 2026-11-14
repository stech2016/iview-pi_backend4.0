"""STGW4.0 - readings that arrive late (the gateway was cut off) are recorded in the alarm list but page nobody."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.timeutil import utcnow
from app.db.session import MasterSession, tenant_session
from app.main import app
from app.models.tenant import Alarm, Device, Notification
from app.services import ingest
from tests.helpers import API, STATE, login, signup_owner


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _co(client):
    name = signup_owner(client)
    su = login(client, name + "_su")
    d = client.post(f"{API}/devices", headers=su, json={
        "device_name": "Boiler", "protocol": "Modbus TCP", "ip_address": "10.0.0.9", "port": 502, "unit_id": 1, "state": STATE, "region": "South",
        "registers": [{"metric_name": "temp", "data_type": "float32", "register_type": "holding", "register_address": 12, "unit": "C", "high_limit": 80, "low_limit": 10},
                      {"metric_name": "flow", "data_type": "float32", "register_type": "holding", "register_address": 14}]})
    assert d.status_code == 201, d.text
    return name, d.json()["id"]


def _send(name, dev, readings):
    with MasterSession() as m, tenant_session(m, name) as db:
        ingest.ingest_readings(db, m, db.get(Device, dev), readings)


def _alarms(name, dev):
    with MasterSession() as m, tenant_session(m, name) as db:
        out = []
        for a in db.scalars(select(Alarm).where(Alarm.device_id == dev).order_by(Alarm.raised_at)):
            n = len(db.scalars(select(Notification).where(Notification.alarm_id == a.id)).all())
            out.append((a.metric_name, a.message, a.raised_at, a.cleared_at, n))
        return out


def test_a_breach_that_is_over_is_recorded_but_nobody_is_paged(client):
    name, dev = _co(client)
    t = utcnow()
    _send(name, dev, [("temp", 70.0, t - timedelta(minutes=60)), ("temp", 95.0, t - timedelta(minutes=50)), ("temp", 99.0, t - timedelta(minutes=40)),
                      ("temp", 70.0, t - timedelta(minutes=30))])
    got = _alarms(name, dev)
    assert len(got) == 1
    metric, msg, raised, cleared, notified = got[0]
    assert metric == "temp" and "received late" in msg and "exceeds limit" in msg
    assert abs((raised - (t - timedelta(minutes=50))).total_seconds()) < 1 and abs((cleared - (t - timedelta(minutes=30))).total_seconds()) < 1
    assert notified == 0


def test_a_low_breach_is_recorded_too_and_a_resend_adds_nothing(client):
    name, dev = _co(client)
    t = utcnow()
    batch = [("temp", 50.0, t - timedelta(minutes=90)), ("temp", 5.0, t - timedelta(minutes=80)), ("temp", 50.0, t - timedelta(minutes=70))]
    _send(name, dev, batch)
    _send(name, dev, batch)
    got = _alarms(name, dev)
    assert len(got) == 1 and "below limit" in got[0][1] and got[0][4] == 0


def test_late_readings_never_open_a_live_alarm(client):
    name, dev = _co(client)
    t = utcnow()
    _send(name, dev, [("temp", 99.0, t - timedelta(minutes=20)), ("temp", 99.5, t - timedelta(minutes=10))])      # still above the limit at the last late reading
    got = _alarms(name, dev)
    assert len(got) == 1 and got[0][3] is not None and got[0][4] == 0                                             # closed entry, nobody told


def test_a_fresh_breach_still_pages_and_stays_open(client):
    name, dev = _co(client)
    _send(name, dev, [("temp", 95.0, utcnow())])
    got = _alarms(name, dev)
    assert len(got) == 1 and got[0][3] is None and "received late" not in got[0][1] and got[0][4] >= 1


def test_catch_up_batch_late_history_plus_fresh_reading(client):
    name, dev = _co(client)
    t = utcnow()
    _send(name, dev, [("temp", 95.0, t - timedelta(minutes=45)), ("temp", 60.0, t - timedelta(minutes=35)), ("temp", 60.0, t)])
    got = _alarms(name, dev)
    assert len(got) == 1 and "received late" in got[0][1] and got[0][3] is not None                              # the past breach is on record, no live alarm
    # and when the sensor is still bad now, the live alarm opens by the normal path in addition
    _send(name, dev, [("temp", 97.0, t - timedelta(minutes=2)), ("temp", 98.0, utcnow())])
    got = _alarms(name, dev)
    assert any(a[3] is None and a[4] >= 1 for a in got)


def test_a_late_batch_cannot_close_or_reopen_a_live_alarm(client):
    name, dev = _co(client)
    t = utcnow()
    _send(name, dev, [("temp", 95.0, t)])                                                                         # live alarm open now
    _send(name, dev, [("temp", 50.0, t - timedelta(minutes=30))])                                                 # an old normal reading arrives afterwards
    live = [a for a in _alarms(name, dev) if a[3] is None]
    assert len(live) == 1                                                                                         # still open: an old reading says nothing about now


def test_sensors_without_limits_and_normal_late_data_write_nothing(client):
    name, dev = _co(client)
    t = utcnow()
    _send(name, dev, [("flow", 1e9, t - timedelta(minutes=60)), ("temp", 50.0, t - timedelta(minutes=60)), ("temp", 60.0, t - timedelta(minutes=50))])
    assert _alarms(name, dev) == []
