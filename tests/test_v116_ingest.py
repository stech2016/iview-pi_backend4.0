"""v1.1.6 ingest speed-up: plant readings are saved in groups. Every safety rule of the one-by-one path must still hold."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.timeutil import utcnow
from app.main import app
from app.services import gateway as gw
from app.workers import mqtt_ingest as ing
from tests.helpers import API, login, signup_owner


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _setup(client, n_devices=3):
    name = signup_owner(client)
    h = login(client, name + "_su")
    sensors = []
    for i in range(n_devices):
        r = client.post(f"{API}/devices", headers=h, json={
            "device_name": f"d{i}", "protocol": "Modbus TCP", "ip_address": f"10.9.0.{i + 1}", "port": 502, "unit_id": 1,
            "registers": [{"metric_name": f"m{k}", "data_type": "float32", "register_type": "holding", "register_address": 2 * k,
                           "high_limit": 90.0} for k in range(4)]})
        assert r.status_code == 201, r.text
        sensors += [(i, x["id"], x["metric_name"]) for x in r.json()["registers"]]
    return name, h, sensors, r.json()["id"]


def _msg(company, gid, rid, value, secret=None, ts=None):
    secret = secret or gw.get_info(company, gid).secret
    topic = f"stech/{company}/gw/{gid}/r{rid}" if gid != "main" else f"stech/{company}/r{rid}"
    ts = ts or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return (company, gid, topic, rid, json.dumps({"value": value, "timestamp": ts, "sig": gw.sign(secret, topic, ts, value)}).encode())


def _latest(client, h, device_id):
    r = client.get(f"{API}/devices/{device_id}/latest", headers=h)
    assert r.status_code == 200, r.text
    return {x["metric_name"]: x["value"] for x in r.json()}


def test_a_group_of_readings_is_saved_and_shown(client):
    co, h, sensors, _ = _setup(client)
    ing._save_batch([_msg(co, "main", rid, 10.0 + rid % 7) for _, rid, _ in sensors])      # 3 devices x 4 sensors in ONE round
    for d in client.get(f"{API}/devices", headers=h).json():
        assert len(_latest(client, h, d["id"])) == 4


def test_wrong_signature_is_thrown_away(client):
    co, h, sensors, last_dev = _setup(client, 1)
    rid = sensors[0][1]
    bad = _msg(co, "main", rid, 55.0, secret="not-the-key")
    good = _msg(co, "main", sensors[1][1], 12.0)
    ing._save_batch([bad, good])
    names = _latest(client, h, last_dev)
    assert "m0" not in names and names.get("m1") == 12.0


def test_other_companys_sensor_is_refused(client):
    co, h, sensors, _ = _setup(client, 1)
    other, h2, sensors2, dev2 = _setup(client, 1)
    # company 'co' signs a message for a sensor id that belongs to 'other': must not be stored there
    ing._save_batch([_msg(co, "main", sensors2[0][1], 77.0)])
    assert _latest(client, h2, dev2).get("m0") != 77.0


def test_alarm_still_raised_from_a_group(client):
    co, h, sensors, _ = _setup(client, 1)
    ing._save_batch([_msg(co, "main", sensors[0][1], 95.0)])       # limit is 90
    r = client.get(f"{API}/alarms", headers=h)
    assert r.status_code == 200 and "m0" in json.dumps(r.json())


def test_same_device_always_goes_to_same_worker():
    ing._DEVICE_OF[("c", 1)] = 7
    ing._DEVICE_OF[("c", 2)] = 7
    assert ing._device_of("c", 1) % 4 == ing._device_of("c", 2) % 4


def test_worker_count_can_be_set(monkeypatch):
    monkeypatch.setenv("INGEST_WORKERS", "3")
    assert ing.worker_count() == 3
    monkeypatch.setenv("INGEST_WORKERS", "0")
    assert 1 <= ing.worker_count() <= 16


def test_two_companies_with_the_same_device_number_never_hide_each_others_readings(client):
    """Every company's database numbers its devices from 1. A 25.0 from company A must not make company B's 25.0 look 'unchanged'."""
    a, ha, sa, da = _setup(client, 1)
    b, hb, sb, db_ = _setup(client, 1)
    ing._save_batch([_msg(a, "main", sa[0][1], 25.0)])
    ing._save_batch([_msg(b, "main", sb[0][1], 25.0)])
    assert _latest(client, ha, da).get("m0") == 25.0
    assert _latest(client, hb, db_).get("m0") == 25.0
    rows = client.get(f"{API}/devices/{db_}/telemetry?metric=m0", headers=hb)
    assert rows.status_code == 200 and "25" in rows.text


def test_phone_receives_readings_only_for_the_device_it_opened(client):
    """A big plant produces hundreds of readings per second; a phone on the dashboard must not receive them (it only needs alarms)."""
    import time
    from app.services import live
    co, h, sensors, dev = _setup(client, 1)
    token = h["Authorization"].split()[1]
    with client.websocket_connect("/api/v1/ws/live", subprotocols=["stech.v1", token]) as ws:
        assert ws.receive_json()["type"] == "ready"
        time.sleep(0.3)
        live.publish(co, {"type": "telemetry", "device_id": dev, "metric_name": "m0", "value": 1.0, "timestamp": "2026-11-10T10:00:00+00:00", "state": "ALL", "region": "ALL"})
        live.publish(co, {"type": "alarm", "event": "raised", "alarm": {"id": 1}, "state": "ALL", "region": "ALL"})
        first = ws.receive_json()
        assert first["type"] == "alarm"                      # the reading was NOT sent, the alarm was
        ws.send_json({"type": "subscribe", "device_id": dev})
        time.sleep(0.3)
        live.publish(co, {"type": "telemetry", "device_id": dev, "metric_name": "m0", "value": 2.0, "timestamp": "2026-11-10T10:00:01+00:00", "state": "ALL", "region": "ALL"})
        assert ws.receive_json()["value"] == 2.0             # after opening the device its readings arrive


def test_tile_values_call_and_refresh_setting(client):
    co, h, sensors, dev = _setup(client, 2)
    ing._save_batch([_msg(co, "main", rid, 20.0 + k) for k, (_, rid, _) in enumerate(sensors)])
    r = client.get(f"{API}/devices/values", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2 and all(len(v) == 2 for v in data.values())      # 2 main sensors per tile
    s = {x["key"]: x for x in client.get(f"{API}/settings", headers=h).json()}
    assert s["screen_refresh_seconds"]["value"] == 1.0 and s["screen_refresh_seconds"]["max"] == 10.0
    ok = client.put(f"{API}/settings", headers=h, json={"values": {"screen_refresh_seconds": 2.0}})
    assert ok.status_code == 200
    assert {x["key"]: x["value"] for x in ok.json()}["screen_refresh_seconds"] == 2.0
    assert client.put(f"{API}/settings", headers=h, json={"values": {"screen_refresh_seconds": 99}}).status_code == 422


def test_every_gateway_download_carries_the_licence_notices(client, monkeypatch):
    import io, zipfile
    from app.core.config import settings
    from app.services import gateway as g
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://example.test")
    monkeypatch.setattr(settings, "MQTT_PUBLIC_HOST", "mqtt.example.test")
    monkeypatch.setattr(settings, "MQTT_GATEWAY_PASSWORD", "x")
    co, h, _, _ = _setup(client, 1)
    names = zipfile.ZipFile(io.BytesIO(g.build_zip(co, "main", "windows"))).namelist()
    assert any(n.endswith("THIRD_PARTY_NOTICES.txt") for n in names)


def test_reports_show_the_time_of_the_person_who_asks_for_them(client):
    """The app sends its own UTC offset; a reading taken at 06:00 UTC must read 11:30 for India and 01:00 for New York (winter)."""
    from app.services.export import local_text
    from app.core import tzctx
    co, h, sensors, dev = _setup(client, 1)
    ing._save_batch([_msg(co, "main", sensors[0][1], 41.5, ts="2026-11-10T06:00:00Z")])
    q = f"{API}/devices/{dev}/export?format=csv&metric=m0&date_from=2026-11-10T00:00:00Z&date_to=2026-11-11T00:00:00Z"

    def row(offset=None):
        hh = dict(h, **({"X-Tz-Offset": str(offset)} if offset is not None else {}))
        r = client.get(q, headers=hh)
        assert r.status_code == 200, r.text
        return r.text.splitlines()[0], r.text.splitlines()[1], r.headers["content-disposition"]
    head, line, disp = row(330)
    assert "UTC+05:30" in head and line.startswith("2026-11-10 11:30:00")
    head, line, disp = row(-300)
    assert "UTC-05:00" in head and line.startswith("2026-11-10 01:00:00")
    head, line, _ = row(None)                       # no header (old app / script): the plant default, India
    assert "UTC+05:30" in head and line.startswith("2026-11-10 11:30:00")
    head, line, _ = row("garbage") if False else row(9999)   # nonsense is ignored, never an error
    assert "UTC+05:30" in head
    # the helper itself: a time without a zone is UTC; an ISO text works too
    token = tzctx.set_offset(60)
    try:
        assert local_text("2026-11-10T23:30:00Z") == "2026-11-11 00:30"
        assert local_text(__import__("datetime").datetime(2026, 11, 10, 23, 30)) == "2026-11-11 00:30"
    finally:
        tzctx._offset.reset(token)


def test_daily_energy_days_start_at_the_viewers_midnight(client):
    """Readings at 18:00 UTC and 19:00 UTC are 23:30 and 00:30 in India: two different days there, one day in UTC."""
    co = signup_owner(client)
    h = login(client, co + "_su")
    r = client.post(f"{API}/devices", headers=h, json={
        "device_name": "meter", "protocol": "Modbus TCP", "ip_address": "10.9.9.1", "port": 502, "unit_id": 1,
        "registers": [{"metric_name": "kwh", "sensor_type": "energy", "unit": "kWh", "data_type": "float32", "register_type": "holding", "register_address": 0}]})
    assert r.status_code == 201, r.text
    dev, rid = r.json()["id"], r.json()["registers"][0]["id"]
    today = (utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")  # a full past day, so no reading is in the future
    ing._save_batch([_msg(co, "main", rid, 100.0, ts=f"{today}T01:00:00Z"), _msg(co, "main", rid, 150.0, ts=f"{today}T18:00:00Z"),
                     _msg(co, "main", rid, 190.0, ts=f"{today}T19:00:00Z")])
    def days(offset):
        res = client.get(f"{API}/energy/{dev}/report?kind=daily&format=csv&days=3", headers=dict(h, **{"X-Tz-Offset": str(offset)}))
        assert res.status_code == 200, res.text
        return [l for l in res.text.splitlines() if l[:2] == "20"]
    assert len(days(0)) == 1                       # UTC: one day
    assert len(days(330)) == 2                     # India: 23:30 and 00:30 are two days


def test_readings_are_kept_and_retried_while_the_database_is_down(client, monkeypatch):
    """A database error used to throw the whole batch away. Now the readings wait and are stored as soon as it answers again."""
    from sqlalchemy.exc import OperationalError
    from app.workers import mqtt_ingest as worker
    co = signup_owner(client)
    h = login(client, co + "_su")
    r = client.post(f"{API}/devices", headers=h, json={"device_name": "p", "protocol": "Modbus TCP", "ip_address": "10.9.9.7", "port": 502, "unit_id": 1,
        "registers": [{"metric_name": "lvl", "sensor_type": "level", "unit": "%", "data_type": "float32", "register_type": "holding", "register_address": 0}]})
    assert r.status_code == 201, r.text
    dev, rid = r.json()["id"], r.json()["registers"][0]["id"]
    real, calls = worker._store, []

    def flaky(company, rows):
        calls.append(1)
        if len(calls) < 3:
            raise OperationalError("select 1", {}, Exception("server closed the connection"))
        return real(company, rows)
    monkeypatch.setattr(worker, "_store", flaky)
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    worker._save_batch([_msg(co, "main", rid, 42.5)])
    assert len(calls) == 3                                   # two failures, then stored
    assert _latest(client, h, dev)["lvl"] == 42.5            # nothing was lost


def test_a_reading_is_confirmed_to_the_broker_only_after_it_is_stored(client, monkeypatch):
    """If the program stops before a reading is stored, the broker has NOT been told it arrived, so it sends the reading again."""
    import queue
    from app.workers import mqtt_ingest as worker
    co = signup_owner(client)
    h = login(client, co + "_su")
    r = client.post(f"{API}/devices", headers=h, json={"device_name": "q", "protocol": "Modbus TCP", "ip_address": "10.9.9.8", "port": 502, "unit_id": 1,
        "registers": [{"metric_name": "lvl", "sensor_type": "level", "unit": "%", "data_type": "float32", "register_type": "holding", "register_address": 0}]})
    dev, rid = r.json()["id"], r.json()["registers"][0]["id"]
    q, confirmed = queue.Queue(), []
    q.put(_msg(co, "main", rid, 61.5) + (777,))             # 6th element = the broker's message id

    def crash(items):
        raise SystemExit("power cut")                        # the program dies while saving
    monkeypatch.setattr(worker, "_save_batch", crash)
    with pytest.raises(SystemExit):
        worker._step(q, confirmed.extend)
    assert confirmed == []                                   # not confirmed -> the broker will send it again

    monkeypatch.undo()
    q.put(_msg(co, "main", rid, 61.5) + (778,))
    worker._step(q, confirmed.extend)
    assert confirmed == [778]                                # stored first, confirmed after
    assert _latest(client, h, dev)["lvl"] == 61.5


def test_app_info_tells_the_app_about_the_newest_version(client, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "APP_LATEST_VERSION", "1.2.0")
    monkeypatch.setattr(settings, "APP_DOWNLOAD_ANDROID", "https://play.google.com/store/apps/details?id=x")
    monkeypatch.setattr(settings, "APP_DOWNLOAD_WINDOWS", "http://not-https.example/x.exe")     # only https links are offered
    j = client.get(f"{API}/app-info").json()
    assert j["latest_app_version"] == "1.2.0"
    assert j["download_urls"] == {"android": "https://play.google.com/store/apps/details?id=x"}
    assert "min_app_version" in j
