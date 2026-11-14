"""v1.1 checks: sign-up details, Super admin notice, 2-step login, keep signed in, storage, on-call, alert rules, ERP export, tiles."""
import uuid
from datetime import timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.timeutil import utcnow
from app.main import app
from tests.helpers import PW, STATE, login, make_user, signup_owner



@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def co(client):
    """A new company: its Super admin, one Admin (Regional admin) of State + Region South, and one device with a sensor."""
    name = signup_owner(client)
    su = login(client, name + "_su")
    ra, ra_user = make_user(client, su, name + "_ra", "Admin", phone="+919999999999")
    dev = client.post("/api/v1/devices", headers=su, json={
        "device_name": "Pump 1", "protocol": "Modbus TCP", "ip_address": "10.0.0.5", "port": 502, "state": STATE, "region": "South",
        "city": "Chennai", "plant_name": "Tambaram Pump House",
        "registers": [{"metric_name": "temp", "data_type": "float32", "register_type": "holding", "register_address": 1, "unit": "C"}]})
    assert dev.status_code == 201, dev.text
    return {"name": name, "su": su, "ra": ra, "device_id": dev.json()["id"], "ra_id": ra_user["id"]}


def test_version_and_offline_time(client):
    from app.core.config import settings
    assert settings.GATEWAY_OFFLINE_SECONDS == 10 and settings.DEVICE_STALE_SECONDS == 5
    assert client.get("/api/v1/app-info").json()["version"] == "2.0.0"


def test_keep_signed_in_lasts_30_days(client, co):
    r = client.post("/api/v1/auth/login", json={"username": co["name"] + "_su", "password": PW, "remember": True})
    assert r.json()["expires_in"] == 30 * 24 * 3600
    r = client.post("/api/v1/auth/login", json={"username": co["name"] + "_su", "password": PW})
    assert r.json()["expires_in"] == 30 * 60


def test_two_step_login(client, co):
    h = co["su"]
    setup = client.post("/api/v1/account/2fa/setup", headers=h).json()
    assert setup["uri"].startswith("otpauth://totp/")
    totp = pyotp.TOTP(setup["secret"])
    assert client.post("/api/v1/account/2fa/enable", headers=h, json={"code": "000000"}).status_code == 400
    assert client.post("/api/v1/account/2fa/enable", headers=h, json={"code": totp.now()}).status_code == 200
    r = client.post("/api/v1/auth/login", json={"username": co["name"] + "_su", "password": PW})
    assert r.status_code == 401 and "OTP_REQUIRED" in r.text
    r = client.post("/api/v1/auth/login", json={"username": co["name"] + "_su", "password": PW, "otp": "000000"})
    assert r.status_code == 401
    r = client.post("/api/v1/auth/login", json={"username": co["name"] + "_su", "password": PW, "otp": totp.now()})
    assert r.status_code == 200
    r = client.post("/api/v1/account/2fa/disable", headers=h, json={"password": PW, "code": totp.now()})
    assert r.status_code == 200, r.text
    assert client.post("/api/v1/auth/login", json={"username": co["name"] + "_su", "password": PW}).status_code == 200


def test_profile_phone(client, co):
    r = client.put("/api/v1/account/profile", headers=co["ra"], json={"phone": "+911234567890"})
    assert r.status_code == 200 and r.json()["phone"] == "+911234567890"


def test_storage_usage_keep_shares_and_free_up(client, co):
    su = co["su"]
    r = client.get("/api/v1/retention", headers=su).json()
    assert r["keep_days"] == 0 and r["level"] == "none" and r["used_gb"] >= 0
    assert client.put("/api/v1/retention/keep", headers=su, json={"keep_days": 7}).status_code == 422
    assert client.put("/api/v1/retention/keep", headers=su, json={"keep_days": 365}).json()["keep_days"] == 365
    assert client.put("/api/v1/retention/keep", headers=co["ra"], json={"keep_days": 30}).status_code == 403
    from app.db.session import MasterSession
    from app.services import retention
    with MasterSession() as m:
        retention.policy(m, co["name"], create=True).quota_gb = 0.001
        m.commit()
    assert client.put("/api/v1/retention/shares", headers=su, json={"state": STATE, "region": "South", "share_gb": 0.0005}).status_code == 200
    assert client.put("/api/v1/retention/shares", headers=su, json={"state": STATE, "region": "North", "share_gb": 0.0009}).status_code == 422
    whole = client.get("/api/v1/retention", headers=su).json()
    assert whole["quota_gb"] == 0.001 and whole["level"] in ("ok", "amber", "red") and whole["shares"][0]["share_gb"] == 0.0005
    mine = client.get("/api/v1/retention", headers=co["ra"]).json()
    assert mine["share_gb"] == 0.0005
    assert client.post("/api/v1/retention/free-up", headers=su, json={"older_than_days": 30, "confirm": False}).status_code == 422
    assert client.post("/api/v1/retention/free-up", headers=su, json={"older_than_days": 3, "confirm": True}).status_code == 422


def test_levels():
    from app.services.retention import level
    assert [level(x) for x in (None, 10, 79.9, 80, 89.9, 90, 100)] == ["none", "ok", "ok", "amber", "amber", "red", "red"]


def _put_readings(company, device_id, rows):
    from app.db.session import MasterSession, tenant_session
    from app.models.tenant import Telemetry
    with MasterSession() as m, tenant_session(m, company) as db:
        for ts, v in rows:
            db.add(Telemetry(timestamp=ts, device_id=device_id, metric_name="temp", value=v))
        db.commit()


def test_free_up_and_nightly_purge_and_erp(client, co):
    now = utcnow()
    _put_readings(co["name"], co["device_id"], [(now - timedelta(days=400), 10.0), (now - timedelta(days=40), 20.0),
                                                (now - timedelta(days=1, hours=1), 30.0), (now - timedelta(days=1, hours=2), 50.0)])
    erp = client.get("/api/v1/reports/erp", headers=co["ra"], params={"format": "csv", "date_from": (now - timedelta(days=3)).isoformat(),
                                                                     "date_to": now.isoformat()})
    assert erp.status_code == 200, erp.text
    body = erp.content.decode("utf-8-sig").splitlines()
    assert body[0].startswith("Company,Date,State,Region,City,Plant,Device ID,Device,Sensor,Unit,Minimum,Average,Maximum,Last value,Readings")
    assert len(body) == 2 and "Tambaram Pump House" in body[1]
    cols = body[1].split(",")
    assert [float(cols[10]), float(cols[11]), float(cols[12]), int(cols[14])] == [30.0, 40.0, 50.0, 2]
    assert cols[0] == co["name"] and int(cols[6]) == co["device_id"]
    for fmt in ("xlsx", "pdf"):
        r = client.get("/api/v1/reports/erp", headers=co["su"], params={"format": fmt, "date_from": (now - timedelta(days=3)).isoformat(), "date_to": now.isoformat()})
        assert r.status_code == 200 and len(r.content) > 500
    assert client.get("/api/v1/reports/erp", headers=co["su"], params={"format": "csv", "date_from": (now - timedelta(days=300)).isoformat(), "date_to": now.isoformat()}).status_code == 413
    # export before freeing: ONE file with everything older than 30 days (the 400-day and 40-day readings)
    ex = client.get("/api/v1/retention/export-before", headers=co["su"], params={"older_than_days": 30, "format": "csv"})
    assert ex.status_code == 200, ex.text
    assert len(ex.content.decode("utf-8-sig").splitlines()) == 3                           # header + two days
    assert client.get("/api/v1/retention/export-before", headers=co["su"], params={"older_than_days": 3}).status_code == 422
    # free up space (Super admin confirms): the 400-day and 40-day readings go, the recent ones stay
    r = client.post("/api/v1/retention/free-up", headers=co["su"], json={"older_than_days": 30, "confirm": True})
    assert r.json()["deleted_rows"] == 2
    # nightly clean-up honours "keep for"
    _put_readings(co["name"], co["device_id"], [(now - timedelta(days=500), 1.0)])
    from app.db.session import MasterSession, tenant_session
    from app.services import retention
    with MasterSession() as m, tenant_session(m, co["name"]) as db:
        from app.core.config import settings
        assert retention.nightly(m, co["name"], db, (settings.PURGE_HOUR + 1) % 24) == 0       # wrong hour: nothing
        assert retention.nightly(m, co["name"], db, settings.PURGE_HOUR) == 1                  # keep 365 days: the 500-day one goes
        assert retention.nightly(m, co["name"], db, settings.PURGE_HOUR) == 0                  # only once a day


def test_tiles(client, co):
    t = client.get("/api/v1/tiles", headers=co["ra"]).json()
    assert len(t) == 1 and t[0]["region"] == "South" and t[0]["devices"] == 1 and t[0]["plants"] == ["Tambaram Pump House"]


def test_alert_rule_raises_and_clears(client, co):
    h = co["su"]
    r = client.post("/api/v1/alert-rules", headers=h, json={"device_id": co["device_id"], "metric_name": "temp", "op": ">", "threshold": 80,
                                                           "for_seconds": 2, "severity": "critical", "message": "Pump too hot"})
    assert r.status_code == 201, r.text
    assert client.post("/api/v1/alert-rules", headers=h, json={"device_id": co["device_id"], "metric_name": "nope", "op": ">", "threshold": 1}).status_code == 422
    from app.db.session import MasterSession, tenant_session
    from app.models.tenant import Alarm, Device
    from app.services import alarms
    t0 = utcnow()
    with MasterSession() as m, tenant_session(m, co["name"]) as db:
        dev = db.get(Device, co["device_id"])
        alarms.evaluate(db, m, dev, {"temp": (90.0, t0)})
        assert not db.scalars(select(Alarm)).all()                                         # breach just started: waits 2 s
        alarms.evaluate(db, m, dev, {"temp": (91.0, t0 + timedelta(seconds=3))})
        a = db.scalars(select(Alarm)).all()
        assert len(a) == 1 and a[0].severity == "critical" and "Pump too hot" in a[0].message
        alarms.evaluate(db, m, dev, {"temp": (92.0, t0 + timedelta(seconds=4))})
        assert len(db.scalars(select(Alarm)).all()) == 1                                   # not raised twice
        alarms.evaluate(db, m, dev, {"temp": (50.0, t0 + timedelta(seconds=5))})
        db.expire_all()
        assert db.scalars(select(Alarm)).all()[0].cleared_at is not None


def test_on_call_roster_steps(client, co):
    su, ra = co["su"], co["ra"]
    body = {"steps": [{"user_id": co["ra_id"], "wait_minutes": 10}]}
    assert client.put("/api/v1/oncall", headers=su, json=body).status_code == 422          # Super admin must name a region
    assert client.put("/api/v1/oncall", headers=su, json={**body, "state": STATE, "region": "South"}).status_code == 200
    assert client.put("/api/v1/oncall", headers=ra, json={"steps": [{"user_id": 999999, "wait_minutes": 5}]}).status_code == 422
    got = client.get("/api/v1/oncall", headers=ra).json()
    assert got["steps"][0]["username"] == co["name"] + "_ra"
    from app.db.session import MasterSession, tenant_session
    from app.models.tenant import Alarm, Notification
    from app.services import oncall
    with MasterSession() as m, tenant_session(m, co["name"]) as db:
        a = Alarm(device_id=co["device_id"], metric_name="temp", severity="major", message="test", raised_at=utcnow() - timedelta(minutes=1))
        db.add(a)
        db.commit()
        oncall.run(m, db, co["name"], [a])
        oncall.run(m, db, co["name"], [a])                                                  # second run must not repeat
        rows = db.scalars(select(Notification).where(Notification.channel == "oncall1")).all()
        assert len(rows) == 1 and rows[0].user_id == co["ra_id"]


def test_report_schedule(client, co):
    su = co["su"]
    body = {"name": "Morning", "kind": "erp", "fmt": "xlsx", "frequency": "daily", "hour": 8, "recipients": "a@example.com, b@example.com"}
    r = client.post("/api/v1/reports/schedules", headers=su, json=body)
    assert r.status_code == 201, r.text
    assert client.post("/api/v1/reports/schedules", headers=su, json={**body, "recipients": "nope"}).status_code == 422
    assert client.post("/api/v1/reports/schedules", headers=co["ra"], json=body).status_code == 403
    assert len(client.get("/api/v1/reports/schedules", headers=su).json()) == 1
    from app.db.session import MasterSession
    from app.models.master import ReportSchedule
    from app.services import jobs
    with MasterSession() as m:
        s = m.get(ReportSchedule, r.json()["id"])
        local = utcnow().replace(hour=8, minute=0)
        assert jobs._is_due(s, local.replace(tzinfo=None) if False else local)
        assert not jobs._is_due(s, local.replace(hour=9))
        tmp, filename, media = jobs._build(s, __import__("app.db.session", fromlist=["x"]).open_tenant_session(m, co["name"]))
        assert filename.endswith(".xlsx") and tmp.read(2) == b"PK"
    assert client.delete(f"/api/v1/reports/schedules/{r.json()['id']}", headers=su).status_code == 200


def test_sms_off_by_default():
    from app.core.config import settings
    assert settings.SMS_PROVIDER == ""


def _operator(client, co):
    """An Operator of the same State + Region (made once per company)."""
    if "op" not in co:
        co["op"], _ = make_user(client, co["su"], co["name"] + "_op", "Operator")
    return co["op"]


def test_operator_cannot_edit_devices_admin_can(client, co):
    op = _operator(client, co)
    did = co["device_id"]
    # an Operator has no Read and Write on Configuration: refused, nothing changes
    assert client.put(f"/api/v1/devices/{did}", headers=op, json={"ip_address": "10.0.0.77"}).status_code == 403
    assert client.get(f"/api/v1/devices/{did}", headers=co["su"]).json()["ip_address"] == "10.0.0.5"
    assert client.get("/api/v1/changes", headers=co["su"]).status_code == 404
    # an administrator's own edit applies at once; a bad value is refused
    assert client.put(f"/api/v1/devices/{did}", headers=co["ra"], json={"device_name": "Pump 2"}).status_code == 200
    assert client.put(f"/api/v1/devices/{did}", headers=co["ra"], json={"protocol": "Modbus RTU", "ip_address": "not a port"}).status_code == 422


def test_guest_and_removed_hub_routes(client, co):
    for path in ("/api/v1/regions", "/api/v1/peer/live"):
        assert client.get(path, headers=co["su"]).status_code == 404


def test_company_list_is_gone(client, co):
    assert client.get("/api/v1/auth/companies", params={"state": STATE}).status_code == 404


def test_edit_serial_and_word_order_settings(client, co):
    d = client.post("/api/v1/devices", headers=co["su"], json={
        "device_name": "Meter 7", "protocol": "Modbus TCP", "ip_address": "10.0.0.9", "port": 502, "state": STATE, "region": "South",
        "registers": [{"metric_name": "kw", "data_type": "float32", "register_type": "holding", "register_address": 5, "unit": "kW"}]}).json()
    r = client.put(f"/api/v1/devices/{d['id']}", headers=co["su"], json={"byte_order": "little", "word_order": "big", "address_base": 1})
    assert r.status_code == 200 and (r.json()["byte_order"], r.json()["address_base"]) == ("little", 1)
    r = client.put(f"/api/v1/devices/{d['id']}", headers=co["su"], json={"protocol": "Modbus RTU", "ip_address": "COM3", "unit_id": 3, "serial_baud": 19200, "serial_parity": "E"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert (got["protocol"], got["ip_address"], got["port"], got["serial_baud"], got["serial_parity"]) == ("Modbus RTU", "COM3", 0, 19200, "E")
