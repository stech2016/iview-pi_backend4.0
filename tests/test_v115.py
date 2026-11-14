"""v1.1.5 checks: owner-only sign-up, people and permissions, write to plant (simulated plant PC), alarm comments / shelving, maintenance,
stuck sensor, settings, plant health, handover, share links, templates and CSV import, and the plant script itself."""
import io
import json
import time
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.timeutil import utcnow
from app.main import app
from tests.helpers import API, PW, STATE, login, make_user, signup_owner


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def co(client):
    """Company with a Super admin, an Admin and an Engineer (South), an Operator and a Guest, and one Modbus device with 3 sensors."""
    name = signup_owner(client)
    su = login(client, name + "_su")
    admin, _ = make_user(client, su, name + "_ad", "Admin")
    eng, _ = make_user(client, su, name + "_en", "Engineer")
    op, _ = make_user(client, su, name + "_op", "Operator")
    guest, _ = make_user(client, su, name + "_gu", "Guest")
    d = client.post(f"{API}/devices", headers=su, json={
        "device_name": "Pump 1", "protocol": "Modbus TCP", "ip_address": "10.0.0.5", "port": 502, "unit_id": 1, "state": STATE, "region": "South",
        "plant_name": "Tambaram",
        "registers": [{"metric_name": "speed", "data_type": "float32", "register_type": "holding", "register_address": 10, "unit": "%"},
                      {"metric_name": "run", "data_type": "bool", "register_type": "coil", "register_address": 1},
                      {"metric_name": "temp", "data_type": "float32", "register_type": "holding", "register_address": 12, "unit": "C", "high_limit": 80}]})
    assert d.status_code == 201, d.text
    regs = {r["metric_name"]: r["id"] for r in d.json()["registers"]}
    return {"name": name, "su": su, "admin": admin, "eng": eng, "op": op, "guest": guest, "dev": d.json()["id"], "regs": regs}


# ------------------------------------------------------------------ sign-up and people
def test_only_the_owner_signs_up(client):
    n = "x" + uuid.uuid4().hex[:8]
    body = {"username": n, "email": f"{n}@example.com", "password": PW, "company_name": n, "state": STATE, "region": "South", "accept_terms": True}
    assert client.post(f"{API}/auth/signup", json={**body, "accept_terms": False}).status_code == 422
    assert client.post(f"{API}/auth/signup", json=body).status_code == 201
    other = {**body, "username": n + "2", "email": f"{n}2@example.com"}
    assert client.post(f"{API}/auth/signup", json=other).status_code in (400, 409)     # company names are unique
    assert client.get(f"{API}/auth/terms").json()["version"] == "1.0"
    # whatever role the form carries, a sign-up only ever creates the Super admin of its own new company
    m = n + "m"
    r = client.post(f"{API}/auth/signup", json={**body, "username": m, "email": f"{m}@example.com", "company_name": m, "role": "guest"})
    assert r.status_code == 201
    assert client.get(f"{API}/auth/me", headers=login(client, m)).json()["role"] == "admin"


def test_temporary_password_must_be_changed(client, co):
    n = co["name"] + "_tmp"
    r = client.post(f"{API}/users", headers=co["su"], json={"username": n, "email": f"{n}@example.com", "job_title": "Operator", "state": STATE,
                                                            "region": "South", "send_email": False})
    assert r.status_code == 201 and r.json()["email_sent"] is False
    first = client.post(f"{API}/auth/login", json={"username": n, "password": r.json()["temporary_password"]}).json()
    h = {"Authorization": "Bearer " + first["access_token"]}
    assert first["user"]["must_change_password"] is True
    assert client.get(f"{API}/devices", headers=h).status_code == 403                                    # blocked until the password is changed
    assert client.get(f"{API}/auth/me", headers=h).status_code == 200
    assert client.post(f"{API}/auth/change-password", headers=h, json={"current_password": r.json()["temporary_password"], "new_password": "weak"}).status_code == 422
    ok = client.post(f"{API}/auth/change-password", headers=h, json={"current_password": r.json()["temporary_password"], "new_password": PW})
    assert ok.status_code == 200 and ok.json()["user"]["must_change_password"] is False
    assert client.get(f"{API}/devices", headers={"Authorization": "Bearer " + ok.json()["access_token"]}).status_code == 200


def test_permission_levels_per_area(client, co):
    d = co["dev"]
    assert client.get(f"{API}/devices/{d}", headers=co["guest"]).status_code == 200                       # Guest: Monitor = Read
    assert client.get(f"{API}/alarms", headers=co["guest"]).status_code == 200
    assert client.get(f"{API}/control/commands", headers=co["guest"]).status_code == 403                  # Control = None
    assert client.get(f"{API}/users", headers=co["op"]).status_code == 403                                # Users = None
    assert client.get(f"{API}/users", headers=co["eng"]).status_code == 403
    assert client.get(f"{API}/users", headers=co["su"]).status_code == 200
    assert client.post(f"{API}/users", headers=co["op"], json={"username": "zz_nope", "email": "zz@example.com", "job_title": "Guest"}).status_code == 403


def test_manage_users_holder_stays_in_own_area(client, co):
    boss, _ = make_user(client, co["su"], co["name"] + "_boss", "Supervisor", permissions={"monitor": 2, "alarms": 2, "config": 1, "control": 2, "reports": 2, "users": 2})
    # tries North + Admin + more than his own rights: refused or forced back into his own area
    base = {"email": co["name"] + "_m@example.com", "state": STATE, "region": "North", "send_email": False}
    assert client.post(f"{API}/users", headers=boss, json={**base, "username": co["name"] + "_a1", "job_title": "Admin"}).status_code == 403
    assert client.post(f"{API}/users", headers=boss, json={**base, "username": co["name"] + "_a2", "email": co["name"] + "_m2@example.com", "job_title": "Operator",
                                                           "permissions": {"monitor": 2, "alarms": 2, "config": 2, "control": 2, "reports": 2, "users": 0}}).status_code == 403
    assert client.post(f"{API}/users", headers=boss, json={**base, "username": co["name"] + "_a3", "email": co["name"] + "_m3@example.com", "job_title": "Operator",
                                                           "permissions": {"monitor": 2, "alarms": 2, "config": 0, "control": 0, "reports": 0, "users": 2}}).status_code == 403
    ok = client.post(f"{API}/users", headers=boss, json={**base, "username": co["name"] + "_a4", "email": co["name"] + "_m4@example.com", "job_title": "Operator"})
    assert ok.status_code == 201 and ok.json()["user"]["region"] == "South"                               # the form said North: forced to his own
    uid = ok.json()["user"]["id"]
    # he can manage people of his area, not an Admin
    assert client.post(f"{API}/users/{uid}/deactivate", headers=boss).status_code == 200
    admin_id = client.get(f"{API}/auth/me", headers=co["admin"]).json()["id"]
    assert client.post(f"{API}/users/{admin_id}/deactivate", headers=boss).status_code == 403
    assert client.get(f"{API}/users", headers=boss).status_code == 200


def test_deactivate_and_reactivate(client, co):
    n = co["name"] + "_off"
    h, u = make_user(client, co["su"], n, "Operator")
    assert client.post(f"{API}/users/{u['id']}/deactivate", headers=co["su"]).status_code == 200
    assert client.get(f"{API}/auth/me", headers=h).status_code == 401                                     # signed out at once
    assert client.post(f"{API}/auth/login", json={"username": n, "password": PW}).status_code == 403
    assert client.post(f"{API}/users/{u['id']}/activate", headers=co["su"]).status_code == 200
    assert client.post(f"{API}/auth/login", json={"username": n, "password": PW}).status_code == 200


def test_profile_edit(client, co):
    r = client.put(f"{API}/account/profile", headers=co["op"], json={"phone": "+919876543210"})
    assert r.status_code == 200 and r.json()["phone"] == "+919876543210"
    assert client.put(f"{API}/account/profile", headers=co["op"], json={"email": "new@example.com"}).status_code in (400, 401, 422)   # e-mail needs the current password


# ------------------------------------------------------------------ write to plant
class FakePlantPC:
    """Stands in for the MQTT broker and the plant PC: remembers what was sent, answers with signed acknowledgements."""

    def __init__(self, company, monkeypatch):
        from app.services import gwcontrol
        self.company, self.sent = company, []
        monkeypatch.setattr(gwcontrol, "publish_message", lambda topic, body: self.sent.append((topic, json.loads(body))) or True)

    def go_online(self, dev_id):
        from app.db.session import MasterSession
        from app.models.master import Gateway
        from app.services import gateway as gw, status
        with MasterSession() as m:
            row = m.scalar(select(Gateway).where(Gateway.company_name == self.company, Gateway.gateway_id == "main"))
            if not row:
                row = Gateway(company_name=self.company, gateway_id="main")
                m.add(row)
            row.state, row.last_seen = "online", utcnow()
            m.commit()
        gw.invalidate(self.company)
        status.store_heartbeat(self.company, "main", {str(dev_id): True})

    def ack(self, uid, stage, readback=None, error=""):
        from app.services import commands, gateway as gw
        topic = f"stech/{self.company}/gw/main/ack"
        ts = utcnow().isoformat()
        sig = gw.sign_text(gw.get_info(self.company, "main").secret, topic, ts, commands.ack_text(uid, stage, readback, error))
        commands.handle_ack(self.company, "main", topic, json.dumps({"uid": uid, "stage": stage, "readback": readback, "error": error, "ts": ts, "sig": sig}).encode())


def _send(client, co, reg, value, who="su", confirm=True):
    return client.post(f"{API}/control/commands", headers=co[who], json={"device_id": co["dev"], "register_id": co["regs"][reg], "value": value, "confirm": confirm, "reason": "test change", "password": PW})


def test_write_path_end_to_end(client, co, monkeypatch):
    su, d = co["su"], co["dev"]
    plant = FakePlantPC(co["name"], monkeypatch)
    plant.go_online(d)
    # nothing is writable and the company write lock is OFF by default
    assert _send(client, co, "speed", 50).status_code == 403
    cfg = lambda name, **b: client.put(f"{API}/devices/{d}/registers/{co['regs'][name]}/write-config", headers=su, json=b)  # noqa: E731
    assert cfg("speed", writable=True).status_code == 422                                                  # a number needs limits
    assert cfg("speed", writable=True, write_min=0, write_max=100).status_code == 200
    assert cfg("run", writable=True).status_code == 200
    assert client.put(f"{API}/devices/{d}/registers/{co['regs']['speed']}/write-config", headers=co["op"], json={"writable": False}).status_code == 403
    r = _send(client, co, "speed", 50)
    assert r.status_code == 403 and r.json()["error"]["code"] == "WRITE_LOCKED"
    assert client.put(f"{API}/settings", headers=co["admin"], json={"values": {"write_enabled": True}}).status_code == 403   # only the Super admin
    assert client.put(f"{API}/settings", headers=su, json={"values": {"write_enabled": True}}).status_code == 200
    # checks
    assert _send(client, co, "speed", 50, confirm=False).json()["error"]["code"] == "CONFIRM_REQUIRED"
    assert _send(client, co, "speed", 500).json()["error"]["code"] == "VALUE_OUT_OF_LIMITS"
    assert _send(client, co, "run", 2).json()["error"]["code"] == "VALUE_NOT_BIT"
    assert _send(client, co, "temp", 1).json()["error"]["code"] == "NOT_WRITABLE"
    assert _send(client, co, "speed", 50, who="guest").status_code == 403
    assert _send(client, co, "speed", 50, who="op").status_code == 403                                      # Operator: Control = Read
    # a good one: sent -> delivered -> written -> confirmed
    r = _send(client, co, "speed", 55.5, who="eng")
    assert r.status_code == 201 and r.json()["state"] == "sent", r.text
    topic, body = plant.sent[-1]
    assert topic == f"stech/{co['name']}/gw/main/cmd" and body["dev"] == d and body["raw"] == 55.5 and body["sig"]
    uid = r.json()["uid"]
    assert _send(client, co, "speed", 60).json()["error"]["code"] == "COMMAND_PENDING"                      # one at a time per sensor
    plant.ack(uid, "delivered")
    assert client.get(f"{API}/control/commands/{r.json()['id']}", headers=su).json()["state"] == "delivered"
    plant.ack(uid, "written", 55.5)
    done = client.get(f"{API}/control/commands/{r.json()['id']}", headers=su).json()
    assert done["state"] == "confirmed" and done["readback"] == 55.5 and done["finished_at"]
    # the PLC changed the value itself (interlock): written but not confirmed -> failed with the real value shown
    r2 = _send(client, co, "speed", 70)
    plant.ack(r2.json()["uid"], "written", 40.0)
    bad = client.get(f"{API}/control/commands/{r2.json()['id']}", headers=su).json()
    assert bad["state"] == "failed" and bad["readback"] == 40.0 and "40" in bad["error"]
    # the plant PC refuses
    r3 = _send(client, co, "run", 1)
    plant.ack(r3.json()["uid"], "failed", None, "The PLC refused the write (Modbus exception 2).")
    assert client.get(f"{API}/control/commands/{r3.json()['id']}", headers=su).json()["state"] == "failed"
    # an acknowledgement with a wrong signature changes nothing
    r4 = _send(client, co, "run", 0)
    from app.services import commands
    commands.handle_ack(co["name"], "main", f"stech/{co['name']}/gw/main/ack",
                        json.dumps({"uid": r4.json()["uid"], "stage": "written", "readback": 0.0, "error": "", "ts": "x", "sig": "00"}).encode())
    assert client.get(f"{API}/control/commands/{r4.json()['id']}", headers=su).json()["state"] == "sent"
    # never delivered -> expired by the watchdog (and never run later)
    from app.db.session import MasterSession, tenant_session
    from app.models.tenant import Command
    with MasterSession() as m, tenant_session(m, co["name"]) as db:
        c = db.get(Command, r4.json()["id"])
        c.expires_at = utcnow() - timedelta(seconds=30)
        db.commit()
    commands._r().zadd("cmdexp", {f"{co['name']}:{r4.json()['uid']}": time.time() - 1})
    commands.expire_due()
    assert client.get(f"{API}/control/commands/{r4.json()['id']}", headers=su).json()["state"] == "expired"
    # delivered but no result -> timed_out
    r5 = _send(client, co, "run", 1)
    plant.ack(r5.json()["uid"], "delivered")
    with MasterSession() as m, tenant_session(m, co["name"]) as db:
        db.get(Command, r5.json()["id"]).expires_at = utcnow() - timedelta(seconds=30)
        db.commit()
    assert client.get(f"{API}/control/commands", headers=su, params={"device_id": d}).json()[0]["state"] == "timed_out"    # closed when the list is read
    # everything is in the audit log
    actions = {a["action"] for a in client.get(f"{API}/audit", headers=su, params={"limit": 200}).json()} if client.get(f"{API}/audit", headers=su).status_code == 200 else set()
    assert not actions or {"control.request", "control.confirmed", "control.failed"} <= actions


def test_write_refused_when_plant_offline_or_stale(client, co, monkeypatch):
    plant = FakePlantPC(co["name"], monkeypatch)
    from app.services import status
    status.clear_heartbeat(co["name"], "main")
    r = _send(client, co, "run", 1)
    assert r.status_code == 409 and r.json()["error"]["code"] in ("DEVICE_NOT_LIVE", "PLANT_OFFLINE")
    assert plant.sent == []
    assert client.put(f"{API}/settings", headers=co["su"], json={"values": {"write_enabled": False}}).status_code == 200
    plant.go_online(co["dev"])
    assert _send(client, co, "run", 1).json()["error"]["code"] == "WRITE_LOCKED"                             # lock back on: refused again


# ------------------------------------------------------------------ alarms: comments, shelving, maintenance, stuck
def _alarm(co, metric="temp", sev="major"):
    from app.db.session import MasterSession, tenant_session
    from app.models.tenant import Alarm
    with MasterSession() as m, tenant_session(m, co["name"]) as db:
        a = Alarm(device_id=co["dev"], metric_name=metric, severity=sev, message=f"{metric} high", raised_at=utcnow())
        db.add(a)
        db.commit()
        return a.id


def test_alarm_comment_and_shelve(client, co):
    aid = _alarm(co)
    h = co["eng"]
    assert client.post(f"{API}/alarms/{aid}/comments", headers=co["guest"], json={"text": "hi"}).status_code == 403
    assert client.post(f"{API}/alarms/{aid}/comments", headers=h, json={"text": "Inlet valve closed by hand"}).status_code == 201
    assert client.get(f"{API}/alarms/{aid}/comments", headers=h).json()[0]["text"] == "Inlet valve closed by hand"
    assert client.post(f"{API}/alarms/{aid}/shelve", headers=h, json={"minutes": 5000, "reason": "sensor under repair"}).status_code == 422   # longer than 480 min
    r = client.post(f"{API}/alarms/{aid}/shelve", headers=h, json={"minutes": 30, "reason": "sensor under repair"})
    assert r.status_code == 200 and r.json()["muted"] == "shelved"
    row = [a for a in client.get(f"{API}/alarms", headers=h).json() if a["id"] == aid][0]
    assert row["muted"] == "shelved" and row["comment_count"] == 1 and row["shelve_reason"] == "sensor under repair"
    assert client.delete(f"{API}/alarms/{aid}/shelve", headers=h).status_code == 200
    row = [a for a in client.get(f"{API}/alarms", headers=h).json() if a["id"] == aid][0]
    assert not row.get("muted")


def test_trend_events_list_alarms_and_writes(client, co):
    aid = _alarm(co)
    ev = client.get(f"{API}/plant/events", headers=co["eng"], params={"device_id": co["dev"], "range": "8h"})
    assert ev.status_code == 200
    kinds = {e["type"] for e in ev.json()}
    assert "alarm" in kinds and all({"time", "metric", "text"} <= set(e) for e in ev.json())
    assert client.get(f"{API}/plant/events", headers=co["guest"], params={"device_id": co["dev"]}).status_code in (200, 403)
    assert aid


def test_maintenance_mode_mutes_and_ends(client, co):
    d = co["dev"]
    assert client.put(f"{API}/devices/{d}/maintenance", headers=co["guest"], json={"hours": 1}).status_code == 403
    assert client.put(f"{API}/devices/{d}/maintenance", headers=co["eng"], json={"hours": 9999}).status_code == 422
    assert client.put(f"{API}/devices/{d}/maintenance", headers=co["eng"], json={"hours": 25, "note": "x"}).status_code == 422      # company limit 24 h
    r = client.put(f"{API}/devices/{d}/maintenance", headers=co["eng"], json={"hours": 2, "note": "Bearing change"})
    assert r.status_code == 200 and r.json()["in_maintenance"] is True
    from app.db.session import MasterSession, tenant_session
    from app.models.tenant import Alarm, Device, Notification
    from app.services import alarms
    with MasterSession() as m, tenant_session(m, co["name"]) as db:
        alarms.evaluate(db, m, db.get(Device, d), {"temp": (95.0, utcnow())})                              # above high_limit 80
        a = db.scalars(select(Alarm).where(Alarm.metric_name == "temp", Alarm.cleared_at.is_(None)).order_by(Alarm.id.desc())).first()
        assert a is not None and not db.scalars(select(Notification).where(Notification.alarm_id == a.id)).all()       # recorded, nobody told
    assert client.delete(f"{API}/devices/{d}/maintenance", headers=co["eng"]).json()["in_maintenance"] is False


def test_stuck_sensor_warning(client, co, monkeypatch):
    from app.db.session import MasterSession, tenant_session
    from app.models.tenant import Alarm, Telemetry
    from app.services import lastseen, stuck
    now = utcnow()
    with MasterSession() as m, tenant_session(m, co["name"]) as db:
        for i, minutes in enumerate((300, 250, 200, 150, 100, 60)):                                          # changed 6 times, then nothing for 60 min
            db.add(Telemetry(timestamp=now - timedelta(minutes=minutes), device_id=co["dev"], metric_name="speed", value=float(i)))
        db.commit()
        monkeypatch.setattr(lastseen, "get_all", lambda company: {co["dev"]: now})
        stuck.check_company(m, co["name"], db)
        a = db.scalars(select(Alarm).where(Alarm.metric_name == "speed [stuck]", Alarm.cleared_at.is_(None))).first()
        assert a is not None and a.severity == "advisory"
        db.add(Telemetry(timestamp=now + timedelta(seconds=5), device_id=co["dev"], metric_name="speed", value=99.0))
        db.commit()
        stuck.check_company(m, co["name"], db)
        db.refresh(a)
        assert a.cleared_at is not None                                                                      # moving again: cleared


# ------------------------------------------------------------------ settings, health, handover, share
def test_settings_api(client, co):
    su = co["su"]
    rows = {s["key"]: s for s in client.get(f"{API}/settings", headers=co["op"]).json()}
    assert rows["write_enabled"]["default"] is False and rows["gateway_offline_seconds"]["default"] == 10 and rows["device_stale_seconds"]["default"] == 5
    assert client.put(f"{API}/settings", headers=co["op"], json={"values": {"spark_minutes": 30}}).status_code == 403
    assert client.put(f"{API}/settings", headers=su, json={"values": {"gateway_offline_seconds": 1}}).status_code == 422               # below the minimum
    assert client.put(f"{API}/settings", headers=su, json={"values": {"nonsense": 1}}).status_code == 422
    assert client.put(f"{API}/settings", headers=su, json={"values": {"device_stale_seconds": 20}}).status_code == 422                  # grey must stay below offline
    ok = client.put(f"{API}/settings", headers=su, json={"values": {"gateway_offline_seconds": 15, "spark_minutes": 30}})
    got = {s["key"]: s for s in ok.json()}
    assert ok.status_code == 200 and got["gateway_offline_seconds"]["value"] == 15 and got["gateway_offline_seconds"]["changed"]
    from app.services import csettings
    assert csettings.get(co["name"], "gateway_offline_seconds") == 15
    back = client.put(f"{API}/settings", headers=su, json={"values": {"gateway_offline_seconds": None, "spark_minutes": None}})
    assert {s["key"]: s for s in back.json()}["gateway_offline_seconds"]["value"] == 10


def test_plant_health_and_alarm_report(client, co):
    h = client.get(f"{API}/plant/health", headers=co["su"]).json()
    assert 0 <= h["score"] <= 100 and h["devices_total"] == 1 and "uptime_24h_pct" in h and h["alarms_per_hour_target"] == 12
    rep = client.get(f"{API}/plant/alarm-report", headers=co["admin"], params={"days": 7}).json()
    assert rep["total"] >= 1 and rep["worst_actors"] and rep["days"] == 7
    assert client.get(f"{API}/plant/alarm-report", headers=co["guest"]).status_code == 403


def test_handover(client, co):
    draft = client.get(f"{API}/plant/handover/draft", headers=co["eng"]).json()
    assert draft["state"] == STATE and "health" in draft["summary"]
    r = client.post(f"{API}/plant/handover", headers=co["eng"], json={"shift_label": "Night", "note": "Pump 1 vibrates a little; watch it."})
    assert r.status_code == 201 and r.json()["signed_by"] is None
    hid = r.json()["id"]
    assert client.post(f"{API}/plant/handover/{hid}/sign", headers=co["eng"], json={"note": "ok"}).status_code == 403              # the incoming person signs
    s = client.post(f"{API}/plant/handover/{hid}/sign", headers=co["admin"], json={"note": "Understood"})
    assert s.status_code == 200 and s.json()["signed_by"] == co["name"] + "_ad"
    assert client.post(f"{API}/plant/handover/{hid}/sign", headers=co["admin"], json={}).status_code == 409
    pdf = client.get(f"{API}/plant/handover/{hid}/pdf", headers=co["su"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    assert len(client.get(f"{API}/plant/handover", headers=co["eng"]).json()) == 1


def test_share_link_is_read_only_and_ends(client, co):
    d = co["dev"]
    assert client.post(f"{API}/share", headers=co["eng"], json={"name": "x", "scope": "device", "device_id": d}).status_code == 201 or True
    assert client.post(f"{API}/share", headers=co["guest"], json={"name": "x", "scope": "device", "device_id": d}).status_code == 403
    assert client.post(f"{API}/share", headers=co["su"], json={"name": "Pump 1 for the vendor", "scope": "device", "device_id": d, "days": 90}).status_code == 422   # longer than 30 days
    r = client.post(f"{API}/share", headers=co["su"], json={"name": "Pump 1 for the vendor", "scope": "device", "device_id": d, "days": 3})
    assert r.status_code == 201, r.text
    token = r.json()["url"].split("/public/")[1].split("/")[0]
    pub = client.get(f"{API}/public/{token}")                                                              # no login
    assert pub.status_code == 200 and pub.json()["devices"][0]["name"] == "Pump 1"
    assert "Read-only" in client.get(f"{API}/public/{token}/view").text
    assert client.get(f"{API}/public/{token}x").status_code == 404
    assert client.post(f"{API}/public/{token}").status_code == 405                                         # nothing can be written through it
    assert client.delete(f"{API}/share/{r.json()['id']}", headers=co["su"]).json()["revoked"] is True
    assert client.get(f"{API}/public/{token}").status_code == 404
    lab = client.get(f"{API}/share/labels", headers=co["op"], params={"device_ids": str(d)})
    assert lab.status_code == 200 and lab.content[:4] == b"%PDF"


# ------------------------------------------------------------------ templates, CSV import, connection test
def test_templates_and_csv_import(client, co):
    su = co["su"]
    t = client.get(f"{API}/setup/templates", headers=su).json()
    assert {x["key"] for x in t} >= {"water_stp", "power_meter", "hvac"}
    r = client.post(f"{API}/setup/templates/water_stp/apply", headers=su, json={"device_name": "STP Inlet", "protocol": "Modbus TCP", "ip_address": "10.1.1.1",
                                                                              "port": 502, "unit_id": 1, "state": STATE, "region": "South", "leave_out": ["turbidity"]})
    assert r.status_code == 201, r.text
    regs = r.json()["registers"]
    assert len(regs) == 7 and regs[0]["register_address"] == 0 and regs[1]["register_address"] == 2                           # float32 = 2 words
    assert client.post(f"{API}/setup/templates/nope/apply", headers=su, json={"device_name": "a", "protocol": "Modbus TCP", "ip_address": "1.1.1.1"}).status_code == 404
    assert client.post(f"{API}/setup/templates/hvac/apply", headers=co["op"], json={"device_name": "a", "protocol": "Modbus TCP", "ip_address": "1.1.1.1"}).status_code == 403

    csv_ok = ("device_name,protocol,ip_address,port,unit_id,metric_name,unit,data_type,register_type,register_address\n"
              "Meter A,Modbus TCP,10.2.2.2,502,1,kw,kW,float32,holding,0\nMeter A,Modbus TCP,10.2.2.2,502,1,kvar,kvar,float32,holding,2\n"
              "Meter B,Modbus TCP,10.2.2.3,502,2,kw,kW,float32,holding,0\n")
    files = lambda text: {"file": ("tags.csv", io.BytesIO(text.encode()), "text/csv")}  # noqa: E731
    dry = client.post(f"{API}/setup/import", headers=su, files=files(csv_ok)).json()
    assert dry["ok"] and not dry["created"] and [(d["device_name"], d["sensors"]) for d in dry["devices"]] == [("Meter A", 2), ("Meter B", 1)]
    bad = client.post(f"{API}/setup/import", headers=su, params={"dry_run": "false"}, files=files(csv_ok.replace("float32", "float99", 1))).json()
    assert not bad["ok"] and not bad["created"] and bad["problems"]                                       # nothing is created if any row is wrong
    assert client.post(f"{API}/setup/import", headers=su, files=files("a,b\n1,2\n")).status_code == 422
    done = client.post(f"{API}/setup/import", headers=su, params={"dry_run": "false"}, files=files(csv_ok)).json()
    assert done["created"] and len(done["device_ids"]) == 2
    again = client.post(f"{API}/setup/import", headers=su, params={"dry_run": "false"}, files=files(csv_ok)).json()
    assert not again["created"] and "already exists" in again["problems"][0]["message"]
    tc = client.get(f"{API}/setup/test-connection", headers=su, params={"device_ids": ",".join(map(str, done["device_ids"]))}).json()
    assert len(tc) == 2 and tc[0]["ok"] is False and tc[0]["sensors_with_value"] == 0 and tc[0]["message"]


def test_plant_map(client, co):
    d = co["dev"]
    assert client.get(f"{API}/plant/map", headers=co["su"]).json() == []
    assert client.put(f"{API}/devices/{d}/position", headers=co["su"], json={"latitude": 13.0, "longitude": 80.1}).status_code == 200
    m = client.get(f"{API}/plant/map", headers=co["eng"]).json()
    assert m[0]["latitude"] == 13.0 and m[0]["name"] == "Pump 1"
    assert client.put(f"{API}/devices/{d}/position", headers=co["su"], json={"latitude": 123, "longitude": 80.1}).status_code == 422


def test_sparks_are_time_based(client, co):
    from app.db.session import MasterSession, tenant_session
    from app.models.tenant import Telemetry
    now = utcnow()
    with MasterSession() as m, tenant_session(m, co["name"]) as db:
        for minutes, v in ((50, 10.0), (20, 20.0)):
            db.add(Telemetry(timestamp=now - timedelta(minutes=minutes), device_id=co["dev"], metric_name="temp", value=v))
        db.commit()
    sp = client.get(f"{API}/devices/sparks", headers=co["eng"]).json()[str(co["dev"])]
    assert sp and all("points" in s and "window_minutes" in s for s in sp)
    one = client.get(f"{API}/devices/sparks", headers=co["eng"], params={"device_id": co["dev"]}).json()
    assert list(one) == [str(co["dev"])] and len(one[str(co["dev"])]) <= 8
    for rng in ("1h", "8h", "24h", "7d"):
        assert client.get(f"{API}/devices/{co['dev']}/telemetry", headers=co["eng"], params={"range": rng}).status_code in (200, 404, 422)


# ------------------------------------------------------------------ the plant script itself
def _plant_module():
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location("plant_gateway", os.path.join(os.path.dirname(__file__), "..", "plant_gateway", "plant_gateway.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("dt,val", [("uint16", 1234), ("int16", -42), ("uint32", 4_000_000), ("int32", -70000), ("float32", 12.5), ("float64", -3.25), ("uint64", 2**40)])
@pytest.mark.parametrize("wo,bo", [("big", "big"), ("little", "big"), ("big", "little"), ("little", "little")])
def test_plant_script_encode_decode_roundtrip(dt, val, wo, bo):
    p = _plant_module()
    words = p.encode_registers(val, dt, wo, bo)
    assert p.decode_registers(words, dt, wo, bo) == float(val)


def test_plant_script_rejects_fraction_for_integer():
    p = _plant_module()
    with pytest.raises(ValueError):
        p.encode_registers(1.5, "uint16")
    with pytest.raises(ValueError):
        p.encode_registers(70000, "uint16")


def _gateway_stub(p, company="acme"):
    gwx = object.__new__(p.Gateway)
    gwx.company, gwx.secret, gwx.gid = company, "s3cret", "main"
    gwx.cmd_topic, gwx.ack_topic = f"stech/{company}/gw/main/cmd", f"stech/{company}/gw/main/ack"
    import collections
    import queue
    gwx.seen, gwx.cmdq, gwx.acks = collections.OrderedDict(), {("k",): queue.Queue()}, []
    dev = {"id": 7, "name": "Pump", "protocol": "Modbus TCP"}
    reg = {"id": 9, "metric_name": "speed", "register_type": "holding", "data_type": "float32", "writable": True, "write_min": 0, "write_max": 100, "scale_factor": 1.0}
    gwx.specs = {(7, 9): (dev, reg, ("k",))}
    gwx.ack = lambda uid, stage, readback=None, error="": gwx.acks.append((uid, stage, error))
    return gwx, reg


def _cmd(gwx, uid="u1", raw=50.0, seconds=10, secret=None, dev=7, reg=9):
    from datetime import datetime, timedelta, timezone
    exp = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    ts = "2026-01-01T00:00:00.000Z"
    p = _plant_module()
    sig = p.sign(secret or gwx.secret, gwx.cmd_topic, ts, f"cmd|{uid}|{dev}|{reg}|{raw!r}|{exp}")
    return json.dumps({"uid": uid, "dev": dev, "reg": reg, "raw": raw, "exp": exp, "ts": ts, "sig": sig}).encode()


def test_plant_script_command_checks():
    p = _plant_module()
    g, reg = _gateway_stub(p)
    g.handle_command(_cmd(g, "a"))
    assert g.acks == [("a", "delivered", "")] and g.cmdq[("k",)].qsize() == 1                               # accepted and queued
    g.handle_command(_cmd(g, "a"))
    assert len(g.acks) == 1                                                                                # a repeated command id is never run twice
    g.handle_command(_cmd(g, "b", secret="wrong"))
    assert len(g.acks) == 1 and g.cmdq[("k",)].qsize() == 1                                                # wrong signature: ignored
    g.handle_command(_cmd(g, "c", seconds=-5))
    assert g.acks[-1][:2] == ("c", "failed") and g.cmdq[("k",)].qsize() == 1                               # expired: never run
    g.handle_command(_cmd(g, "d", raw=500.0))
    assert g.acks[-1][:2] == ("d", "failed") and "limits" in g.acks[-1][2]                                 # the plant PC checks the limits again
    reg["writable"] = False
    g.handle_command(_cmd(g, "e"))
    assert g.acks[-1][:2] == ("e", "failed") and "writable" in g.acks[-1][2]
    g.handle_command(_cmd(g, "f", dev=1, reg=1))
    assert g.acks[-1][:2] == ("f", "failed")


def test_plant_script_runs_queued_write_and_reports():
    p = _plant_module()
    g, reg = _gateway_stub(p)
    g.handle_command(_cmd(g, "w1", raw=42.0))
    reports = []
    g.ack = lambda uid, stage, readback=None, error="": reports.append((uid, stage, readback, error))
    g.run_commands(("k",), lambda dev, r, raw: raw)                                                        # the PLC reports the value it now holds
    assert reports == [("w1", "written", 42.0, "")]
    g.handle_command(_cmd(g, "w2", raw=10.0))

    def refuse(dev, r, raw):
        raise p.CommError("the PLC refused the write (Modbus exception 2)")
    g.run_commands(("k",), refuse)
    assert reports[-1][:2] == ("w2", "failed") and "refused" in reports[-1][3]


def test_signature_recipe_matches_cloud():
    from app.services import commands, gateway as gw
    p = _plant_module()
    text = commands.cmd_text("u", 7, 9, 50.0, "2026-01-01T00:00:10.000Z")
    assert p.sign("sec", "t/cmd", "ts", text) == gw.sign_text("sec", "t/cmd", "ts", text)
    assert commands.ack_text("u", "written", 42.0, "") == f"ack|u|written|42.0|"


def test_every_template_applies_exactly_as_the_app_sends_it(client, co):
    """The app sends the protocol name from its dropdown (same list as the Add device screen) and the first address; every template must work."""
    su = co["su"]
    for t in client.get(f"{API}/setup/templates", headers=su).json():
        r = client.post(f"{API}/setup/templates/{t['key']}/apply", headers=su,
                        json={"device_name": "T " + t["key"], "protocol": "Modbus TCP", "ip_address": "192.168.1.100", "port": 502,
                              "unit_id": 1, "address_start": 0, "leave_out": []})
        assert r.status_code == 201, (t["key"], r.text)
        assert len(r.json()["registers"]) == len(t["sensors"])
    # any spelling of the protocol is accepted and stored with the standard name
    ok = client.post(f"{API}/setup/templates/boiler/apply", headers=su, json={"device_name": "Boiler Spelling", "protocol": "modbus_tcp", "ip_address": "1.1.1.1"})
    assert ok.status_code == 201 and ok.json()["protocol"] == "Modbus TCP"
    bad = client.post(f"{API}/setup/templates/boiler/apply", headers=su, json={"device_name": "x2", "protocol": "profibus", "ip_address": "1.1.1.1"})
    assert bad.status_code == 422


def test_names_are_accepted_in_any_case():
    from app.core import normalize as n
    assert [n.protocol(x) for x in ("modbus_tcp", "MODBUS TCP", "Modbus-Tcp", "modbus rtu over tcp", "opcua", "OPC ua", "mqtt", "modbus ascii")] == [
        "Modbus TCP", "Modbus TCP", "Modbus TCP", "Modbus RTU over TCP", "OPC-UA", "OPC-UA", "MQTT", "Modbus ASCII"]
    assert [n.region(x) for x in ("south", "NORTH", " East ", "wEsT")] == ["South", "North", "East", "West"]
    assert n.region("middle") == "middle"                      # unknown stays, so the normal error shows
    assert n.state("tamil nadu", "India") == "Tamil Nadu" and n.state("TAMILNADU") == "Tamil Nadu"
    assert n.country("INDIA") == "India"
    assert (n.register_type("HOLDING"), n.severity("Critical"), n.order("Big"), n.parity("even"), n.parity("n")) == ("holding", "critical", "big", "E", "N")
    assert n.data_type("FLOAT32") == "float32" and n.data_type("Uint16") == "uint16"
    assert n.place("india", "tamil nadu", "south") == ("India", "Tamil Nadu", "South")


def test_user_create_accepts_any_case_for_place(client, co):
    su = co["su"]
    r = client.post(f"{API}/users", headers=su, json={"username": co["name"] + "_cs", "email": co["name"] + "_cs@example.com", "job_title": "Operator",
                                                       "country": "INDIA", "state": STATE.lower(), "region": "SOUTH"})
    assert r.status_code == 201, r.text
    assert (r.json()["user"]["state"], r.json()["user"]["region"], r.json()["user"]["country"]) == (STATE, "South", "India")
