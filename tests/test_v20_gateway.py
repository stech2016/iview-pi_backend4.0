"""V2.0 - customer-owned gateways (any brand): JSON + Sparkplug B intake, Detected list, plant logins, scope, write path."""
import json
import time
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.timeutil import utcnow
from app.main import app
from app.services import brokeradmin, extgw, sparkplug as sp
from tests.helpers import API, PW, STATE, login, make_user, signup_owner


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# =============================================================================================== Sparkplug B
def test_sparkplug_round_trip_all_types():
    ms = [sp.Metric("a/Temp", alias=1, datatype=sp.DOUBLE, value=55.25), sp.Metric("Run", alias=2, datatype=sp.BOOLEAN, value=True),
          sp.Metric("Cnt", alias=3, datatype=sp.INT32, value=-5), sp.Metric("Big", alias=4, datatype=sp.INT64, value=-2 ** 40),
          sp.Metric("F", alias=5, datatype=sp.FLOAT, value=1.5), sp.Metric("U", alias=6, datatype=sp.UINT16, value=65535),
          sp.Metric("S", datatype=sp.STRING, value="hi"), sp.Metric("I8", datatype=sp.INT8, value=-128), sp.Metric("N", datatype=sp.DOUBLE, value=None)]
    out = sp.decode(sp.encode(ms, seq=7)).metrics
    assert [m.value for m in out] == [55.25, True, -5, -2 ** 40, 1.5, 65535, "hi", -128, None]
    assert out[0].name == "a/Temp" and out[0].alias == 1 and out[8].is_null
    assert out[1].as_number() == 1.0 and out[6].as_number() is None and out[8].as_number() is None


def test_sparkplug_is_compatible_with_an_independent_protobuf_implementation():
    pb = pytest.importorskip("google.protobuf.descriptor_pb2")
    from google.protobuf import descriptor_pool, message_factory
    F = pb.FieldDescriptorProto
    fd = pb.FileDescriptorProto(name="sp_t.proto", package="spt", syntax="proto2")
    m = fd.message_type.add(name="Metric")
    for name, num, typ in [("name", 1, F.TYPE_STRING), ("alias", 2, F.TYPE_UINT64), ("timestamp", 3, F.TYPE_UINT64), ("datatype", 4, F.TYPE_UINT32),
                           ("is_null", 7, F.TYPE_BOOL), ("int_value", 10, F.TYPE_UINT32), ("long_value", 11, F.TYPE_UINT64), ("float_value", 12, F.TYPE_FLOAT),
                           ("double_value", 13, F.TYPE_DOUBLE), ("boolean_value", 14, F.TYPE_BOOL), ("string_value", 15, F.TYPE_STRING)]:
        m.field.add(name=name, number=num, type=typ, label=F.LABEL_OPTIONAL)
    p = fd.message_type.add(name="Payload")
    p.field.add(name="timestamp", number=1, type=F.TYPE_UINT64, label=F.LABEL_OPTIONAL)
    p.field.add(name="metrics", number=2, type=F.TYPE_MESSAGE, label=F.LABEL_REPEATED, type_name=".spt.Metric")
    p.field.add(name="seq", number=3, type=F.TYPE_UINT64, label=F.LABEL_OPTIONAL)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(fd)
    P = message_factory.GetMessageClass(pool.FindMessageTypeByName("spt.Payload"))
    theirs = P(timestamp=123, seq=9)
    theirs.metrics.add(name="T", alias=4, datatype=10, double_value=-3.5)
    theirs.metrics.add(name="C", datatype=3, int_value=(-7) & 0xFFFFFFFF)
    theirs.metrics.add(name="B", datatype=11, boolean_value=True)
    theirs.metrics.add(name="L", datatype=4, long_value=(-9) & (2 ** 64 - 1))
    got = sp.decode(theirs.SerializeToString())
    assert [(x.name, x.value) for x in got.metrics] == [("T", -3.5), ("C", -7), ("B", True), ("L", -9)] and got.seq == 9 and got.timestamp == 123
    mine = P()
    mine.ParseFromString(sp.encode([sp.Metric("Z", alias=1, datatype=sp.INT16, value=-300), sp.Metric("Q", datatype=sp.DOUBLE, value=2.5)], seq=3))
    assert mine.metrics[0].name == "Z" and mine.metrics[0].int_value == (-300) & 0xFFFFFFFF and mine.metrics[1].double_value == 2.5 and mine.seq == 3


def test_sparkplug_garbage_and_topics():
    for bad in (b"", b"\xff\xff\xff", b"\x12\x05ab", b"\x08"):
        with pytest.raises(sp.SparkplugError):
            sp.decode(bad)
    assert sp.parse_topic("spBv1.0/g/DDATA/e/d").device == "d" and sp.parse_topic("spBv1.0/g/NDATA/e").device is None
    assert sp.parse_topic("spBv1.0/g/NDATA/e/d") is None and sp.parse_topic("spBv1.0/g/DDATA/e") is None and sp.parse_topic("x/g/NDATA/e") is None


# =============================================================================================== JSON layouts
def _read(rest, body, dev="Gateway"):
    return [(r.device, r.tag, r.value) for r in extgw.parse_json(rest.split("/") if rest else [], body, dev)]


def test_json_layouts_are_detected():
    assert _read("Boiler1", {"Temp": 55.1, "Press": "2.5", "Run": True, "name": "text", "ts": "2026-01-01T00:00:00Z"}) == \
        [("Boiler1", "Temp", 55.1), ("Boiler1", "Press", 2.5), ("Boiler1", "Run", 1.0)]
    assert _read("", [{"name": "Temp", "value": 1}, {"n": "Run", "v": False}, {"tag": "Bad", "value": "x"}]) == [("Gateway", "Temp", 1.0), ("Gateway", "Run", 0.0)]
    assert _read("", {"device": "Pump 3", "ts": 1767225600, "tags": [{"id": "Flow", "value": 9.5}]}) == [("Pump 3", "Flow", 9.5)]
    assert _read("", {"device": "Pump 3", "data": {"Flow": 9.5, "Motor": {"Amps": 3.1}}}) == [("Pump 3", "Flow", 9.5), ("Pump 3", "Motor.Amps", 3.1)]
    assert _read("Boiler1/Temp", 55.5) == [("Boiler1", "Temp", 55.5)] and _read("Temp", "7") == [("Gateway", "Temp", 7.0)]
    assert _read("Boiler1/Temp", {"value": 3}) == [("Boiler1", "Temp", 3.0)]
    assert _read("Boiler1/data", {"temp": 1}) == [("Boiler1", "temp", 1.0)]
    assert _read("Boiler1", {"a": float("nan"), "b": "inf", "c": [1, 2], "d": None}) == []
    assert _read("Boiler1", "hello") == [] and _read("", 5) == []
    assert len(_read("D", {f"t{i}": i for i in range(900)})) == extgw.MAX_TAGS_PER_MESSAGE
    assert _read("D", {"x" * 65: 1, "ok": 2}) == [("D", "ok", 2.0)]          # a name that is too long is skipped, never cut


def test_future_and_odd_timestamps_fall_back_to_arrival_time():
    assert extgw._when("2999-01-01T00:00:00Z") is None and extgw._when("nonsense") is None and extgw._when(True) is None
    assert extgw._when(1767225600).year == 2026 and extgw._when(1767225600000).year == 2026
    assert extgw._when("2026-01-01T05:00:00+05:30").hour == 23


def test_topic_routing():
    P = "pl0123456789"
    j = lambda t, b: extgw.parse_message(t, b if isinstance(b, bytes) else json.dumps(b).encode())  # noqa: E731
    assert j(f"iview/{P}/B1", {"T": 1})[0] == P
    assert j(f"iview/{P}/cmd/B1/T", {"value": 1}) is None and j(f"iview/{P}/cmdack/x", {}) is None          # the cloud's own commands are not data
    assert j("iview/notaplant/B1", {"T": 1}) is None and j("other/x", b"1") is None and j("iview", b"1") is None
    assert j(f"iview/{P}/status", b"offline")[3] == {"status": False} and j(f"iview/{P}/status", {"online": True})[3] == {"status": True}
    assert j(f"spBv1.0/{P}/NDEATH/e1", b"")[3]["death"] is True
    assert j(f"spBv1.0/{P}/NCMD/e1", b"") is None and j(f"spBv1.0/{P}/DCMD/e1/d", b"") is None
    assert j(f"spBv1.0/{P}/NDATA/e1", b"\xff\xff") is None


def test_sparkplug_aliases_and_rebirth(monkeypatch):
    P, asked = "plabcdef0123", []
    monkeypatch.setattr(extgw, "request_rebirth", lambda plant, edge: asked.append((plant, edge)))
    birth = sp.encode([sp.Metric("Boiler/Temp", alias=10, datatype=sp.DOUBLE, value=50.0), sp.Metric("Node Control/Rebirth", datatype=sp.BOOLEAN, value=False),
                       sp.Metric("Run", alias=11, datatype=sp.BOOLEAN, value=True)], seq=0)
    plant, fmt, readings, extra = extgw.parse_message(f"spBv1.0/{P}/DBIRTH/edge1/dev1", birth)
    assert fmt == "sparkplug" and [(r.device, r.tag, r.value) for r in readings] == [("dev1", "Boiler/Temp", 50.0), ("dev1", "Run", 1.0)]
    data = sp.encode([sp.Metric(alias=10, datatype=sp.DOUBLE, value=51.5)], seq=1)
    assert [(r.tag, r.value) for r in extgw.parse_message(f"spBv1.0/{P}/DDATA/edge1/dev1", data)[2]] == [("Boiler/Temp", 51.5)]
    unknown = sp.encode([sp.Metric(alias=99, datatype=sp.DOUBLE, value=1.0)], seq=2)
    assert extgw.parse_message(f"spBv1.0/{P}/DDATA/edge1/dev1", unknown)[3]["rebirth"] == "edge1"
    assert extgw.parse_message(f"spBv1.0/{P}/NDATA/edge1", sp.encode([sp.Metric("Up", datatype=sp.INT32, value=3)]))[2][0].device == "edge1"   # node-level metrics


# =============================================================================================== full flow with the app
@pytest.fixture()
def broker(monkeypatch):
    calls = []
    monkeypatch.setattr(brokeradmin, "create_plant", lambda pid, pw: calls.append(("create", pid, pw)))
    monkeypatch.setattr(brokeradmin, "set_password", lambda pid, pw: calls.append(("password", pid, pw)))
    monkeypatch.setattr(brokeradmin, "set_enabled", lambda pid, on: calls.append(("enabled", pid, on)))
    monkeypatch.setattr(brokeradmin, "delete_plant", lambda pid: calls.append(("delete", pid)))
    return calls


@pytest.fixture(scope="module")
def co(client):
    name = signup_owner(client)
    su = login(client, name + "_su")
    admin, _ = make_user(client, su, name + "_ad", "Admin")                      # Regional admin of Tamil Nadu / South
    north, _ = make_user(client, su, name + "_no", "Admin", region="North")      # Regional admin of another region
    op, _ = make_user(client, su, name + "_op", "Operator")
    guest, _ = make_user(client, su, name + "_gu", "Guest")
    return {"name": name, "su": su, "admin": admin, "north": north, "op": op, "guest": guest}


def _add(client, who, state=STATE, region="South", name="Chennai boiler house", **kw):
    r = client.post(f"{API}/plants", headers=who, json={"state": state, "region": region, "name": name, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _feed(topic, body):
    extgw.process([(topic, body if isinstance(body, bytes) else json.dumps(body).encode())])


def _devices(client, who):
    return {d["device_name"]: d for d in client.get(f"{API}/devices", headers=who).json()}


def _latest(client, who, device_id):
    r = client.get(f"{API}/devices/{device_id}/latest", headers=who)
    assert r.status_code == 200, r.text
    return {x["metric_name"]: x["value"] for x in r.json()}


def test_add_gateway_gives_the_login_once(client, co, broker):
    made = _add(client, co["su"])
    pid, conn = made["plant"]["plant_id"], made["connection"]
    assert broker[0][:2] == ("create", pid) and broker[0][2] == conn["password"] and len(conn["password"]) >= 20
    assert conn["port"] == 8883 and conn["tls"] and conn["username"] == pid and conn["client_id"] == pid
    assert conn["json"]["topic_prefix"] == f"iview/{pid}" and conn["sparkplug_b"]["group_id"] == pid
    again = client.get(f"{API}/plants/{pid}/connection", headers=co["su"]).json()["connection"]
    assert again["password"] is None and again["username"] == pid                       # the password is never shown again
    reset = client.post(f"{API}/plants/{pid}/reset-password", headers=co["su"]).json()
    assert broker[-1] == ("password", pid, reset["connection"]["password"]) and reset["connection"]["password"] != conn["password"]


def test_scope_and_roles(client, co, broker):
    made = _add(client, co["su"], name="South plant")
    pid = made["plant"]["plant_id"]
    other = _add(client, co["su"], region="North", name="North plant")["plant"]["plant_id"]
    names = lambda who: {p["name"] for p in client.get(f"{API}/plants", headers=who).json()["plants"]}  # noqa: E731
    assert {"South plant", "North plant"} <= names(co["su"])
    assert "South plant" in names(co["admin"]) and "North plant" not in names(co["admin"])
    assert client.get(f"{API}/plants/{other}/connection", headers=co["admin"]).status_code == 403
    assert client.post(f"{API}/plants/{other}/reset-password", headers=co["admin"]).status_code == 403
    assert client.get(f"{API}/plants/{pid}/detected", headers=co["north"]).status_code == 403
    assert client.post(f"{API}/plants", headers=co["admin"], json={"state": STATE, "region": "North", "name": "x"}).status_code == 403
    assert client.post(f"{API}/plants", headers=co["admin"], json={"state": STATE, "region": "South", "name": "by admin"}).status_code == 201
    for who in ("op", "guest"):
        assert client.get(f"{API}/plants", headers=co[who]).status_code == 403
        assert client.post(f"{API}/plants", headers=co[who], json={"state": STATE, "region": "South"}).status_code == 403
    assert client.get(f"{API}/plants/plffffffffff/connection", headers=co["su"]).status_code == 404


def test_detected_list_then_tick_then_values(client, co, broker):
    pid = _add(client, co["su"], name="Detect plant")["plant"]["plant_id"]
    # 1+2: the gateway sends many points; they are only LISTED
    _feed(f"iview/{pid}/Boiler1", {"Steam_Temp": 182.4, "Pressure": 9.1, "Pump_Run": True, "Spare1": 0, "Spare2": 0})
    _feed(f"iview/{pid}/Boiler2", {"Steam_Temp": 150.0})
    items = client.get(f"{API}/plants/{pid}/detected", headers=co["su"]).json()["items"]
    assert {(i["device"], i["tag"]) for i in items} == {("Boiler1", "Steam_Temp"), ("Boiler1", "Pressure"), ("Boiler1", "Pump_Run"), ("Boiler1", "Spare1"),
                                                       ("Boiler1", "Spare2"), ("Boiler2", "Steam_Temp")}
    assert next(i for i in items if i["tag"] == "Pump_Run")["kind"] == "bool"
    assert "Boiler1" not in _devices(client, co["su"]) and "Boiler1" not in _devices(client, co["op"])      # not in the app yet
    # 3: a Regional admin of that region ticks and names; nobody else can
    pick = {i["tag"]: i["id"] for i in items if i["device"] == "Boiler1"}
    body = {"items": [{"id": pick["Steam_Temp"], "name": "Steam temperature", "unit": "C", "device_name": "Boiler 1"},
                      {"id": pick["Pressure"], "name": "Steam pressure", "unit": "bar", "device_name": "Boiler 1"},
                      {"id": pick["Pump_Run"], "name": "Pump running", "device_name": "Boiler 1"}]}
    assert client.post(f"{API}/plants/{pid}/detected/accept", headers=co["north"], json=body).status_code == 403
    assert client.post(f"{API}/plants/{pid}/detected/accept", headers=co["op"], json=body).status_code == 403
    got = client.post(f"{API}/plants/{pid}/detected/accept", headers=co["admin"], json=body)
    assert got.status_code == 200 and got.json()["sensors_added"] == 3 and got.json()["devices"] == ["Boiler 1"], got.text
    # 4: unticked stay hidden; dismissed ones leave the list
    assert client.post(f"{API}/plants/{pid}/detected/dismiss", headers=co["admin"], json={"ids": [pick["Spare1"]]}).json()["dismissed"] == 1
    left = {i["tag"] for i in client.get(f"{API}/plants/{pid}/detected", headers=co["su"]).json()["items"]}
    assert left == {"Spare2", "Steam_Temp"} and "Spare1" in {i["tag"] for i in client.get(f"{API}/plants/{pid}/detected?show=dismissed", headers=co["su"]).json()["items"]}
    assert client.post(f"{API}/plants/{pid}/detected/restore", headers=co["admin"], json={"ids": [pick["Spare1"]]}).json()["restored"] == 1
    # the chosen device now works like any other: values, scope
    time.sleep(0.01)
    _feed(f"iview/{pid}/Boiler1", {"Steam_Temp": 190.5, "Pressure": 9.4, "Pump_Run": False, "Spare1": 5, "Spare2": 6})
    dev = _devices(client, co["op"])["Boiler 1"]                           # an Operator of that region sees it ...
    assert dev["protocol"] == "MQTT" and dev["state"] == STATE and dev["region"] == "South"
    assert "Boiler 1" not in _devices(client, co["north"])                 # ... a person of another region does not
    values = _latest(client, co["op"], dev["id"])
    assert values == {"Steam_temperature": 190.5, "Steam_pressure": 9.4, "Pump_running": 0.0}      # the unchosen Spare1 / Spare2 are not stored
    assert "Spare2" in {i["tag"] for i in client.get(f"{API}/plants/{pid}/detected", headers=co["su"]).json()["items"]}
    # ticking an already ticked name again is refused politely; another tick adds to the same device
    again = client.post(f"{API}/plants/{pid}/detected/accept", headers=co["su"],
                        json={"items": [{"id": pick["Spare2"], "name": "Steam_pressure", "device_name": "Boiler 1"}]}).json()
    assert again["sensors_added"] == 0 and "already exists" in again["skipped"][0]["why"]
    more = client.post(f"{API}/plants/{pid}/detected/accept", headers=co["su"], json={"items": [{"id": pick["Spare2"], "name": "Spare two", "device_name": "Boiler 1"}]}).json()
    assert more["sensors_added"] == 1


def test_first_value_is_shown_at_once_when_the_gateway_reports_only_changes(client, co, broker):
    pid = _add(client, co["su"], name="Quiet plant")["plant"]["plant_id"]
    _feed(f"iview/{pid}/Tank", {"Level": 61.5})
    item = client.get(f"{API}/plants/{pid}/detected", headers=co["su"]).json()["items"][0]
    client.post(f"{API}/plants/{pid}/detected/accept", headers=co["su"], json={"items": [{"id": item["id"], "name": "Level", "device_name": "Tank 1"}]})
    assert _latest(client, co["su"], _devices(client, co["su"])["Tank 1"]["id"]) == {"Level": 61.5}


def test_link_goes_offline_with_an_alarm_and_comes_back(client, co, broker):
    pid = _add(client, co["su"], name="Alarm plant")["plant"]["plant_id"]
    _feed(f"iview/{pid}/D1", {"x": 1})
    row = lambda: next(p for p in client.get(f"{API}/plants", headers=co["su"]).json()["plants"] if p["plant_id"] == pid)  # noqa: E731
    assert row()["online"] is True and row()["format"] == "json"
    _feed(f"iview/{pid}/status", b"offline")                                  # the MQTT will message / a clean goodbye
    assert row()["online"] is False and row()["state_text"] == "offline"
    alarms = client.get(f"{API}/alarms", headers=co["su"]).json()
    items = alarms["items"] if isinstance(alarms, dict) else alarms
    assert any("Alarm plant" in json.dumps(a) and not a.get("cleared_at") for a in items)
    _feed(f"iview/{pid}/D1", {"x": 2})
    assert row()["online"] is True
    items = client.get(f"{API}/alarms", headers=co["su"]).json()
    items = items["items"] if isinstance(items, dict) else items
    assert not any("Alarm plant" in json.dumps(a) and not a.get("cleared_at") for a in items)


def test_silent_link_is_marked_offline_by_the_watchdog(client, co, broker, monkeypatch):
    pid = _add(client, co["su"], name="Silent plant")["plant"]["plant_id"]
    _feed(f"iview/{pid}/D1", {"x": 1})
    from app.db.session import MasterSession
    from app.models.master import PlantLink
    with MasterSession() as m:
        r = m.scalar(select(PlantLink).where(PlantLink.plant_id == pid))
        r.last_seen = utcnow() - timedelta(seconds=extgw.offline_after("") + 5)
        m.commit()
    assert extgw.is_online(r) is False
    monkeypatch.setattr(extgw.time, "sleep", lambda s: (_ for _ in ()).throw(StopIteration()))
    with pytest.raises(StopIteration):
        extgw.watchdog()
    assert next(p for p in client.get(f"{API}/plants", headers=co["su"]).json()["plants"] if p["plant_id"] == pid)["online"] is False


def test_disabled_gateway_is_ignored_and_removed_cleanly(client, co, broker):
    pid = _add(client, co["su"], name="Off plant")["plant"]["plant_id"]
    assert client.post(f"{API}/plants/{pid}/enable", headers=co["su"], json={"enabled": False}).status_code == 200
    assert broker[-1] == ("enabled", pid, False)
    extgw.forget_link(pid)
    _feed(f"iview/{pid}/D1", {"x": 1})
    assert client.get(f"{API}/plants/{pid}/detected", headers=co["su"]).json()["items"] == []
    assert client.delete(f"{API}/plants/{pid}", headers=co["su"]).status_code == 200 and broker[-1] == ("delete", pid)
    assert client.get(f"{API}/plants/{pid}/connection", headers=co["su"]).status_code == 404


def test_detected_list_is_capped(client, co, broker, monkeypatch):
    pid = _add(client, co["su"], name="Flood plant")["plant"]["plant_id"]
    monkeypatch.setattr(extgw, "MAX_DETECTED_PER_PLANT", 10)
    _feed(f"iview/{pid}/D1", {f"t{i}": i for i in range(30)})
    res = client.get(f"{API}/plants/{pid}/detected", headers=co["su"]).json()
    assert len(res["items"]) == 10 and res["limit_reached"] is True


def test_a_gateway_cannot_write_into_another_plants_devices(client, co, broker):
    a = _add(client, co["su"], name="Plant A")["plant"]["plant_id"]
    b = _add(client, co["su"], name="Plant B")["plant"]["plant_id"]
    _feed(f"iview/{a}/Dev", {"T": 1})
    it = client.get(f"{API}/plants/{a}/detected", headers=co["su"]).json()["items"][0]
    client.post(f"{API}/plants/{a}/detected/accept", headers=co["su"], json={"items": [{"id": it["id"], "name": "T", "device_name": "Dev A"}]})
    _feed(f"iview/{b}/Dev", {"T": 99})                      # plant B sends the same names: only B's own Detected list changes
    assert _latest(client, co["su"], _devices(client, co["su"])["Dev A"]["id"]) == {"T": 1.0}
    assert [i["tag"] for i in client.get(f"{API}/plants/{b}/detected", headers=co["su"]).json()["items"]] == ["T"]


# =============================================================================================== writing
class Sent:
    def __init__(self, monkeypatch):
        from app.services import gwcontrol
        self.msgs = []
        monkeypatch.setattr(gwcontrol, "publish_message", lambda topic, body: self.msgs.append((topic, body)) or True)


def _writable_setup(client, co, pid, fmt_json=True):
    if fmt_json:
        _feed(f"iview/{pid}/PLC1", {"SetPoint": 40.0, "Pump": False})
    items = {i["tag"]: i["id"] for i in client.get(f"{API}/plants/{pid}/detected", headers=co["su"]).json()["items"]}
    r = client.post(f"{API}/plants/{pid}/detected/accept", headers=co["su"], json={"items": [
        {"id": items["SetPoint"], "name": "Set_point", "device_name": "PLC 1"}, {"id": items["Pump"], "name": "Pump", "device_name": "PLC 1"}]})
    assert r.json()["sensors_added"] == 2, r.text
    dev = _devices(client, co["su"])["PLC 1"]
    regs = {r["metric_name"]: r["id"] for r in client.get(f"{API}/devices/{dev['id']}", headers=co["su"]).json()["registers"]}
    assert client.put(f"{API}/settings", headers=co["su"], json={"values": {"write_enabled": True}}).status_code == 200
    cfg = lambda name, **b: client.put(f"{API}/devices/{dev['id']}/registers/{regs[name]}/write-config", headers=co["su"], json=b)  # noqa: E731
    assert cfg("Set_point", writable=True, write_min=0, write_max=100).status_code == 200
    assert cfg("Pump", writable=True).status_code == 200
    return dev["id"], regs


def _send(client, co, dev, reg, value, who="su"):
    return client.post(f"{API}/control/commands", headers=co[who], json={"device_id": dev, "register_id": reg, "value": value, "confirm": True,
                                                                          "reason": "test change", "password": PW})


def test_write_json_sent_then_confirmed_by_next_reading(client, co, broker, monkeypatch):
    sent = Sent(monkeypatch)
    pid = _add(client, co["su"], name="Write plant")["plant"]["plant_id"]
    dev, regs = _writable_setup(client, co, pid)
    ready = client.get(f"{API}/control/readiness", params={"device_id": dev}, headers=co["su"]).json()
    assert ready["via"] == "customer_gateway" and ready["ready"] is True, ready
    assert _send(client, co, dev, regs["Set_point"], 500).json()["error"]["code"] == "VALUE_OUT_OF_LIMITS"
    r = _send(client, co, dev, regs["Set_point"], 55.5)
    assert r.status_code == 201 and r.json()["state"] == "sent", r.text
    topic, body = sent.msgs[-1]
    assert topic == f"iview/{pid}/cmd/PLC1/SetPoint" and json.loads(body)["value"] == 55.5 and json.loads(body)["tag"] == "SetPoint" and json.loads(body)["uid"] == r.json()["uid"]
    assert _send(client, co, dev, regs["Set_point"], 60).json()["error"]["code"] == "COMMAND_PENDING"
    _feed(f"iview/{pid}/PLC1", {"SetPoint": 40.0})                        # an old value: still waiting
    assert client.get(f"{API}/control/commands/{r.json()['id']}", headers=co["su"]).json()["state"] == "sent"
    _feed(f"iview/{pid}/PLC1", {"SetPoint": 55.5})                        # the gateway wrote it to the PLC and reports it back
    done = client.get(f"{API}/control/commands/{r.json()['id']}", headers=co["su"]).json()
    assert done["state"] == "confirmed" and done["readback"] == 55.5
    for who in ("op", "guest"):
        assert _send(client, co, dev, regs["Set_point"], 50, who=who).status_code == 403


def test_write_times_out_when_the_gateway_never_reports_the_value(client, co, broker, monkeypatch):
    Sent(monkeypatch)
    pid = _add(client, co["su"], name="Timeout plant")["plant"]["plant_id"]
    dev, regs = _writable_setup(client, co, pid)
    r = _send(client, co, dev, regs["Pump"], 1)
    assert r.status_code == 201
    from app.db.session import MasterSession, tenant_session
    from app.models.tenant import Command
    with MasterSession() as m, tenant_session(m, co["name"]) as db:
        c = db.scalar(select(Command).where(Command.uid == r.json()["uid"]))
        c.expires_at = utcnow() - timedelta(seconds=60)
        db.commit()
    state = client.get(f"{API}/control/commands/{r.json()['id']}", headers=co["su"]).json()
    assert state["state"] == "timed_out" and "does not accept writes" in state["error"] or "may not accept writes" in state["error"]


def test_write_refused_when_the_gateway_is_offline(client, co, broker, monkeypatch):
    Sent(monkeypatch)
    pid = _add(client, co["su"], name="Offline write plant")["plant"]["plant_id"]
    dev, regs = _writable_setup(client, co, pid)
    _feed(f"iview/{pid}/status", b"offline")
    r = _send(client, co, dev, regs["Pump"], 1)
    assert r.status_code == 409 and r.json()["error"]["code"] == "PLANT_OFFLINE"
    assert client.get(f"{API}/control/readiness", params={"device_id": dev}, headers=co["su"]).json()["ready"] is False


def test_write_sparkplug_uses_dcmd_with_the_right_datatype(client, co, broker, monkeypatch):
    sent = Sent(monkeypatch)
    pid = _add(client, co["su"], name="Spark plant")["plant"]["plant_id"]
    _feed(f"spBv1.0/{pid}/DBIRTH/EdgeA/Press1", sp.encode([sp.Metric("Speed", alias=1, datatype=sp.INT32, value=10), sp.Metric("Run", alias=2, datatype=sp.BOOLEAN, value=False)]))
    items = {i["tag"]: i["id"] for i in client.get(f"{API}/plants/{pid}/detected", headers=co["su"]).json()["items"]}
    assert client.post(f"{API}/plants/{pid}/detected/accept", headers=co["su"], json={"items": [
        {"id": items["Speed"], "name": "Speed", "device_name": "Press 1"}, {"id": items["Run"], "name": "Run", "device_name": "Press 1"}]}).json()["sensors_added"] == 2
    dev = _devices(client, co["su"])["Press 1"]
    regs = {r["metric_name"]: r for r in client.get(f"{API}/devices/{dev['id']}", headers=co["su"]).json()["registers"]}
    assert regs["Run"]["data_type"] == "bool"                                 # a Sparkplug Boolean becomes an on / off point
    client.put(f"{API}/settings", headers=co["su"], json={"values": {"write_enabled": True}})
    assert client.put(f"{API}/devices/{dev['id']}/registers/{regs['Run']['id']}/write-config", headers=co["su"], json={"writable": True}).status_code == 200
    r = _send(client, co, dev["id"], regs["Run"]["id"], 1)
    assert r.status_code == 201, r.text
    topic, body = sent.msgs[-1]
    assert topic == f"spBv1.0/{pid}/DCMD/EdgeA/Press1"
    m = sp.decode(body).metrics[0]
    assert m.name == "Run" and m.datatype == sp.BOOLEAN and m.value is True
    # a later Sparkplug data message (names via aliases) confirms it
    _feed(f"spBv1.0/{pid}/DDATA/EdgeA/Press1", sp.encode([sp.Metric(alias=2, datatype=sp.BOOLEAN, value=True)], seq=1))
    assert client.get(f"{API}/control/commands/{r.json()['id']}", headers=co["su"]).json()["state"] == "confirmed"


def test_command_format_can_be_forced_to_json(client, co, broker, monkeypatch):
    sent = Sent(monkeypatch)
    pid = _add(client, co["su"], name="Force json")["plant"]["plant_id"]
    _feed(f"spBv1.0/{pid}/DBIRTH/E/D", sp.encode([sp.Metric("V", alias=1, datatype=sp.DOUBLE, value=1.0)]))
    it = client.get(f"{API}/plants/{pid}/detected", headers=co["su"]).json()["items"][0]
    client.post(f"{API}/plants/{pid}/detected/accept", headers=co["su"], json={"items": [{"id": it["id"], "name": "V", "device_name": "DD"}]})
    dev = _devices(client, co["su"])["DD"]
    reg = client.get(f"{API}/devices/{dev['id']}", headers=co["su"]).json()["registers"][0]["id"]
    client.put(f"{API}/settings", headers=co["su"], json={"values": {"write_enabled": True}})
    client.put(f"{API}/devices/{dev['id']}/registers/{reg}/write-config", headers=co["su"], json={"writable": True, "write_min": 0, "write_max": 10})
    assert client.patch(f"{API}/plants/{pid}", headers=co["su"], json={"cmd_format": "json"}).json()["plant"]["cmd_format"] == "json"
    extgw.forget_link(pid)
    assert _send(client, co, dev["id"], reg, 5).status_code == 201
    assert sent.msgs[-1][0] == f"iview/{pid}/cmd/D/V"
    assert client.patch(f"{API}/plants/{pid}", headers=co["su"], json={"cmd_format": "nonsense"}).status_code == 422


def test_existing_plant_pc_devices_and_hand_made_mqtt_devices_still_work(client, co):
    d = client.post(f"{API}/devices", headers=co["su"], json={
        "device_name": "Hand MQTT", "protocol": "MQTT", "ip_address": "broker.example", "port": 1883,
        "registers": [{"metric_name": "t", "data_type": "float32", "topic": f"stech/{co['name']}/hand/t"}]})
    assert d.status_code == 201, d.text
    m = client.post(f"{API}/devices", headers=co["su"], json={
        "device_name": "Plain Modbus", "protocol": "Modbus TCP", "ip_address": "10.5.5.5", "port": 502, "unit_id": 1,
        "registers": [{"metric_name": "m", "data_type": "float32", "register_type": "holding", "register_address": 0}]})
    assert m.status_code == 201


def test_a_chosen_gateway_device_can_still_be_edited_in_the_app(client, co, broker):
    pid = _add(client, co["su"], name="Edit plant")["plant"]["plant_id"]
    _feed(f"iview/{pid}/Fan", {"Speed rpm": 900.0})
    it = client.get(f"{API}/plants/{pid}/detected", headers=co["su"]).json()["items"][0]
    client.post(f"{API}/plants/{pid}/detected/accept", headers=co["su"], json={"items": [{"id": it["id"], "name": "Speed rpm", "device_name": "Fan 1"}]})
    dev = _devices(client, co["su"])["Fan 1"]
    full = client.get(f"{API}/devices/{dev['id']}", headers=co["su"]).json()
    reg = full["registers"][0]
    assert reg["metric_name"] == "Speed_rpm" and reg["topic"] == f"ext:{pid}:Fan" and reg["json_field"] == "Speed rpm"
    r = client.put(f"{API}/devices/{dev['id']}/registers/{reg['id']}", headers=co["su"], json={**reg, "unit": "rpm", "high_limit": 1500})
    assert r.status_code == 200, r.text
    assert client.put(f"{API}/devices/{dev['id']}", headers=co["su"], json={"city": "Chennai", "plant_name": "Edit plant"}).status_code == 200
    _feed(f"iview/{pid}/Fan", {"Speed rpm": 1600.0})                                    # above the limit he just set: an alarm is raised as for any sensor
    items = client.get(f"{API}/alarms", headers=co["su"]).json()
    items = items["items"] if isinstance(items, dict) else items
    assert any(a.get("metric_name") == "Speed_rpm" for a in items)


def test_summary_pill_counts_customer_gateways(client, co, broker):
    pid = _add(client, co["su"], name="Pill plant")["plant"]["plant_id"]
    before = client.get(f"{API}/gateway/summary", headers=co["su"]).json()
    assert before["customer_gateways"] >= 1 and "Pill plant" in before["waiting"] and before["all_connected"] is False
    _feed(f"iview/{pid}/D", {"x": 1})
    after = client.get(f"{API}/gateway/summary", headers=co["su"]).json()
    assert after["customer_gateways_connected"] == before["customer_gateways_connected"] + 1 and "Pill plant" not in after["waiting"]
    ra = client.get(f"{API}/gateway/summary", headers=co["admin"]).json()            # a Regional admin counts only his own region
    assert ra["customer_gateways"] <= after["customer_gateways"]


def test_server_readiness_check_names_the_problem(client, co):
    r = client.get(f"{API}/plants/health", headers=co["su"])
    assert r.status_code == 200 and r.json()["ready"] is False and r.json()["problems"]
    assert client.get(f"{API}/plants/health", headers=co["op"]).status_code == 403
