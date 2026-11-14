"""Signed self-update of the plant script: signature check, endpoint, on/off switch, install, rollback."""
import base64
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.helpers import API
from tests.test_v115 import client, co  # noqa: F401  (fixtures)

SRC = Path(__file__).resolve().parents[1] / "plant_gateway" / "plant_gateway.py"
ROOT = Path(__file__).resolve().parents[1]


def load_script(folder: Path):
    spec = importlib.util.spec_from_file_location(f"pg_{folder.name}", folder / "plant_gateway.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def make_key():
    k = Ed25519PrivateKey.generate()
    raw = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return k, base64.b64encode(raw).decode()


def sign_folder(folder: Path, b64: str, monkeypatch):
    spec = importlib.util.spec_from_file_location("sign_release", ROOT / "scripts" / "sign_release.py")
    sr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sr)
    monkeypatch.setattr(sr, "D", folder)
    monkeypatch.setenv("PLANT_SCRIPT_SIGNING_KEY", b64)
    assert sr.main() == 0


@pytest.fixture
def signed(tmp_path, monkeypatch):
    """A copy of plant_gateway/ signed with a fresh key and set as the server's release; version bumped to 9.9."""
    from app.services import selfupdate
    d = tmp_path / "srv"
    d.mkdir()
    d.joinpath("plant_gateway.py").write_text(SRC.read_text().replace('VERSION = "3.5"', 'VERSION = "9.9"', 1))
    d.joinpath("launcher.py").write_text('LAUNCHER_VERSION = "1"\n')
    k, b64 = make_key()
    sign_folder(d, b64, monkeypatch)
    monkeypatch.setattr(selfupdate, "SCRIPT_DIR", d)
    return {"dir": d, "key": k, "b64": b64}


def test_pure_python_ed25519_matches_the_library():
    pg = load_script(SRC.parent)
    k, _ = make_key()
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    msg = b"STECH-PLANT-SCRIPT\n3.5\nabc"
    sig = k.sign(msg)
    assert pg.ed25519_verify(pub, msg, sig)
    assert not pg.ed25519_verify(pub, msg + b"x", sig)
    assert not pg.ed25519_verify(pub, msg, sig[:-1] + bytes([sig[-1] ^ 1]))
    other, _ = make_key()
    assert not pg.ed25519_verify(other.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw), msg, sig)
    assert not pg.ed25519_verify(pub[:31], msg, sig)


def test_unsigned_build_offers_nothing(tmp_path, monkeypatch):
    from app.services import selfupdate
    d = tmp_path / "x"
    d.mkdir()
    d.joinpath("plant_gateway.py").write_text("VERSION = '1'")
    monkeypatch.setattr(selfupdate, "SCRIPT_DIR", d)
    assert selfupdate.release() is None and selfupdate.public_key() is None


def test_script_changed_after_signing_is_not_offered(signed):
    from app.services import selfupdate
    assert selfupdate.release()["version"] == "9.9"
    (signed["dir"] / "plant_gateway.py").write_text("VERSION = \"9.9\"\n# tampered\n")
    assert selfupdate.release() is None


def _gw_auth(co):
    from app.services import gateway as gw
    return {"Authorization": f"Gateway {co['name']}:{gw.get_info(co['name'], 'main').secret}"}


def test_update_endpoint_auth_versions_and_switch(client, co, signed):
    h = _gw_auth(co)
    assert client.get(f"{API}/gateway/update", params={"current": "3.5"}).status_code == 401
    assert client.get(f"{API}/gateway/update", params={"current": "3.5"}, headers={"Authorization": "Gateway nope:nope"}).status_code == 401
    r = client.get(f"{API}/gateway/update", params={"current": "3.5"}, headers=h).json()
    assert r["update"] is True and r["version"] == "9.9" and r["auto"] is True
    assert base64.b64decode(r["script"]) == (signed["dir"] / "plant_gateway.py").read_bytes()
    assert client.get(f"{API}/gateway/update", params={"current": "9.9"}, headers=h).json()["update"] is False   # same version: nothing
    assert client.get(f"{API}/gateway/update", params={"current": "10.0"}, headers=h).json()["update"] is False  # newer already: never a downgrade
    # the app's switch
    assert client.post(f"{API}/gateway/main/auto-update", headers=co["guest"], json={"enabled": False}).status_code == 403
    assert client.post(f"{API}/gateway/main/auto-update", headers=co["su"], json={"enabled": False}).status_code == 200
    r = client.get(f"{API}/gateway/update", params={"current": "3.5"}, headers=h).json()
    assert r["update"] is False and r["auto"] is False and r["latest"] == "9.9"
    assert client.post(f"{API}/gateway/main/auto-update", headers=co["su"], json={"enabled": True}).status_code == 200
    assert client.get(f"{API}/gateway/update", params={"current": "3.5"}, headers=h).json()["update"] is True


def test_update_report_goes_to_the_audit_log(client, co, signed):
    h = _gw_auth(co)
    r = client.get(f"{API}/gateway/update", headers=h, params={"current": "9.9", "event": "updated", "from": "3.5", "to": "9.9"}).json()
    assert r["reported"] is True
    rows = client.get(f"{API}/audit", headers=co["su"]).json()
    rows = rows["items"] if isinstance(rows, dict) else rows
    assert any(x["action"] == "gateway.script_updated" for x in rows)
    ev = client.get(f"{API}/gateway/update", headers=h, params={"current": "9.9", "event": "hack"}).json()
    assert ev["reported"] is False


def test_download_carries_the_public_key(client, co, signed, monkeypatch):
    import io
    import zipfile
    from app.core.config import settings
    from app.services import gateway as gw
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://example.test")
    monkeypatch.setattr(gw, "mqtt_block", lambda: {"host": "h", "port": 8883, "tls": True, "username": "u", "password": "p", "ca_pem": ""})
    z = zipfile.ZipFile(io.BytesIO(gw.build_zip(co["name"], "main")))
    link = json.loads(z.read([n for n in z.namelist() if n.endswith("link.json")][0]))
    pub = k_pub = signed["key"].public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert base64.b64decode(link["update_key"]) == pub == k_pub


# ------------------------------------------------------------------ the plant PC side
class FakeResp:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def plant(tmp_path, signed, monkeypatch):
    """A plant PC folder running version 3.3 whose link.json carries the public key of `signed`."""
    d = tmp_path / "plant"
    d.mkdir()
    shutil.copy(SRC, d / "plant_gateway.py")
    pg = load_script(d)
    pub = signed["key"].public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    link = {"api": "https://x", "company": "c", "gateway_id": "main", "gateway_name": "m", "secret": "s",
            "mqtt": {"host": "h", "port": 8883, "tls": True, "username": "u", "password": "p", "ca_pem": ""},
            "update_key": base64.b64encode(pub).decode()}
    gw = pg.Gateway(link)
    return {"pg": pg, "gw": gw, "dir": d, "link": link, "signed": signed, "mp": monkeypatch}


def offer(plant, **over):
    from app.services import selfupdate
    rel = selfupdate.release()
    data = {"auto": True, "update": True, "reported": False, **selfupdate.encode(rel), **over}
    plant["mp"].setattr(plant["pg"].urllib.request, "urlopen", lambda req, timeout=0: FakeResp(data))


def test_plant_installs_a_valid_update_and_confirms_it(plant):
    pg, gw, d = plant["pg"], plant["gw"], plant["dir"]
    offer(plant)
    gw.check_update()
    assert gw.restart_code == pg.EXIT_UPDATED and gw.stop.is_set()
    assert 'VERSION = "9.9"' in (d / "plant_gateway.py").read_text() and 'VERSION = "3.5"' in (d / "plant_gateway.py.bak").read_text()
    assert json.loads((d / "update_pending.json").read_text())["to"] == "9.9"
    # next start (the new file): not yet confirmed, then connected -> pending file removed, report queued
    pending = json.loads((d / "update_pending.json").read_text())
    assert pending["from"] == "3.5"


def test_plant_refuses_bad_signature_wrong_hash_old_version_and_tamper(plant):
    d, gw = plant["dir"], plant["gw"]
    original = (d / "plant_gateway.py").read_text()
    from app.services import selfupdate
    rel = selfupdate.release()
    other, _ = make_key()
    forged = other.sign(selfupdate.message(rel["version"], rel["sha256"]))
    offer(plant, signature=base64.b64encode(forged).decode())
    gw.check_update()
    assert (d / "plant_gateway.py").read_text() == original and not gw.stop.is_set()               # wrong key
    body = b'VERSION = "9.9"\nprint("evil")\n'
    offer(plant, script=base64.b64encode(body).decode())
    gw.check_update()
    assert (d / "plant_gateway.py").read_text() == original and not gw.stop.is_set()               # script differs from signed hash
    import hashlib
    sha = hashlib.sha256(body).hexdigest()
    offer(plant, script=base64.b64encode(body).decode(), sha256=sha)
    gw.check_update()
    assert (d / "plant_gateway.py").read_text() == original and not gw.stop.is_set()               # hash fits but signature does not
    offer(plant, version="3.3")
    gw.check_update()
    assert not gw.stop.is_set()                                                                    # not newer
    offer(plant, version="3.1")
    gw.check_update()
    assert not gw.stop.is_set()                                                                    # downgrade
    assert not (d / "update_pending.json").exists()


def test_plant_ignores_updates_when_switched_off_locally_or_without_key(plant):
    gw = plant["gw"]
    offer(plant)
    gw.link["auto_update"] = False
    gw.check_update()
    assert not gw.stop.is_set()
    gw.link["auto_update"] = True
    gw.link.pop("update_key")
    gw.check_update()
    assert not gw.stop.is_set()


def test_failed_update_is_rolled_back_and_not_tried_again(plant):
    pg, gw, d = plant["pg"], plant["gw"], plant["dir"]
    offer(plant)
    gw.check_update()
    assert (d / "update_pending.json").exists()
    # simulate the new version failing to start 4 times in a row
    pg.HERE = str(d)
    pg.VERSION = "9.9"
    for _ in range(3):
        assert pg.boot_guard() is False
    assert pg.boot_guard() is True
    assert 'VERSION = "3.5"' in (d / "plant_gateway.py").read_text()
    assert json.loads((d / "update_blocked.json").read_text())["version"] == "9.9"
    assert json.loads((d / "update_report.json").read_text())["event"] == "rolled_back"
    assert not (d / "update_pending.json").exists()
    # the blocked version is refused from now on
    pg.VERSION = "3.5"
    gw2 = pg.Gateway(plant["link"])
    offer(plant)
    gw2.check_update()
    assert not gw2.stop.is_set()


def test_unconfirmed_update_rolls_back_after_the_deadline(plant):
    pg, gw, d = plant["pg"], plant["gw"], plant["dir"]
    offer(plant)
    gw.check_update()
    pg.HERE = str(d)
    pg.VERSION = "9.9"
    gw2 = pg.Gateway(plant["link"])
    pg.CONFIRM_WITHIN = -1          # deadline already passed, cloud never reached
    pg.UPDATE_FIRST = 9999
    gw2.update_loop()
    assert gw2.restart_code == pg.EXIT_UPDATED and 'VERSION = "3.5"' in (d / "plant_gateway.py").read_text()


def test_selftest_flag_exits_cleanly(tmp_path):
    import subprocess
    r = subprocess.run([sys.executable, str(SRC), "--selftest"], capture_output=True, cwd=tmp_path, timeout=60)
    assert r.returncode == 0, r.stderr.decode()[-500:]


def test_confirmed_update_clears_pending_and_queues_the_report(plant):
    import threading
    pg, gw, d = plant["pg"], plant["gw"], plant["dir"]
    offer(plant)
    gw.check_update()
    pg.HERE = str(d)
    pg.VERSION = "9.9"
    gw2 = pg.Gateway(plant["link"])
    gw2.connected.set()
    gw2.cfg_hash = "x"
    pg.UPDATE_FIRST = 9999
    t = threading.Thread(target=gw2.update_loop, daemon=True)
    t.start()
    for _ in range(50):
        if not (d / "update_pending.json").exists():
            break
        threading.Event().wait(0.1)
    gw2.stop.set()
    t.join(5)
    assert not (d / "update_pending.json").exists()
    rep = json.loads((d / "update_report.json").read_text())
    assert rep == {"event": "updated", "from": "3.5", "to": "9.9"} and 'VERSION = "9.9"' in (d / "plant_gateway.py").read_text()


# ------------------------------------------------------------------ write readiness checklist
def test_readiness_tells_what_is_missing_and_how_to_fix(client, co):
    su, d = co["su"], co["dev"]
    r = client.get(f"{API}/control/readiness", headers=su, params={"device_id": d}).json()
    titles = {i["title"]: i for i in r["items"]}
    assert r["ready"] is False
    assert titles["The plant PC is online"]["ok"] is False and "START.bat" in titles["The plant PC is online"]["fix"]
    assert titles["Your job title may send values"]["ok"] is True
    assert all(i["fix"] for i in r["items"] if not i["ok"])                 # every red item says what to do
    g = client.get(f"{API}/control/readiness", headers=co["guest"], params={"device_id": d}).status_code
    assert g in (200, 403)
    me = client.get(f"{API}/control/readiness", headers=co["guest"], params={"device_id": d})
    if me.status_code == 200:
        assert {i["title"]: i for i in me.json()["items"]}["Your job title may send values"]["ok"] is False


# ------------------------------------------------------------------ sliding session
def test_session_is_renewed_while_in_use_but_not_a_keep_signed_in_token(client, co, monkeypatch):
    from datetime import timedelta
    import jwt
    from app.core import security
    from app.core.config import settings
    from app.core.timeutil import utcnow
    tok = co["su"]["Authorization"].split()[1]
    p = jwt.decode(tok, settings.SECRET_KEY, algorithms=["HS256"])
    # a normal token with less than half of its time left -> the next successful call hands out a fresh one
    now = utcnow()
    old = jwt.encode({**p, "iat": now - timedelta(minutes=20), "exp": now + timedelta(minutes=10)}, settings.SECRET_KEY, algorithm="HS256")
    r = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 200 and r.headers.get("x-renewed-token")
    new = jwt.decode(r.headers["x-renewed-token"], settings.SECRET_KEY, algorithms=["HS256"])
    assert new["exp"] - new["iat"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60 and new["sub"] == p["sub"] and new["tv"] == p["tv"]
    # plenty of time left -> nothing to do
    fresh = jwt.encode({**p, "iat": now, "exp": now + timedelta(minutes=29)}, settings.SECRET_KEY, algorithm="HS256")
    assert "x-renewed-token" not in client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {fresh}"}).headers
    # 'keep me signed in' token (30 days) is left alone
    long = jwt.encode({**p, "iat": now - timedelta(days=20), "exp": now + timedelta(days=10)}, settings.SECRET_KEY, algorithm="HS256")
    assert "x-renewed-token" not in client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {long}"}).headers
    # an expired token is never revived
    dead = jwt.encode({**p, "iat": now - timedelta(minutes=40), "exp": now - timedelta(minutes=10)}, settings.SECRET_KEY, algorithm="HS256")
    r = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {dead}"})
    assert r.status_code == 401 and "x-renewed-token" not in r.headers
    # a token that is revoked (password changed / logout) fails the call, so it is not renewed either
    assert security.renewed_token("garbage") is None


# ------------------------------------------------------------------ ready-made gateway program (no Python on the plant PC)
def _zip(client, co, monkeypatch, platform, binaries):
    import io
    import zipfile
    from app.core.config import settings
    from app.services import gateway as gw
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://example.test")
    monkeypatch.setattr(gw, "mqtt_block", lambda: {"host": "h", "port": 8883, "tls": True, "username": "u", "password": "p", "ca_pem": ""})
    monkeypatch.setattr(gw, "program_for", lambda p: binaries.get(p))
    return gw, lambda: zipfile.ZipFile(io.BytesIO(gw.build_zip(co["name"], "main", platform)))


def test_windows_download_has_the_program_and_needs_no_python(client, co, monkeypatch, tmp_path):
    exe = tmp_path / "stech-gateway.exe"
    exe.write_bytes(b"MZ-fake")
    gw, get = _zip(client, co, monkeypatch, "windows", {"windows": exe})
    z = get()
    names = z.namelist()
    assert "STECH_Gateway/STECH-Gateway.exe" in names and "STECH_Gateway/START.bat" not in names
    assert z.read("STECH_Gateway/STECH-Gateway.exe") == b"MZ-fake"
    # nothing but the program and the customer's own files: no Python file, no .bat (the program sets up its own autostart)
    assert not any(n.endswith(".py") or n.endswith(".bat") for n in names), names
    assert sorted(n.split("/")[-1] for n in names if not n.endswith("/")) == ["README.txt", "STECH-Gateway.exe", "THIRD_PARTY_NOTICES.txt", "link.json"]
    assert "Python" not in z.read("STECH_Gateway/README.txt").decode().split("Serial")[0]


def test_windows_without_a_program_falls_back_to_the_old_start_bat(client, co, monkeypatch):
    gw, get = _zip(client, co, monkeypatch, "windows", {})
    z = get()
    assert "STECH_Gateway/START.bat" in z.namelist() and "STECH-Gateway.exe" not in " ".join(z.namelist())
    assert 'START.bat\\" auto' in z.read("STECH_Gateway/AUTOSTART.bat").decode()


def test_linux_and_mac_downloads_have_an_executable_program_and_autostart(client, co, monkeypatch, tmp_path):
    b = tmp_path / "stech-gateway"
    b.write_bytes(b"\x7fELF-fake")
    for plat in ("linux", "macos"):
        gw, get = _zip(client, co, monkeypatch, plat, {plat: b})
        z = get()
        info = {i.filename: i for i in z.infolist()}
        assert (info["STECH_Gateway/stech-gateway"].external_attr >> 16) & 0o111          # executable
        assert (info["STECH_Gateway/install-autostart.sh"].external_attr >> 16) & 0o111
        sh = z.read("STECH_Gateway/install-autostart.sh").decode()
        assert "systemd" in sh and "launchctl" in sh and "__SLUG__" not in sh
    gw, get = _zip(client, co, monkeypatch, "linux", {})
    with pytest.raises(Exception) as e:
        get()
    assert "PROGRAM_NOT_AVAILABLE" in str(getattr(e.value, "code", e.value)) or "not in this server build" in str(e.value)


def test_download_link_takes_a_system_and_the_token_remembers_it(client, co, monkeypatch, tmp_path):
    from app.services import gateway as gw
    b = tmp_path / "stech-gateway"
    b.write_bytes(b"x")
    monkeypatch.setattr(gw, "program_for", lambda p: b if p == "linux" else None)
    monkeypatch.setattr(gw, "mqtt_block", lambda: {"host": "h"})
    from app.core.config import settings
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://example.test")
    ok = client.post(f"{API}/gateway/download-link", headers=co["su"], params={"platform": "linux"})
    assert ok.status_code == 200
    tok = ok.json()["url"].split("token=")[1]
    assert gw.token_platform(tok) == "linux"
    assert client.post(f"{API}/gateway/download-link", headers=co["su"], params={"platform": "macos"}).status_code == 409
    assert client.post(f"{API}/gateway/download-link", headers=co["su"], params={"platform": "beos"}).status_code == 422
    assert gw.token_platform(gw.download_token("c", "main")) == "windows"


# ------------------------------------------------------------------ the ready-made program updates itself too
def _program_release(tmp_path, monkeypatch, version="2", platform="linux", body=b"#!/bin/sh\nexit 0\n"):
    """A signed 'program' for the server side: bin/<platform>/ + release.json with a program entry."""
    import hashlib
    from app.services import selfupdate
    d = tmp_path / "srv2"
    (d / "bin" / platform).mkdir(parents=True)
    (d / "plant_gateway.py").write_text(SRC.read_text().replace('VERSION = "3.5"', 'VERSION = "9.9"', 1))
    (d / "launcher.py").write_text(f'LAUNCHER_VERSION = "{version}"\n')
    f = d / "bin" / platform / selfupdate.BIN_NAMES[platform]
    f.write_bytes(body)
    k, b64 = make_key()
    sign_folder(d, b64, monkeypatch)
    monkeypatch.setattr(selfupdate, "SCRIPT_DIR", d)
    return k, f, hashlib.sha256(body).hexdigest()


def test_server_signs_and_offers_the_program_only_to_an_older_one(client, co, tmp_path, monkeypatch):
    from app.services import selfupdate
    k, f, sha = _program_release(tmp_path, monkeypatch)
    rel = json.loads((selfupdate.SCRIPT_DIR / "release.json").read_text())
    assert rel["programs"]["linux"]["sha256"] == sha and rel["programs"]["linux"]["version"] == "2"
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    k.public_key().verify(base64.b64decode(rel["programs"]["linux"]["signature"]), selfupdate.program_message("linux", "2", sha))  # raises when wrong
    h = _gw_auth(co)
    r = client.get(f"{API}/gateway/update", params={"current": "9.9", "launcher": "1", "platform": "linux"}, headers=h).json()
    assert r["program"]["version"] == "2" and r["program"]["sha256"] == sha and r["program"]["size"] == f.stat().st_size
    assert "program" not in client.get(f"{API}/gateway/update", params={"current": "9.9", "launcher": "2", "platform": "linux"}, headers=h).json()
    assert "program" not in client.get(f"{API}/gateway/update", params={"current": "9.9", "launcher": "1", "platform": "windows"}, headers=h).json()
    assert "program" not in client.get(f"{API}/gateway/update", params={"current": "9.9"}, headers=h).json()           # a plain-Python gateway: no program
    got = client.get(f"{API}/gateway/update/program", params={"platform": "linux"}, headers=h)
    assert got.status_code == 200 and got.content == f.read_bytes()
    assert client.get(f"{API}/gateway/update/program", params={"platform": "linux"}).status_code == 401
    assert client.get(f"{API}/gateway/update/program", params={"platform": "windows"}, headers=h).status_code == 404
    f.write_bytes(b"changed after signing")                                                                              # no longer what was signed
    selfupdate._sha_cache.clear()
    assert selfupdate.program_release("linux") is None
    client.post(f"{API}/gateway/main/auto-update", headers=co["su"], json={"enabled": False})
    f.write_bytes(b"#!/bin/sh\nexit 0\n")
    assert "program" not in client.get(f"{API}/gateway/update", params={"current": "9.9", "launcher": "1", "platform": "linux"}, headers=h).json()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="uses a shell script as the fake program")
def test_plant_installs_a_valid_program_and_refuses_bad_ones(plant, tmp_path, monkeypatch):
    import hashlib
    pg, gw, d = plant["pg"], plant["gw"], plant["dir"]
    body = b"#!/bin/sh\nexit 0\n" + b"#" * 1_100_000
    sha = hashlib.sha256(body).hexdigest()
    key = plant["signed"]["key"]
    sig = base64.b64encode(key.sign(pg.program_message(pg.my_platform(), "2", sha))).decode()
    fake_exe = d / "stech-gateway"
    fake_exe.write_bytes(b"old")
    monkeypatch.setattr(pg.sys, "executable", str(fake_exe))
    monkeypatch.setattr(pg.sys, "frozen", True, raising=False)
    monkeypatch.setenv("STECH_LAUNCHER", "1")
    (d / "plant_gateway.py").write_text("print('x')")
    class Download:
        def __init__(self):
            self.chunks = [body, b""]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return self.chunks.pop(0)
    monkeypatch.setattr(pg.urllib.request, "urlopen", lambda req, timeout=0: Download())
    pub = plant["link"]["update_key"]
    prog = {"version": "2", "sha256": sha, "signature": sig, "size": len(body)}
    assert gw.update_program(prog, pub) is True
    assert gw.restart_code == pg.EXIT_PROGRAM_SWAP and (d / "stech-gateway.new").read_bytes() == body
    assert json.loads((d / "program_pending.json").read_text()) == {"from": "1", "to": "2", "starts": 0}
    (d / "stech-gateway.new").unlink()
    (d / "program_pending.json").unlink()
    # refusals: wrong key, older/same version, oversized
    other, _ = make_key()
    bad = dict(prog, signature=base64.b64encode(other.sign(pg.program_message(pg.my_platform(), "2", sha))).decode())
    gw2 = pg.Gateway(plant["link"])
    assert gw2.update_program(bad, pub) is False and not gw2.stop.is_set()
    assert gw2.update_program(dict(prog, version="1"), pub) is False
    assert gw2.update_program(dict(prog, size=900_000_000), pub) is False
    assert not (d / "stech-gateway.new").exists() and not (d / "program_pending.json").exists()


def test_supervisor_swaps_the_program_and_can_put_the_old_one_back(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("launcher_mod", SRC.parent / "launcher.py")
    lm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lm)
    monkeypatch.setattr(lm, "_folder", lambda: str(tmp_path))
    exe = tmp_path / "stech-gateway"
    exe.write_bytes(b"OLD")
    (tmp_path / "stech-gateway.new").write_bytes(b"NEW")
    assert lm.swap_in(str(exe)) is True
    assert exe.read_bytes() == b"NEW" and (tmp_path / "stech-gateway.old").read_bytes() == b"OLD"
    assert lm.swap_in(str(exe)) is False                                   # nothing waiting
    (tmp_path / "program_pending.json").write_text(json.dumps({"from": "1", "to": "2", "starts": 4}))
    lm.roll_back_program(str(exe), "it kept stopping")
    assert exe.read_bytes() == b"OLD" and (tmp_path / "stech-gateway.bad").read_bytes() == b"NEW"
    assert json.loads((tmp_path / "program_blocked.json").read_text()) == {"version": "2"}
    assert json.loads((tmp_path / "update_report.json").read_text())["event"] == "program_rolled_back"
    assert not (tmp_path / "program_pending.json").exists()


def test_program_sets_up_its_own_autostart_once(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("launcher_auto", SRC.parent / "launcher.py")
    lm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lm)
    monkeypatch.setattr(lm, "_folder", lambda: str(tmp_path))
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return type("R", (), {"returncode": 0})()
    exe = str(tmp_path / "STECH-Gateway.exe")
    assert lm.ensure_autostart(exe, run=fake_run, platform="win32", admin=False) == "user"       # no administrator needed
    assert calls[0][0] == "reg" and "HKCU" in calls[0][2] and "/min" in " ".join(calls[0])
    assert lm.ensure_autostart(exe, run=fake_run, platform="win32", admin=False) == "already" and len(calls) == 1   # only once
    (tmp_path / "autostart.json").unlink()
    assert lm.ensure_autostart(exe, run=fake_run, platform="win32", admin=True) == "task"        # administrator -> whole-PC start-up task
    assert calls[1][:2] == ["schtasks", "/create"] and "SYSTEM" in calls[1] and "onstart" in calls[1]
    (tmp_path / "autostart.json").unlink()
    failing = lambda cmd, **kw: type("R", (), {"returncode": 1})()
    assert lm.ensure_autostart(exe, run=failing, platform="win32", admin=True) == ""             # task refused -> falls back, then gives up quietly
    assert not (tmp_path / "autostart.json").exists()                                            # so it tries again next start
    assert lm.ensure_autostart(str(tmp_path / "stech-gateway"), platform="darwin", home=str(tmp_path / "home")) == "launchagent"
    assert (tmp_path / "home" / "Library" / "LaunchAgents" / "com.stech.iview.gateway.plist").is_file()


def test_sender_never_deadlocks_with_the_mqtt_thread(tmp_path):
    """The real freeze: the sender held a lock around client.publish() while the MQTT thread, holding paho's own lock, waited for that same lock
    in on_publish. Both stopped forever: no heartbeat, no reconnect, no log line. Here a fake paho keeps exactly paho's lock rule."""
    import queue as _q
    import threading as th
    import time as _t
    gm = load_script(SRC.parent)
    paho_lock = th.Lock()          # paho's _out_message_mutex: held while it calls on_publish, and by publish()

    class FakeInfo:
        def __init__(self, mid):
            self.rc, self.mid = 0, mid

    class FakeClient:
        def __init__(self):
            self.mid, self.pending = 0, _q.Queue()

        def publish(self, topic, payload, qos=0):
            with paho_lock:
                self.mid += 1
                self.pending.put(self.mid)
                return FakeInfo(self.mid)

    g = object.__new__(gm.Gateway)
    g.stop, g.connected = th.Event(), th.Event()
    g.connected.set()
    g.inflight, g.sent = {}, set()
    g.acked, g.link_reset = _q.SimpleQueue(), th.Event()
    g.client, g.secret = FakeClient(), "test-secret"
    g.outbox = gm.Outbox(str(tmp_path / "o.db"))
    for i in range(20000):          # enough traffic for the old code to freeze (it stopped with ~15000 still waiting)
        g.outbox.add(f"t/{i}", "{}")

    def network_thread():
        while not g.stop.is_set():
            try:
                mid = g.client.pending.get(timeout=0.05)
            except _q.Empty:
                continue
            with paho_lock:                 # paho holds its lock while calling on_publish
                g._on_publish(g.client, None, mid)

    threads = [th.Thread(target=g.pump_loop, daemon=True), th.Thread(target=network_thread, daemon=True)]
    for t in threads:
        t.start()
    deadline = _t.time() + 40
    while _t.time() < deadline and g.outbox.backlog() > 0:
        _t.sleep(0.05)
    g.stop.set()
    assert g.outbox.backlog() == 0, "the sender and the MQTT thread froze each other"


# ---- the script lives INSIDE the program: no .py file in the customer's folder
def _launcher():
    import importlib.util
    spec = importlib.util.spec_from_file_location("launcher_inside", SRC.parent / "launcher.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_first_start_takes_the_script_out_of_the_program(tmp_path, monkeypatch):
    lau = _launcher()
    inside = tmp_path / "inside" / "plant_gateway.py"
    inside.parent.mkdir()
    inside.write_text('VERSION = "3.5"\nprint("x")\n')
    monkeypatch.setattr(lau, "bundled_script", lambda: str(inside))
    home = tmp_path / "home"
    home.mkdir()
    script = lau.prepare_script(str(home))
    assert script == str(home / "program" / "gateway.bin") and 'VERSION = "3.5"' in open(script).read()
    assert not list(home.glob("*.py"))                                  # nothing but the program folder next to the program


def test_a_newer_update_in_the_folder_is_not_overwritten_by_an_older_program(tmp_path, monkeypatch):
    lau = _launcher()
    inside = tmp_path / "plant_gateway.py"
    inside.write_text('VERSION = "3.5"\n')
    monkeypatch.setattr(lau, "bundled_script", lambda: str(inside))
    home = tmp_path / "home"
    (home / "program").mkdir(parents=True)
    (home / "program" / "gateway.bin").write_text('VERSION = "3.10"\n')    # installed by a signed self-update
    lau.prepare_script(str(home))
    assert 'VERSION = "3.10"' in (home / "program" / "gateway.bin").read_text()
    inside.write_text('VERSION = "3.11"\n')                                 # a newer program arrives with a newer script
    lau.prepare_script(str(home))
    assert 'VERSION = "3.11"' in (home / "program" / "gateway.bin").read_text()


def test_script_from_an_older_download_is_taken_over_and_moved_out_of_sight(tmp_path, monkeypatch):
    lau = _launcher()
    inside = tmp_path / "plant_gateway.py"
    inside.write_text('VERSION = "3.5"\n')
    monkeypatch.setattr(lau, "bundled_script", lambda: str(inside))
    home = tmp_path / "home"
    home.mkdir()
    (home / "plant_gateway.py").write_text('VERSION = "3.9"\n')
    lau.prepare_script(str(home))
    assert not (home / "plant_gateway.py").exists()
    assert 'VERSION = "3.9"' in (home / "program" / "gateway.bin").read_text()


def test_the_script_runs_from_any_file_name_and_keeps_its_data_in_the_program_folder(tmp_path):
    """The updater tests a new script as <file>.new and the program runs gateway.bin: the extension must not matter."""
    import subprocess, sys
    f = tmp_path / "program" / "gateway.bin"
    f.parent.mkdir()
    shutil.copy(SRC, f)
    env = dict(os.environ, STECH_HOME=str(tmp_path), STECH_SCRIPT=str(f))
    out = subprocess.run([sys.executable, str(SRC.parent / "launcher.py"), str(f), "--selftest"], capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path)
    assert out.returncode == 0, out.stdout[-400:] + out.stderr[-400:]
