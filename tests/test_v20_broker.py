"""V2.0 against a REAL Mosquitto broker (skipped when the broker or its Dynamic Security plugin is not installed on the machine):
the repository's own acl file + the Dynamic Security plugin + a TLS listener, a gateway that connects with the login the cloud made,
the ingest worker's real message path, and a write command travelling back."""
import glob
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import brokeradmin, extgw, gwcontrol
from tests.helpers import API, PW, login, signup_owner

BACKEND = Path(__file__).resolve().parents[1]
PLUGIN = next(iter(glob.glob("/usr/lib/**/mosquitto_dynamic_security.so", recursive=True)), None)
pytestmark = pytest.mark.skipif(not (shutil.which("mosquitto") and shutil.which("mosquitto_ctrl") and shutil.which("mosquitto_passwd") and PLUGIN and shutil.which("openssl")),
                                reason="mosquitto + Dynamic Security plugin + openssl are not installed here")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def broker(tmp_path_factory):
    d = tmp_path_factory.mktemp("mosq")
    plain, tls = _free_port(), _free_port()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(d / "k.pem"), "-out", str(d / "c.pem"), "-days", "2",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, capture_output=True)
    subprocess.run(["mosquitto_passwd", "-b", "-c", str(d / "passwd"), "stech_backend", "backendpw"], check=True, capture_output=True)
    subprocess.run(["mosquitto_passwd", "-b", str(d / "passwd"), "stech_gateway", "gatewaypw"], check=True, capture_output=True)
    shutil.copy(BACKEND / "mosquitto" / "acl", d / "acl")                     # the REAL acl file of the product
    subprocess.run(["mosquitto_ctrl", "dynsec", "init", str(d / "dyn.json"), "stech_admin", "adminpw"], check=True, capture_output=True)
    for f in d.iterdir():
        os.chmod(f, 0o644)
    conf = [f"allow_anonymous false", f"password_file {d / 'passwd'}", f"acl_file {d / 'acl'}", f"plugin {PLUGIN}",
            f"plugin_opt_config_file {d / 'dyn.json'}", f"listener {plain} 127.0.0.1", f"listener {tls} 127.0.0.1",
            f"certfile {d / 'c.pem'}", f"keyfile {d / 'k.pem'}", "tls_version tlsv1.2", "max_queued_messages 100000"]
    if os.geteuid() == 0:
        conf.append("user root")
    (d / "m.conf").write_text("\n".join(conf) + "\n")
    proc = subprocess.Popen(["mosquitto", "-c", str(d / "m.conf")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", plain), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("the broker did not start: " + proc.stdout.read().decode()[:500])
    yield {"plain": plain, "tls": tls, "ca": str(d / "c.pem")}
    proc.terminate()
    proc.wait(5)


@pytest.fixture(scope="module")
def client(broker):
    saved = {k: getattr(settings, k) for k in ("MQTT_HOST", "MQTT_PORT", "MQTT_USERNAME", "MQTT_PASSWORD", "MQTT_ADMIN_USERNAME", "MQTT_ADMIN_PASSWORD")}
    for k, v in {"MQTT_HOST": "127.0.0.1", "MQTT_PORT": broker["plain"], "MQTT_USERNAME": "stech_backend", "MQTT_PASSWORD": "backendpw",
                 "MQTT_ADMIN_USERNAME": "stech_admin", "MQTT_ADMIN_PASSWORD": "adminpw"}.items():
        object.__setattr__(settings, k, v)
    gwcontrol._client = None
    with TestClient(app) as c:
        yield c
    for k, v in saved.items():
        object.__setattr__(settings, k, v)
    gwcontrol._client = None


def _gateway(broker, user, pw, tls=True):
    got = []
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=user)
    c.username_pw_set(user, pw)
    if tls:
        c.tls_set(ca_certs=broker["ca"], tls_version=ssl.PROTOCOL_TLS_CLIENT)
    ok = threading.Event()
    result = {}

    def on_connect(cl, u, f, rc, p=None):
        result["rc"] = rc
        ok.set()

    c.on_connect = on_connect
    c.on_message = lambda cl, u, m: got.append((m.topic, m.payload))
    c.connect("127.0.0.1", broker["tls" if tls else "plain"])
    c.loop_start()
    ok.wait(5)
    return c, got, result.get("rc")


def _ingest(broker):
    """The ingest worker's real message path: the same callbacks, the same queue and saver thread as `python -m app.workers.mqtt_ingest`."""
    from app.workers import mqtt_ingest as ing
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="stech-ingest-test", clean_session=False)
    c.username_pw_set("stech_backend", "backendpw")
    c.manual_ack_set(True)
    ing._client = c
    c.on_connect, c.on_message = ing.on_connect, ing.on_message
    c.connect("127.0.0.1", broker["plain"])
    c.loop_start()
    t = threading.Thread(target=ing.ext_loop, args=(None, ing._ack_now), daemon=True)
    t.start()
    return c


def test_real_chain_gateway_to_detected_list_to_values_and_back(client, broker):
    name = signup_owner(client)
    su = login(client, name + "_su")
    made = client.post(f"{API}/plants", headers=su, json={"state": "Tamil Nadu", "region": "South", "name": "Real broker plant"})
    assert made.status_code == 201, made.text
    conn, pid = made.json()["connection"], made.json()["plant"]["plant_id"]
    ing = _ingest(broker)
    try:
        # a wrong password and a wrong client id are refused; the right login works over TLS
        assert _gateway(broker, pid, "wrong")[2].is_failure
        bad_id = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="someone-else")
        bad_id.username_pw_set(pid, conn["password"])
        bad_id.tls_set(ca_certs=broker["ca"], tls_version=ssl.PROTOCOL_TLS_CLIENT)
        rc = {}
        bad_id.on_connect = lambda c, u, f, r, p=None: rc.setdefault("r", r)
        bad_id.connect("127.0.0.1", broker["tls"])
        bad_id.loop_start()
        time.sleep(1)
        bad_id.loop_stop()
        assert rc["r"].is_failure
        gate, got, rc = _gateway(broker, pid, conn["password"])
        assert not rc.is_failure
        gate.subscribe(f"iview/{pid}/cmd/#", qos=1)
        gate.subscribe(f"spBv1.0/{pid}/DCMD/#", qos=1)
        gate.subscribe("iview/someoneelse/#", qos=1)
        time.sleep(0.5)
        # data -> Detected list
        gate.publish(f"iview/{pid}/Boiler1", json.dumps({"Steam_Temp": 181.0, "Pump": True}), qos=1)
        for _ in range(50):
            items = client.get(f"{API}/plants/{pid}/detected", headers=su).json()["items"]
            if len(items) == 2:
                break
            time.sleep(0.1)
        assert {i["tag"] for i in items} == {"Steam_Temp", "Pump"}
        # tick, then values flow through the same real path
        picks = {i["tag"]: i["id"] for i in items}
        assert client.post(f"{API}/plants/{pid}/detected/accept", headers=su, json={"items": [
            {"id": picks["Steam_Temp"], "name": "Steam_temp", "device_name": "Boiler 1"}, {"id": picks["Pump"], "name": "Pump", "device_name": "Boiler 1"}]}).json()["sensors_added"] == 2
        dev = next(d for d in client.get(f"{API}/devices", headers=su).json() if d["device_name"] == "Boiler 1")
        gate.publish(f"iview/{pid}/Boiler1", json.dumps({"Steam_Temp": 190.5, "Pump": False}), qos=1)
        for _ in range(50):
            latest = {x["metric_name"]: x["value"] for x in client.get(f"{API}/devices/{dev['id']}/latest", headers=su).json()}
            if latest.get("Steam_temp") == 190.5:
                break
            time.sleep(0.1)
        assert latest == {"Steam_temp": 190.5, "Pump": 0.0}
        # a gateway may not write into another plant's topics (the broker refuses it, the ingest never sees it)
        other = client.post(f"{API}/plants", headers=su, json={"state": "Tamil Nadu", "region": "South", "name": "Other plant"}).json()["plant"]["plant_id"]
        gate.publish(f"iview/{other}/Boiler1", json.dumps({"Evil": 1}), qos=1)
        time.sleep(1)
        assert client.get(f"{API}/plants/{other}/detected", headers=su).json()["items"] == []
        # write: the cloud publishes, the gateway receives it on ITS command topic only
        regs = client.get(f"{API}/devices/{dev['id']}", headers=su).json()["registers"]
        pump = next(r for r in regs if r["metric_name"] == "Pump")
        client.put(f"{API}/settings", headers=su, json={"values": {"write_enabled": True}})
        assert client.put(f"{API}/devices/{dev['id']}/registers/{pump['id']}/write-config", headers=su, json={"writable": True}).status_code == 200
        sent = client.post(f"{API}/control/commands", headers=su, json={"device_id": dev["id"], "register_id": pump["id"], "value": 1, "confirm": True,
                                                                        "reason": "broker test", "password": PW})
        assert sent.status_code == 201, sent.text
        for _ in range(50):
            if got:
                break
            time.sleep(0.1)
        assert [t for t, _ in got] == [f"iview/{pid}/cmd/Boiler1/Pump"]
        assert json.loads(got[0][1])["value"] == 1.0
        gate.publish(f"iview/{pid}/Boiler1", json.dumps({"Pump": True}), qos=1)       # the gateway wrote it to the PLC and reports the new value
        for _ in range(50):
            state = client.get(f"{API}/control/commands/{sent.json()['id']}", headers=su).json()["state"]
            if state == "confirmed":
                break
            time.sleep(0.1)
        assert state == "confirmed"
        # reset the password: the old one is refused at once, the new one works; switch off: refused again
        new = client.post(f"{API}/plants/{pid}/reset-password", headers=su).json()["connection"]["password"]
        assert _gateway(broker, pid, conn["password"])[2].is_failure and not _gateway(broker, pid, new)[2].is_failure
        assert client.post(f"{API}/plants/{pid}/enable", headers=su, json={"enabled": False}).status_code == 200
        assert _gateway(broker, pid, new)[2].is_failure
        gate.loop_stop()
    finally:
        ing.loop_stop()


def test_old_plant_script_logins_still_work_next_to_the_new_ones(broker):
    c, _, rc = _gateway(broker, "stech_gateway", "gatewaypw", tls=False)
    assert not rc.is_failure
    c.publish("stech/x/gw/main/r1", "{}", qos=1)
    c.loop_stop()
    plain = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="anon")
    seen = {}
    plain.on_connect = lambda cl, u, f, r, p=None: seen.setdefault("r", r)
    plain.connect("127.0.0.1", broker["plain"])
    plain.loop_start()
    time.sleep(0.7)
    plain.loop_stop()
    assert seen["r"].is_failure                                                  # nobody gets in without a login


def test_a_plant_login_cannot_listen_to_everything(client, broker):
    made = client.post(f"{API}/plants", headers=login(client, signup_owner(client) + "_su"), json={"state": "Tamil Nadu", "region": "South"})
    pid, pw = made.json()["plant"]["plant_id"], made.json()["connection"]["password"]
    gate, got, rc = _gateway(broker, pid, pw)
    assert not rc.is_failure
    spy = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="spy-backend")
    spy.username_pw_set("stech_backend", "backendpw")
    spy.connect("127.0.0.1", broker["plain"])
    spy.loop_start()
    for t in ("#", "$SYS/#", "stech/#", "iview/#", "iview/pl0000000000/#"):
        gate.subscribe(t, qos=1)
    time.sleep(0.5)
    for t in ("stech/secret/x", "iview/pl0000000000/x", "iview/other/x", "spBv1.0/other/NDATA/e"):
        spy.publish(t, "secret", qos=1)
    time.sleep(1)
    spy.loop_stop()
    gate.loop_stop()
    assert got == []
