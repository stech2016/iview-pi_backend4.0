"""v1.1.5 build additions: alert rule kinds, control upgrades (reason, step-up, pulse, approval), downtime, batches, saved screens, energy documents."""
import json
import time
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.core.timeutil import utcnow
from tests.helpers import API, PW
from tests.test_v115 import FakePlantPC, client, co, _send  # noqa: F401  (fixtures)


def _tenant(co):
    from app.db.session import MasterSession, tenant_session
    return MasterSession(), tenant_session


# ------------------------------------------------------------------ alert rule kinds
def test_alert_rule_kinds_validate_and_round_trip(client, co):
    su, d = co["su"], co["dev"]
    base = {"device_id": d, "metric_name": "temp", "severity": "major"}
    assert client.post(f"{API}/alert-rules", headers=su, json={**base, "kind": "deadband", "op": ">", "threshold": 50}).status_code == 422     # needs a margin
    ok = client.post(f"{API}/alert-rules", headers=su, json={**base, "kind": "deadband", "op": ">", "threshold": 50, "deadband": 5})
    assert ok.status_code == 201 and ok.json()["kind"] == "deadband" and ok.json()["deadband"] == 5
    rate = client.post(f"{API}/alert-rules", headers=su, json={**base, "kind": "rate", "op": ">", "threshold": 10, "window_seconds": 30})
    assert rate.status_code == 201 and rate.json()["window_seconds"] == 30
    nodata = client.post(f"{API}/alert-rules", headers=su, json={**base, "kind": "no_data", "window_seconds": 300})
    assert nodata.status_code == 201
    assert client.post(f"{API}/alert-rules", headers=su, json={**base, "kind": "magic"}).status_code == 422
    assert {r["kind"] for r in client.get(f"{API}/alert-rules", headers=su, params={"device_id": d}).json()} >= {"deadband", "rate", "no_data"}


def test_alert_rule_logic():
    from datetime import datetime, timezone
    from app.services import alertrules as ar
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    dead = SimpleNamespace(op=">", threshold=50.0, deadband=5.0, window_seconds=60, metric_name="t")
    assert ar._level(dead, 51, t0, None, (1, 1))[0] is True
    assert ar._level(dead, 48, t0, object(), (1, 1))[0] is True        # still inside the deadband -> stays raised
    assert ar._level(dead, 44, t0, object(), (1, 1))[0] is False       # back past the margin -> clears
    rate = SimpleNamespace(op=">", threshold=10.0, deadband=0, window_seconds=60, metric_name="p")
    k = (99, 99)
    assert ar._rate(rate, 20, t0, None, k)[0] is False
    assert ar._rate(rate, 25, t0 + timedelta(seconds=30), None, k)[0] is False       # +5 only
    assert ar._rate(rate, 45, t0 + timedelta(seconds=50), None, k)[0] is True        # +25 inside 60 s


# ------------------------------------------------------------------ control upgrades
def _prep(client, co):
    su, d = co["su"], co["dev"]
    for name, b in (("speed", {"writable": True, "write_min": 0, "write_max": 100}), ("run", {"writable": True})):
        assert client.put(f"{API}/devices/{d}/registers/{co['regs'][name]}/write-config", headers=su, json=b).status_code == 200
    assert client.put(f"{API}/settings", headers=su, json={"values": {"write_enabled": True}}).status_code == 200


def test_control_reason_stepup_pulse_and_approval(client, co, monkeypatch):
    su, d = co["su"], co["dev"]
    plant = FakePlantPC(co["name"], monkeypatch)
    plant.go_online(d)
    _prep(client, co)
    body = {"device_id": d, "register_id": co["regs"]["speed"], "value": 10, "confirm": True}
    assert client.post(f"{API}/control/commands", headers=su, json={**body, "password": PW}).status_code == 422                       # reason is required
    r = client.post(f"{API}/control/commands", headers=su, json={**body, "reason": "trial", "password": "wrong-password"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "STEPUP_FAILED"
    assert client.post(f"{API}/control/commands", headers=su, json={**body, "reason": "trial"}).json()["error"]["code"] == "STEPUP_FAILED"
    # pulse works on on/off points only
    bad = client.post(f"{API}/control/commands", headers=su, json={**body, "reason": "reset", "password": PW, "pulse": True})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "PULSE_NEEDS_BIT"
    pb = {"device_id": d, "register_id": co["regs"]["run"], "value": 0, "confirm": True, "reason": "fault reset", "password": PW, "pulse": True}
    p = client.post(f"{API}/control/commands", headers=su, json=pb)
    assert p.status_code == 201 and p.json()["value"] == 1 and p.json()["pulse"] is True and p.json()["reason"] == "fault reset"
    plant.ack(p.json()["uid"], "written", 1.0)
    from app.services import commands
    commands.release_pulse(co["name"], p.json()["uid"])                                         # what the 1-second timer does
    last = plant.sent[-1][1]
    assert last["raw"] == 0.0 and last["uid"] != p.json()["uid"]
    plant.ack(last["uid"], "written", 0.0)
    # second-person approval
    assert client.put(f"{API}/settings", headers=su, json={"values": {"write_approval": True}}).status_code == 200
    a = client.post(f"{API}/control/commands", headers=su, json={**body, "reason": "needs approval", "password": PW})
    assert a.status_code == 201 and a.json()["state"] == "awaiting"
    n = len(plant.sent)
    assert client.post(f"{API}/control/commands/{a.json()['id']}/approve", headers=su).json()["error"]["code"] == "SELF_APPROVAL"
    ok = client.post(f"{API}/control/commands/{a.json()['id']}/approve", headers=co["admin"])
    assert ok.status_code == 200 and ok.json()["state"] == "sent" and ok.json()["approved_by"] and len(plant.sent) == n + 1
    plant.ack(a.json()["uid"], "written", 10.0)
    b = client.post(f"{API}/control/commands", headers=su, json={**body, "value": 20, "reason": "reject me", "password": PW})
    rej = client.post(f"{API}/control/commands/{b.json()['id']}/reject", headers=co["admin"])
    assert rej.json()["state"] == "rejected"
    assert client.put(f"{API}/settings", headers=su, json={"values": {"write_approval": False}}).status_code == 200


# ------------------------------------------------------------------ downtime
def test_downtime_log(client, co):
    from app.db.session import MasterSession, tenant_session
    from app.models.tenant import DowntimeEvent
    su, d = co["su"], co["dev"]
    now = utcnow()
    with MasterSession() as m, tenant_session(m, co["name"]) as db:
        db.add(DowntimeEvent(device_id=d, started_at=now - timedelta(hours=3), ended_at=now - timedelta(hours=2), source="runbit"))
        db.add(DowntimeEvent(device_id=d, started_at=now - timedelta(hours=1), ended_at=now - timedelta(minutes=30), source="offline"))
        db.commit()
    rows = client.get(f"{API}/downtime", headers=su, params={"device_id": d}).json()
    assert len(rows) >= 2 and round(rows[-1]["minutes"]) == 60
    assert client.put(f"{API}/downtime/{rows[0]['id']}", headers=co["op"], json={"reason": "Not in list"}).status_code == 422
    assert client.put(f"{API}/downtime/{rows[0]['id']}", headers=co["op"], json={"reason": "Breakdown", "note": "bearing"}).json()["reason"] == "Breakdown"
    s = client.get(f"{API}/downtime/summary", headers=su, params={"device_id": d, "days": 1}).json()
    assert s["events"] >= 2 and 0 <= s["availability_percent"] < 100 and s["pareto"][0]["minutes"] >= 30
    assert client.get(f"{API}/downtime/export", headers=su, params={"format": "csv", "device_id": d}).status_code == 200
    assert client.get(f"{API}/downtime/reasons", headers=co["guest"]).status_code == 200


def test_downtime_auto_detection(client, co):
    from app.db.session import MasterSession, tenant_session
    from app.models.tenant import Device, DeviceRegister, DowntimeEvent, Telemetry
    from app.services import downtime, lastseen
    d = co["dev"]
    with MasterSession() as m, tenant_session(m, co["name"]) as db:
        db.get(DeviceRegister, co["regs"]["run"]).sensor_type = "status"
        db.add(Telemetry(device_id=d, metric_name="run", value=0.0, timestamp=utcnow()))
        db.query(DowntimeEvent).filter(DowntimeEvent.device_id == d, DowntimeEvent.ended_at.is_(None)).delete()
        db.commit()
        lastseen.record(co["name"], d, utcnow())
        downtime.check_company(co["name"], db)
        ev = db.scalar(select(DowntimeEvent).where(DowntimeEvent.device_id == d, DowntimeEvent.ended_at.is_(None)))
        assert ev is not None and ev.source == "runbit"
        db.add(Telemetry(device_id=d, metric_name="run", value=1.0, timestamp=utcnow() + timedelta(seconds=1)))
        db.commit()
        downtime.check_company(co["name"], db)
        assert db.scalar(select(DowntimeEvent).where(DowntimeEvent.device_id == d, DowntimeEvent.ended_at.is_(None))) is None


# ------------------------------------------------------------------ batches
def test_batches(client, co):
    from app.db.session import MasterSession, tenant_session
    from app.models.tenant import Telemetry
    su, d = co["su"], co["dev"]
    s = client.post(f"{API}/batches/start", headers=co["op"], json={"device_id": d, "batch_no": "B-2026/001", "product": "Resin"})
    assert s.status_code == 201 and s.json()["running"] is True
    assert client.post(f"{API}/batches/start", headers=su, json={"device_id": d, "batch_no": "B2"}).json()["error"]["code"] == "BATCH_RUNNING"
    with MasterSession() as m, tenant_session(m, co["name"]) as db:
        for i, v in enumerate((60.0, 70.0, 80.0)):
            db.add(Telemetry(device_id=d, metric_name="temp", value=v, timestamp=utcnow()))
        db.commit()
    e = client.post(f"{API}/batches/{s.json()['id']}/end", headers=co["op"])
    assert e.status_code == 200 and e.json()["running"] is False
    t = [x for x in e.json()["summary"]["sensors"] if x["metric"] == "temp"][0]
    assert (t["min"], t["avg"], t["max"]) == (60.0, 70.0, 80.0)
    assert client.post(f"{API}/batches/{s.json()['id']}/end", headers=su).status_code == 409
    for fmt in ("pdf", "xlsx", "csv"):
        r = client.get(f"{API}/batches/{s.json()['id']}/report", headers=su, params={"format": fmt})
        assert r.status_code == 200 and len(r.content) > 50
    assert "/" not in client.get(f"{API}/batches/{s.json()['id']}/report", headers=su, params={"format": "csv"}).headers["content-disposition"].split("filename=")[1].strip('"')
    assert client.post(f"{API}/batches/start", headers=co["guest"], json={"device_id": d, "batch_no": "B3"}).status_code == 403
    assert len(client.get(f"{API}/batches", headers=su, params={"device_id": d}).json()) == 1


# ------------------------------------------------------------------ saved screens
def test_screens(client, co):
    su, d = co["su"], co["dev"]
    g = client.get(f"{API}/screens/process/{d}", headers=co["guest"], params={"template": "boiler"}).json()
    assert g["source"] == "template" and any(o["type"] == "flame" for o in g["config"]["objects"])
    auto = client.get(f"{API}/screens/process/{d}", headers=co["guest"]).json()                    # no hint: this pump has its own sensors -> a tile per sensor
    assert auto["source"] == "template" and {o["bind"]["metric"] for o in auto["config"]["objects"]} == {"speed", "run", "temp"}
    cfg = {"objects": [{"type": "tank", "bind": {"metric": "speed"}, "x": 5, "y": 5}]}
    assert client.put(f"{API}/screens/process/{d}", headers=co["guest"], json={"config": cfg}).status_code == 403
    assert client.put(f"{API}/screens/process/{d}", headers=su, json={"config": {"objects": [{"type": "bomb"}]}}).status_code == 422
    assert client.put(f"{API}/screens/process/{d}", headers=su, json={"config": cfg}).json()["source"] == "device"
    assert client.get(f"{API}/screens/process/{d}", headers=su).json()["config"] == cfg
    assert client.delete(f"{API}/screens/process/{d}", headers=su).json()["reset"] is True
    assert client.get(f"{API}/screens/process/{d}", headers=su).json()["source"] == "template"
    h = client.post(f"{API}/screens/hmi", headers=su, json={"name": "Boiler house", "config": {"objects": [{"type": "lamp", "x": 1, "y": 2}]}})
    assert h.status_code == 201
    assert client.post(f"{API}/screens/hmi", headers=su, json={"name": "Boiler house", "config": {"objects": []}}).status_code == 409
    assert client.post(f"{API}/screens/hmi", headers=co["op"], json={"name": "x", "config": {"objects": []}}).status_code == 403
    assert [x["name"] for x in client.get(f"{API}/screens/hmi", headers=co["op"]).json()] == ["Boiler house"]
    assert client.put(f"{API}/screens/hmi/{h.json()['id']}", headers=su, json={"name": "Boiler house 2", "config": {"objects": []}}).json()["name"] == "Boiler house 2"
    assert client.delete(f"{API}/screens/hmi/{h.json()['id']}", headers=su).status_code == 200


def test_device_from_a_template_gets_its_own_scene(client, co):
    su = co["su"]
    me = client.get(f"{API}/auth/me", headers=su).json()
    r = client.post(f"{API}/setup/templates/boiler/apply", headers=su, json={"device_name": "Boiler 1", "protocol": "modbus tcp", "ip_address": "10.0.0.20", "port": 502,
                                                                                "unit_id": 1, "state": me["state"], "region": "south", "plant_name": "Plant A"})
    assert r.status_code == 201, r.text
    scene = client.get(f"{API}/screens/process/{r.json()['id']}", headers=su).json()["config"]["objects"]
    assert any(o["type"] == "flame" for o in scene)                                  # recognised as a boiler from its sensor names, no hint needed


def test_every_template_has_an_animation_scene():
    import json as j, os
    here = os.path.dirname(os.path.dirname(__file__))
    tpl = j.load(open(os.path.join(here, "app/data/industry_templates.json")))
    anim = j.load(open(os.path.join(here, "app/data/animations.json")))
    for key, t in tpl.items():
        assert key in anim, key
        roles = {s["sensor_type"] for s in t["sensors"]}
        assert all(o["bind"]["sensor_type"] in roles for o in anim[key]), key        # every animated object has a sensor to follow


# ------------------------------------------------------------------ energy meter
def test_energy_page_and_documents(client, co):
    from app.db.session import MasterSession, tenant_session
    from app.models.tenant import DeviceRegister, Telemetry
    su = co["su"]
    r = client.post(f"{API}/devices", headers=su, json={
        "device_name": "Main meter", "protocol": "Modbus TCP", "ip_address": "10.0.0.9", "port": 502, "unit_id": 1, "state": client.get(f"{API}/auth/me", headers=su).json()["state"],
        "region": "South", "registers": [
            {"metric_name": "kwh", "data_type": "float32", "register_type": "holding", "register_address": 0, "unit": "kWh", "sensor_type": "energy"},
            {"metric_name": "kw", "data_type": "float32", "register_type": "holding", "register_address": 2, "unit": "kW", "sensor_type": "power"}]})
    assert r.status_code == 201, r.text
    dev = r.json()["id"]
    with MasterSession() as m, tenant_session(m, co["name"]) as db:
        now = utcnow()
        for i, v in enumerate((100.0, 110.0, 125.0)):
            db.add(Telemetry(device_id=dev, metric_name="kwh", value=v, timestamp=now - timedelta(minutes=30 - i)))
        db.add(Telemetry(device_id=dev, metric_name="kw", value=12.5, timestamp=now))
        db.commit()
    live = client.get(f"{API}/energy/{dev}/live", headers=co["guest"]).json()
    assert set(live["roles_present"]) == {"energy", "power"} and {t["metric"] for t in live["tiles"]} == {"kwh", "kw"}
    for kind in ("daily", "monthly", "raw", "alarms"):
        for fmt in ("csv", "xlsx", "pdf"):
            x = client.get(f"{API}/energy/{dev}/report", headers=su, params={"kind": kind, "format": fmt})
            assert x.status_code == 200, (kind, fmt, x.text)
    csv = client.get(f"{API}/energy/{dev}/report", headers=su, params={"kind": "daily", "format": "csv"}).text
    assert "25.0" in csv                                                                  # 125 - 100 kWh in the day
    # a device with no energy sensor cannot make the energy documents
    assert client.get(f"{API}/energy/{co['dev']}/report", headers=su, params={"kind": "daily", "format": "csv"}).json()["error"]["code"] == "NO_ENERGY_SENSOR"
