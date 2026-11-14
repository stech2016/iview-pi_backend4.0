"""v1.1.4 checks: countries, sign-up validation, sign-up makes a Super admin, main sensors and sparks, plant PC summary."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from tests.helpers import PW, login

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def su_body(name, **kw):
    return {"username": name, "email": f"{name}@example.com", "password": PW, "company_name": name, "role": "admin", "accept_terms": True,
            "country": "India", "state": "Tamil Nadu", "region": "South", **kw}


def test_countries_and_states(client):
    c = client.get(f"{API}/auth/countries").json()
    assert "India" in c and "Singapore" in c and "United States" in c and len(c) > 240
    assert "Tamil Nadu" in client.get(f"{API}/auth/states", params={"country": "India"}).json()
    assert "Texas" in client.get(f"{API}/auth/states", params={"country": "United States"}).json()
    assert client.get(f"{API}/auth/states", params={"country": "Atlantis"}).status_code == 404


def test_signup_checks_every_field(client):
    n = "v" + uuid.uuid4().hex[:8]
    bad = client.post(f"{API}/auth/signup", json={**su_body(n), "state": "Texas"})
    assert bad.status_code == 422                                   # Texas is not in India
    bad = client.post(f"{API}/auth/signup", json={**su_body(n), "country": "Narnia"})
    assert bad.status_code == 422
    bad = client.post(f"{API}/auth/signup", json={**su_body(n), "state": ""})
    assert bad.status_code == 422
    bad = client.post(f"{API}/auth/signup", json={**su_body(n), "phone": "abc"})
    assert bad.status_code == 422
    ok = client.post(f"{API}/auth/signup", json={**su_body(n), "country": "United States", "state": "Texas", "phone": ""})
    assert ok.status_code == 201, ok.text                           # phone is optional
    me = client.get(f"{API}/auth/me", headers=login(client, n)).json()
    assert (me["country"], me["state"], me["phone"]) == ("United States", "Texas", None)


def test_signup_makes_a_super_admin_and_main_sensors(client):
    a = "a" + uuid.uuid4().hex[:8]
    r = client.post(f"{API}/auth/signup", json=su_body(a))
    assert r.status_code == 201 and r.json()["status"] == "approved", r.text
    ah = login(client, a)
    me = client.get(f"{API}/auth/me", headers=ah).json()
    assert (me["role"], me["job_title"]) == ("admin", "Super admin")      # the person who creates the account is its Super admin
    assert client.get(f"{API}/auth/parents").status_code == 404                          # no Parent company any more
    assert client.post(f"{API}/auth/signup", json={**su_body("z" + uuid.uuid4().hex[:8]), "role": "parent_admin"}).json()["status"] == "approved"   # a role in the form is ignored
    d = client.post(f"{API}/devices", headers=ah, json={"device_name": "Pump", "protocol": "Modbus TCP", "ip_address": "10.0.0.9", "port": 502,
                    "registers": [{"metric_name": "t1", "data_type": "float32", "register_type": "holding", "register_address": 1, "unit": "C"},
                                  {"metric_name": "t2", "data_type": "float32", "register_type": "holding", "register_address": 3, "unit": "bar"},
                                  {"metric_name": "t3", "data_type": "float32", "register_type": "holding", "register_address": 5}]})
    assert d.status_code == 201, d.text

    # main sensors: default first two, then a chosen one; sparks report them
    dev = d.json()
    sp = client.get(f"{API}/devices/sparks", headers=ah).json()[str(dev["id"])]
    assert [x["metric"] for x in sp] == ["t1", "t2"]
    ids3 = [r["id"] for r in dev["registers"]]
    r = client.put(f"{API}/devices/{dev['id']}/main-sensors", headers=ah, json={"register_ids": [ids3[2]]})
    assert r.status_code == 200 and [x["id"] for x in r.json()["registers"] if x["is_main"]] == [ids3[2]]
    assert [x["metric"] for x in client.get(f"{API}/devices/sparks", headers=ah).json()[str(dev["id"])]] == ["t3"]
    assert client.put(f"{API}/devices/{dev['id']}/main-sensors", headers=ah, json={"register_ids": [999999]}).status_code == 422
    assert client.put(f"{API}/devices/{dev['id']}/main-sensors", headers=ah, json={"register_ids": ids3}).status_code == 422

    # plant PC summary: a device exists but no plant PC has connected yet
    s = client.get(f"{API}/gateway/summary", headers=ah).json()
    assert s["server"] is True and s["plant_pcs"] == 1 and s["connected"] == 0 and s["all_connected"] is False
