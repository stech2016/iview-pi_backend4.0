"""Shared helpers: sign up a company owner, create people the way the Super admin does, log in."""
import uuid

PW = "Str0ng!Passw0rd#42"
STATE = "Tamil Nadu"
API = "/api/v1"


def login(client, username, password=PW, **extra):
    r = client.post(f"{API}/auth/login", json={"username": username, "password": password, **extra})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def signup_owner(client, name=None, **kw):
    name = name or "t" + uuid.uuid4().hex[:8]
    body = {"username": name + "_su", "email": f"{name}_su@example.com", "password": PW, "company_name": name, "role": "admin",
            "accept_terms": True, "state": STATE, "region": "South", **kw}
    r = client.post(f"{API}/auth/signup", json=body)
    assert r.status_code == 201, r.text
    return name


def make_user(client, creator, username, title, state=STATE, region="South", permissions=None, **kw):
    """Creates a person (as the Super admin or a 'Manage users' holder), then does the forced first-login password change.
    Returns (headers, user dict)."""
    body = {"username": username, "email": f"{username}@example.com", "job_title": title, "state": state, "region": region,
            "send_email": False, **kw}
    if permissions is not None:
        body["permissions"] = permissions
    r = client.post(f"{API}/users", headers=creator, json=body)
    assert r.status_code == 201, r.text
    temp = r.json()["temporary_password"]
    first = client.post(f"{API}/auth/login", json={"username": username, "password": temp})
    assert first.status_code == 200, first.text
    assert first.json()["user"]["must_change_password"] is True
    h = {"Authorization": "Bearer " + first.json()["access_token"]}
    changed = client.post(f"{API}/auth/change-password", headers=h, json={"current_password": temp, "new_password": PW})
    assert changed.status_code == 200, changed.text
    return {"Authorization": "Bearer " + changed.json()["access_token"]}, r.json()["user"]
