"""STGW4.0 gateway box: the one-time setup code (make it in the app, type it on the box, the box gets its connection details)."""
import io
import json
import re
import time
import zipfile

import pytest
import redis
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import enroll, gateway as svc
from tests.helpers import API, login, make_user, signup_owner

CODE_RE = re.compile(r"^[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}$")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def cloud_is_set_up(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://example.test")
    monkeypatch.setattr(settings, "MQTT_PUBLIC_HOST", "mqtt.example.test")
    monkeypatch.setattr(settings, "MQTT_GATEWAY_PASSWORD", "pw-for-tests")


def _owner(client):
    name = signup_owner(client)
    return name, login(client, name + "_su")


def _code(client, h, gid="main"):
    r = client.post(f"{API}/gateway/{gid}/setup-code", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_code_is_short_readable_and_valid_for_a_day(client):
    _, h = _owner(client)
    out = _code(client, h)
    assert CODE_RE.match(out["code"]) and out["expires_in_minutes"] == 1440 and out["gateway_id"] == "main"
    ttl = enroll._r().ttl(enroll._key(out["code"]))
    assert 1400 * 60 < ttl <= 1440 * 60                       # the clock really runs in Valkey, not just in the answer
    stored = " ".join(enroll._r().keys("stgw:enroll:*"))
    assert out["code"].replace("-", "") not in stored         # only a hash of the code is kept


def test_the_box_gets_exactly_what_link_json_holds_and_the_code_works_once(client):
    co, h = _owner(client)
    code = _code(client, h)["code"]
    r = client.post(f"{API}/gateway/enroll", json={"code": code})          # no login: the code is the proof
    assert r.status_code == 200, r.text
    link = r.json()
    assert r.headers["cache-control"] == "no-store"
    assert link["company"] == co and link["gateway_id"] == "main" and link["api"] == "https://example.test"
    assert link["secret"] == svc.get_info(co, "main").secret and link["mqtt"]["host"] == "mqtt.example.test"
    zipped = json.loads(zipfile.ZipFile(io.BytesIO(svc.build_zip(co, "main", "windows"))).read("STECH_Gateway/link.json"))
    assert link == zipped                                                   # same as the file inside the normal download
    again = client.post(f"{API}/gateway/enroll", json={"code": code})
    assert again.status_code == 404 and again.json()["error"]["code"] == "CODE_INVALID"


def test_typing_the_code_loosely_still_works(client):
    _, h = _owner(client)
    code = _code(client, h)["code"]
    loose = "  " + code.lower().replace("-", " ") + " "
    assert client.post(f"{API}/gateway/enroll", json={"code": loose}).status_code == 200


def test_a_new_code_cancels_the_older_one(client):
    _, h = _owner(client)
    first = _code(client, h)["code"]
    second = _code(client, h)["code"]
    assert first != second
    assert client.post(f"{API}/gateway/enroll", json={"code": first}).status_code == 404
    assert client.post(f"{API}/gateway/enroll", json={"code": second}).status_code == 200


def test_wrong_and_malformed_codes_all_get_the_same_answer(client):
    for bad in ("ZZZZ-ZZZZ", "AAAA-AAAA", "0000-0000", "ABCD-EFGH-IJKL", "ab cd"[:6] + "!!"):
        r = client.post(f"{API}/gateway/enroll", json={"code": bad})
        assert r.status_code == 404 and r.json()["error"]["code"] == "CODE_INVALID", (bad, r.text)
    assert client.post(f"{API}/gateway/enroll", json={"code": "x"}).status_code == 422     # far too short: refused by the shape check


def test_an_expired_code_is_refused(client):
    _, h = _owner(client)
    code = _code(client, h)["code"]
    enroll._r().expire(enroll._key(code), 1)
    time.sleep(1.6)
    assert client.post(f"{API}/gateway/enroll", json={"code": code}).status_code == 404


def test_only_administrators_can_make_a_code(client):
    co, su = _owner(client)
    op, _ = make_user(client, su, co + "_op", "Operator")
    guest, _ = make_user(client, su, co + "_gu", "Guest")
    for who in (op, guest):
        assert client.post(f"{API}/gateway/main/setup-code", headers=who).status_code == 403
    assert client.post(f"{API}/gateway/main/setup-code").status_code == 401
    assert client.post(f"{API}/gateway/nope/setup-code", headers=su).status_code == 404


def test_a_regional_admin_only_gets_codes_for_their_own_region(client):
    co, su = _owner(client)
    north, _ = make_user(client, su, co + "_no", "Admin", region="North")        # the company's gateway 'main' is South
    r = client.post(f"{API}/gateway/main/setup-code", headers=north)
    print("regional admin of another region:", r.status_code, r.text[:120])
    assert r.status_code == 403 and r.json()["error"]["code"] == "OUTSIDE_YOUR_REGION"


def test_a_reset_key_is_followed_by_the_box(client):
    co, h = _owner(client)
    code = _code(client, h)["code"]
    assert client.post(f"{API}/gateway/main/reset-key", headers=h).status_code == 200
    link = client.post(f"{API}/gateway/enroll", json={"code": code}).json()
    assert link["secret"] == svc.get_info(co, "main").secret                        # the NEW key, never the cancelled one


def test_both_steps_are_in_the_audit_log(client):
    _, h = _owner(client)
    code = _code(client, h)["code"]
    assert client.post(f"{API}/gateway/enroll", json={"code": code}).status_code == 200
    text = client.get(f"{API}/audit?limit=100", headers=h).text
    assert "gateway.setup_code" in text and "gateway.box_enrolled" in text
    assert code.replace("-", "") not in text and "pw-for-tests" not in text         # neither the code nor a secret is ever logged


def test_server_not_set_up_gives_a_clear_message(client, monkeypatch):
    _, h = _owner(client)
    monkeypatch.setattr(settings, "MQTT_PUBLIC_HOST", "")
    r = client.post(f"{API}/gateway/main/setup-code", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "MQTT_NOT_CONFIGURED"


def test_live_data_service_down_is_a_clear_503_not_a_crash(client, monkeypatch):
    _, h = _owner(client)

    def down():
        raise redis.ConnectionError("down")
    monkeypatch.setattr(enroll, "_r", down)
    assert client.post(f"{API}/gateway/main/setup-code", headers=h).status_code == 503
    r = client.post(f"{API}/gateway/enroll", json={"code": "ABCD-EFGH"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "CODES_UNAVAILABLE"


# =============================================================================================== the gateway program on the box (3.5)
import importlib.util
import queue as _q
import threading as th
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.workers import mqtt_ingest as ing
from tests.test_v116_ingest import _msg, _setup

SRC = Path(__file__).resolve().parents[1] / "plant_gateway" / "plant_gateway.py"


@pytest.fixture()
def gm(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("pg_stgw", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, "HERE", str(tmp_path))
    return m


def _link(**extra):
    return {"company": "c", "secret": "s", "api": "https://x.test", "gateway_id": "main", "mqtt": {"username": "u", "password": "p"}, **extra}


def test_outbox_size_comes_from_link_json_and_is_kept_sane(gm):
    assert gm.VERSION == "3.5"
    assert gm.Gateway(_link()).outbox.max_rows == 500_000                         # a plant PC: unchanged
    assert gm.Gateway(_link(outbox_max_rows=4_000_000)).outbox.max_rows == 4_000_000
    assert gm.Gateway(_link(outbox_max_rows=5)).outbox.max_rows == 10_000         # never absurdly small
    assert gm.Gateway(_link(outbox_max_rows=10 ** 12)).outbox.max_rows == 50_000_000
    assert gm.Gateway(_link(outbox_max_rows="lots")).outbox.max_rows == 500_000   # garbage: the safe default


def test_a_full_outbox_drops_the_oldest_and_keeps_the_newest(gm, tmp_path):
    o = gm.Outbox(str(tmp_path / "full.db"), max_rows=700)
    for i in range(1500):
        o.add(f"t/{i}", "{}")
    left = [r[1] for r in o.batch(set(), 5000)]
    assert 700 <= len(left) <= 700 + 499                                         # trimmed every 500 readings, cheaply
    assert left[-1] == "t/1499" and "t/0" not in left and left == sorted(left, key=lambda t: int(t[2:]))


def _gateway_with_fake_cloud(gm, tmp_path, n, record):
    class FakeInfo:
        def __init__(self, mid):
            self.rc, self.mid = 0, mid

    class FakeClient:
        def __init__(self):
            self.mid, self.pending = 0, _q.Queue()

        def publish(self, topic, payload, qos=0):
            self.mid += 1
            record.append(topic)
            self.pending.put(self.mid)
            return FakeInfo(self.mid)

    g = object.__new__(gm.Gateway)
    g.stop, g.connected = th.Event(), th.Event()
    g.inflight, g.sent = {}, set()
    g.acked, g.link_reset = _q.SimpleQueue(), th.Event()
    g.client, g.secret = FakeClient(), "test-secret"
    g.outbox = gm.Outbox(str(tmp_path / "o.db"))
    for i in range(n):
        g.outbox.add(f"t/{i}", "{}")
    return g


def _run(g, until):
    def network():
        while not g.stop.is_set():
            try:
                mid = g.client.pending.get(timeout=0.02)
            except _q.Empty:
                continue
            g._on_publish(g.client, None, mid)
    ts = [th.Thread(target=g.pump_loop, daemon=True), th.Thread(target=network, daemon=True)]
    for t in ts:
        t.start()
    deadline = time.time() + 60
    while time.time() < deadline and not until():
        time.sleep(0.02)
    g.stop.set()
    for t in ts:
        t.join(2)


def test_after_a_long_outage_the_live_readings_go_first_then_the_backlog(gm, tmp_path):
    sent = []
    g = _gateway_with_fake_cloud(gm, tmp_path, 5000, sent)        # 5,000 readings waited while the cloud was away
    g.connected.set()                                              # the cloud is back
    _run(g, lambda: len(sent) >= 400)
    first = [int(t[2:]) for t in sent[:gm.LIVE_LANE_ROWS]]
    assert first == list(range(4999, 4999 - gm.LIVE_LANE_ROWS, -1)), "the newest readings must go out first"
    assert sent[gm.LIVE_LANE_ROWS] == "t/0", "then the oldest, in order"


def test_a_short_outage_is_sent_oldest_first_as_before(gm, tmp_path):
    sent = []
    g = _gateway_with_fake_cloud(gm, tmp_path, 500, sent)
    g.connected.set()
    _run(g, lambda: g.outbox.backlog() == 0)
    assert sent == [f"t/{i}" for i in range(500)]


def test_every_reading_arrives_exactly_once_with_the_live_lane(gm, tmp_path):
    sent = []
    g = _gateway_with_fake_cloud(gm, tmp_path, 7000, sent)
    g.connected.set()
    _run(g, lambda: g.outbox.backlog() == 0)
    assert g.outbox.backlog() == 0
    assert len(sent) == 7000 and len(set(sent)) == 7000, "nothing lost, nothing sent twice"


def test_the_cloud_stores_readings_that_arrive_newest_first_correctly(client):
    """After an outage the box sends the live readings first; the history must still be complete and the latest value the newest one."""
    co, h, sensors, dev = _setup(client, 1)
    rid = sensors[0][1]
    base = datetime.now(timezone.utc) - timedelta(minutes=30)
    stamps = [(base + timedelta(minutes=i)).isoformat().replace("+00:00", "Z") for i in range(10)]
    newest_first = [(i, stamps[i]) for i in (9, 8, 7)] + [(i, stamps[i]) for i in range(0, 7)]
    for i, ts in newest_first:                                   # one by one, as the box sends them
        ing._save_batch([_msg(co, "main", rid, float(i + 1), ts=ts)])
    r = client.get(f"{API}/devices/{dev}/latest", headers=h)
    assert {x["metric_name"]: x["value"] for x in r.json()}["m0"] == 10.0          # the newest reading is the current value
    q = (f"{API}/devices/{dev}/export?format=csv&metric=m0&date_from={(base - timedelta(minutes=1)).isoformat().replace('+00:00', 'Z')}"
         f"&date_to={(base + timedelta(hours=1)).isoformat().replace('+00:00', 'Z')}")
    lines = client.get(q, headers=h).text.strip().splitlines()
    assert len(lines) - 1 == 10, lines                                                # all ten readings are in the history
    again = [(i, stamps[i]) for i in (9, 3, 5)]                                       # a resend after a power cut changes nothing
    for i, ts in again:
        ing._save_batch([_msg(co, "main", rid, float(i + 1), ts=ts)])
    assert len(client.get(q, headers=h).text.strip().splitlines()) - 1 == 10


# =============================================================================================== a cancelled key and its replacement
def test_waiting_readings_are_signed_again_with_the_new_key_when_sent(client, gm):
    """Readings stored while the OLD key was valid must still be accepted after a reset-key and a new setup code."""
    co, h, sensors, dev = _setup(client, 1)
    rid = sensors[0][1]
    topic = f"stech/{co}/r{rid}"
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    stored = gm.reading_payload("the-old-cancelled-key", topic, 7.0, ts)
    ing._save_batch([(co, "main", topic, rid, stored.encode())])                      # as the box would send it unchanged: refused
    assert "m0" not in {x["metric_name"] for x in client.get(f"{API}/devices/{dev}/latest", headers=h).json()}
    from app.services import gateway as gws
    fresh = gm.resign(gws.get_info(co, "main").secret, topic, stored)
    ing._save_batch([(co, "main", topic, rid, fresh.encode())])                       # signed again with the current key: accepted
    assert {x["metric_name"]: x["value"] for x in client.get(f"{API}/devices/{dev}/latest", headers=h).json()}["m0"] == 7.0
    assert json.loads(fresh)["timestamp"] == ts and json.loads(fresh)["value"] == 7.0  # same reading, same time: only the signature changed


def test_resign_leaves_anything_else_alone(gm):
    for odd in ("not json", "[1,2]", '{"value": 1}', ""):
        assert gm.resign("k", "t", odd) == odd


def test_a_refused_key_is_said_once_and_retried_slowly(gm, caplog):
    calls = []

    class Client:
        def reconnect_delay_set(self, a, b):
            calls.append((a, b))

    class Rc:
        is_failure = True
        value = 135

    me = type("G", (), {"key_refused": False})()
    with caplog.at_level("WARNING"):
        gm.Gateway._on_connect(me, Client(), None, None, Rc())
        gm.Gateway._on_connect(me, Client(), None, None, Rc())
    assert me.key_refused is True and calls == [(30, 120), (30, 120)]
    assert sum("KEY CANCELLED" in r.getMessage() for r in caplog.records) == 1        # one clear line, not a flood
    assert "NEW setup code" in gm.KEY_CANCELLED_TEXT


def test_a_refused_connection_that_is_not_about_the_key_keeps_normal_retry(gm):
    calls = []

    class Client:
        def reconnect_delay_set(self, a, b):
            calls.append((a, b))

    class Rc:
        is_failure = True
        value = 136                                                                    # server unavailable: not a key problem

    me = type("G", (), {"key_refused": False})()
    gm.Gateway._on_connect(me, Client(), None, None, Rc())
    assert me.key_refused is False and calls == []


def test_a_cancelled_key_that_keeps_trying_is_noted_once_in_the_audit_log(client):
    co, h = _owner(client)
    old = svc.get_info(co, "main").secret
    assert client.post(f"{API}/gateway/main/reset-key", headers=h).status_code == 200
    svc.invalidate(co)
    enroll._r().delete(f"stgw:oldkey:{co}:main")
    hdr = {"Authorization": f"Gateway {co}:main:{old}"}
    for _ in range(3):
        assert client.get(f"{API}/gateway/devices", headers=hdr).status_code == 401
    text = client.get(f"{API}/audit?limit=100", headers=h).text
    assert text.count("gateway.old_key_used") == 1                                   # once, not three times
    assert old not in text
    good = {"Authorization": f"Gateway {co}:main:{svc.get_info(co, 'main').secret}"}
    assert client.get(f"{API}/gateway/devices", headers=good).status_code == 200      # the new key works


def test_garbage_for_an_unknown_company_writes_nothing(client):
    enroll._r().delete("stgw:oldkey:nosuchco:main")
    assert client.get(f"{API}/gateway/devices", headers={"Authorization": "Gateway nosuchco:main:xyz"}).status_code == 401
    assert enroll._r().keys("stgw:oldkey:nosuchco:*") == []
