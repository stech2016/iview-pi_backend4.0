"""Plant gateways: one plant script per State + Region, each with its OWN secret key.

How it fits together
  * 'main' is the original company-wide script (its key is the old company key, so scripts downloaded earlier keep working).
  * Every other gateway ("plant PC") belongs to ONE State + Region and reads only the devices tagged with that State + Region.
  * A State + Region may have SEVERAL plant PCs. Each device names the plant PC that reads it (devices.plant_pc);
    a device that names none (or a plant PC that was removed) is read by the FIRST plant PC of its State + Region.
  * 'main' reads everything that no regional gateway has claimed.
  * Every reading / status message is signed with the key of the gateway named in its topic. A South gateway can never
    write North devices (the signature fails, and the device scope is checked again on the cloud side).
  * Raising a gateway's key_version cancels the key inside a script already downloaded (lost or stolen plant PC).
"""
import hmac
import hashlib
import io
import json
import os
import re
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from itsdangerous import BadSignature, URLSafeTimedSerializer

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.constants import MODBUS_ALL
from app.core.errors import AppError
from app.models.tenant import Device
from app.services import selfupdate

GATEWAY_PROTOCOLS = tuple(MODBUS_ALL) + ("OPC-UA",)   # everything a plant PC can read itself
MAIN = "main"
GID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
SCRIPT_VERSION = "3.2"


# ------------------------------------------------------------------ keys and signatures
def company_secret(company: str) -> str:
    """The key of the 'main' gateway (unchanged, so scripts downloaded before this update keep working)."""
    return hmac.new(settings.SECRET_KEY.encode(), b"gateway-secret:" + company.encode(), hashlib.sha256).hexdigest()


def gateway_secret(company: str, gid: str, version: int = 0) -> str:
    if gid == MAIN and version == 0:
        return company_secret(company)
    msg = f"gateway-secret:{company}:{gid}:{version}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()


def sign(secret: str, topic: str, timestamp: str, value: float) -> str:
    msg = f"{topic}|{timestamp}|{float(value)!r}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ gateway registry (cached, shared rules for all workers)
@dataclass(frozen=True)
class GwInfo:
    gid: str
    name: str
    version: int
    site_state: str | None
    site_region: str | None
    secret: str = field(repr=False)
    claimed: frozenset = frozenset()   # (state, region) pairs that regional gateways own - 'main' skips these
    regions: dict | None = field(default=None, compare=False, repr=False)   # (state, region) -> plant PC ids there, oldest first

    @property
    def is_main(self) -> bool:
        return self.gid == MAIN

    def allows(self, state: str, region: str, plant_pc: str | None = None) -> bool:
        """May this gateway read / write a device tagged with this State + Region (and, if it names one, this plant PC)?"""
        if self.site_state:
            if state != self.site_state or region != self.site_region:
                return False
            mine = (self.regions or {}).get((state, region)) or [self.gid]
            return self.gid == (plant_pc if plant_pc in mine else mine[0])
        return (state, region) not in self.claimed


_CACHE: dict[str, tuple[float, dict[str, GwInfo]]] = {}
_CACHE_LOCK = threading.Lock()
CACHE_SECONDS = 20     # a cancelled key / new gateway is noticed by every process within this time


def invalidate(company: str | None = None) -> None:
    with _CACHE_LOCK:
        if company is None:
            _CACHE.clear()
        else:
            _CACHE.pop(company, None)


def build_infos(company: str, rows: list) -> dict[str, GwInfo]:
    """gateway_id -> info from the Gateway rows of one company. 'main' is always present (virtual until its first message)."""
    rows = sorted(rows, key=lambda r: (r.id or 0, r.gateway_id))
    claimed = frozenset((r.site_state, r.site_region) for r in rows if r.site_state)
    regions: dict[tuple, list[str]] = {}
    for r in rows:
        if r.site_state:
            regions.setdefault((r.site_state, r.site_region), []).append(r.gateway_id)
    out: dict[str, GwInfo] = {}
    for r in rows:
        out[r.gateway_id] = GwInfo(
            r.gateway_id, r.name or (r.gateway_id if r.site_state else "Main (all other regions)"), r.key_version or 0,
            r.site_state, r.site_region, gateway_secret(company, r.gateway_id, r.key_version or 0),
            frozenset() if r.site_state else claimed, regions)
    if MAIN not in out:
        out[MAIN] = GwInfo(MAIN, "Main (all other regions)", 0, None, None, gateway_secret(company, MAIN, 0), claimed)
    return out


def company_gateways(company: str) -> dict[str, GwInfo]:
    """Cached for CACHE_SECONDS (used for every message the plant scripts send)."""
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(company)
        if hit and hit[0] > now:
            return hit[1]
    from app.db.session import MasterSession
    from app.models.master import Gateway
    with MasterSession() as master:
        rows = list(master.scalars(select(Gateway).where(Gateway.company_name == company)))
    out = build_infos(company, rows)
    with _CACHE_LOCK:
        if len(_CACHE) > 1000:   # never let made-up company names grow the cache without limit
            _CACHE.clear()
        _CACHE[company] = (now + CACHE_SECONDS, out)
    return out


def get_info(company: str, gid: str) -> GwInfo | None:
    return company_gateways(company).get(gid)


def verify(company: str, gid: str, topic: str, timestamp: str, value: float, signature: str) -> bool:
    info = get_info(company, gid)
    return bool(info) and hmac.compare_digest(sign(info.secret, topic, timestamp, value), str(signature))


def sign_text(secret: str, topic: str, ts: str, text: str) -> str:
    """Signature of a text message (heartbeat, command, acknowledgement) - the same recipe the plant script uses."""
    return hmac.new(secret.encode(), f"{topic}|{ts}|{text}".encode(), hashlib.sha256).hexdigest()


def verify_text(company: str, gid: str, topic: str, ts: str, state: str, signature: str) -> bool:
    """Status / heartbeat messages of the plant script are signed the same way as readings."""
    info = get_info(company, gid)
    if not info:
        return False
    good = hmac.new(info.secret.encode(), f"{topic}|{ts}|{state}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, str(signature))


def verify_dev(company: str, gid: str, topic: str, ts: str, dev: str, signature: str) -> bool:
    """The per-device health list inside a heartbeat is signed too (so it cannot be forged or edited on the way)."""
    info = get_info(company, gid)
    if not info:
        return False
    good = hmac.new(info.secret.encode(), f"{topic}|{ts}|dev|{dev}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, str(signature))


def responsible_gid(infos: dict[str, "GwInfo"], state: str, region: str, plant_pc: str | None) -> str:
    """The one plant PC (or 'main') that reads a device with this State, Region and plant PC."""
    for gid, info in infos.items():
        if info.site_state and info.allows(state, region, plant_pc):
            return gid
    return MAIN


def slug_for(state: str, region: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", f"{state}_{region}".lower()).strip("_")[:28] or "site"
    gid, n = base, 2
    while gid in taken or gid == MAIN:
        gid = f"{base}_{n}"
        n += 1
    return gid


# ------------------------------------------------------------------ what a gateway must read
def device_specs(db: Session, info: GwInfo | None = None) -> list[dict]:
    """Devices a plant PC can read itself (every Modbus variant / OPC-UA) with their sensors, exactly as set up in the app.
    With `info`, only the devices this gateway is responsible for (its State + Region)."""
    devices = db.scalars(select(Device).where(Device.protocol.in_(GATEWAY_PROTOCOLS)).order_by(Device.device_name)).all()
    return [{
        "id": d.id, "name": d.device_name, "protocol": d.protocol, "host": d.ip_address, "port": d.port,
        "state": d.state, "region": d.region, "plant_pc": d.plant_pc,
        # Modbus details (None = the company default sent in "modbus")
        "unit_id": d.unit_id, "baud": d.serial_baud, "parity": d.serial_parity, "databits": d.serial_databits,
        "stopbits": d.serial_stopbits, "byte_order": d.byte_order, "word_order": d.word_order,
        "address_base": d.address_base or 0, "timeout_ms": d.timeout_ms, "retries": d.retries,
        "registers": [{"id": r.id, "metric_name": r.metric_name, "data_type": r.data_type,
                       "register_type": r.register_type, "register_address": r.register_address,
                       "bit_index": r.bit_index, "node_id": r.node_id,
                       # v1.1.5: the plant PC checks these again before it writes anything (a second lock behind the cloud's)
                       "writable": bool(r.writable), "write_min": r.write_min, "write_max": r.write_max, "scale_factor": r.scale_factor or 1.0}
                      for r in d.registers],
    } for d in devices if info is None or info.allows(d.state, d.region, d.plant_pc)]


def _notices() -> str:
    """Licence notice of the open-source parts, put into every gateway download."""
    try:
        return (Path(__file__).resolve().parent.parent / "data" / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8").replace("\n", "\r\n")
    except OSError:
        return "STECH iVIEW uses open-source components; the list is available from your supplier.\r\n"


# ---- the pre-linked script the admin downloads from the app
def mqtt_block() -> dict:
    if not settings.MQTT_PUBLIC_HOST or not settings.MQTT_GATEWAY_PASSWORD:
        raise AppError(409, "MQTT_NOT_CONFIGURED",
                       "The cloud MQTT broker is not set up yet. On the server set MQTT_PUBLIC_HOST and MQTT_GATEWAY_PASSWORD in .env.")
    pem = ""
    if settings.MQTT_CA_FILE and os.path.isfile(settings.MQTT_CA_FILE):
        with open(settings.MQTT_CA_FILE, encoding="utf-8") as f:
            pem = f.read()
    return {"host": settings.MQTT_PUBLIC_HOST, "port": settings.MQTT_PUBLIC_PORT, "tls": settings.MQTT_PUBLIC_TLS,
            "username": settings.MQTT_GATEWAY_USERNAME, "password": settings.MQTT_GATEWAY_PASSWORD, "ca_pem": pem,
            "transport": settings.MQTT_PUBLIC_TRANSPORT, "ws_path": settings.MQTT_PUBLIC_WS_PATH}


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.SECRET_KEY, salt="gateway-download")


PLATFORMS = ("windows", "linux", "macos")


def download_token(company: str, gid: str = MAIN, platform: str = "windows") -> str:
    return _signer().dumps({"c": company, "g": gid, "p": platform})


def token_platform(token: str) -> str:
    try:
        p = _signer().loads(token, max_age=600).get("p", "windows")
    except BadSignature:
        return "windows"
    return p if p in PLATFORMS else "windows"


def parse_token(token: str) -> tuple[str, str]:
    try:
        data = _signer().loads(token, max_age=600)  # link works for 10 minutes
        return data["c"], data.get("g", MAIN)
    except (BadSignature, KeyError):
        raise AppError(401, "DOWNLOAD_LINK_EXPIRED", "This download link has expired. Open the Gateway page and press Download again.")


SCRIPT_DIR = Path(__file__).resolve().parents[2] / "plant_gateway"

START_BAT = r"""@echo off
title STECH iVIEW plant gateway
cd /d "%~dp0"
if exist ".venv\ready3.flag" goto run
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^) and double-click this file again.
  if not "%1"=="auto" pause
  exit /b 1
)
echo First start: preparing, please wait 1-2 minutes...
if not exist ".venv\Scripts\python.exe" python -m venv .venv
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet paho-mqtt "pymodbus>=3.11,<4" pyserial asyncua
if errorlevel 1 (
  echo Could not download the required parts. Check the internet connection and double-click this file again.
  if not "%1"=="auto" pause
  exit /b 1
)
echo ok> ".venv\ready3.flag"
:run
".venv\Scripts\python.exe" plant_gateway.py
if errorlevel 3 if not errorlevel 4 (
  echo Another copy of this gateway is already running on this PC, so this window can be closed.
  if not "%1"=="auto" pause
  exit /b 0
)
echo The gateway stopped. Restarting in 5 seconds...
timeout /t 5 >nul
goto run
"""


def autostart_bat(task: str, target: str = "START.bat auto") -> str:
    prog, _, extra = target.partition(" ")
    tr = f'\\"%~dp0{prog}\\"' + (f" {extra}" if extra else "")
    return r"""@echo off
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo FAILED - nothing was set up.
  echo This file must be started with "Run as administrator":
  echo right-click AUTOSTART.bat and choose "Run as administrator".
  echo.
  pause
  exit /b 1
)
schtasks /create /tn "__TASK__" /tr "__TR__" /sc onstart /ru SYSTEM /rl HIGHEST /f
if errorlevel 1 (
  echo.
  echo FAILED - Windows would not create the start-up task. Nothing was set up.
  pause
  exit /b 1
)
schtasks /run /tn "__TASK__" >nul 2>&1
echo.
echo DONE. The gateway now starts by itself whenever this PC starts, and a copy was started just now.
echo It runs hidden, so no window is shown. To check it: open gateway.log in this folder, or look at the Gateway page in the app.
echo If a gateway window is also open, close that window.
echo.
pause
""".replace("__TASK__", task).replace("__TR__", tr)


def readme(name: str, folder: str, program: bool = False) -> str:
    first = ("1. Double-click STECH-Gateway.exe. Nothing needs to be installed. Leave the window open.\n"
             "   If Windows shows \"Windows protected your PC\", click More info, then Run anyway.") if program else \
            "1. Double-click START.bat  (needs Python installed on this PC; the first start needs internet for 1-2 minutes). Leave the window open."
    return f"""STECH iVIEW plant gateway - {name}
=========================
{first}
2. Open the STECH iVIEW app: the Gateway page shows "{name}" green when this script is running.
3. That is all. The first time it runs it sets itself up to start with this PC, so you do not run anything else.
   If it is started with "Run as administrator" it also runs hidden with nobody signed in (a server PC). Check it in gateway.log or on the Gateway page.
   For a PC where nobody signs in: right-click STECH-Gateway.exe once and choose "Run as administrator".

This script reads only the devices assigned to this plant PC (the State and Region it belongs to, and the plant PC chosen for each device) and sends them to the cloud.
Keep this PC switched on and connected to the PLC network and the internet.
Serial cable (RS-485 / RS-232): plug the USB-serial adapter into this PC and enter its port (for example COM3) in the app.
Only one program can use a COM port at a time, so close other PLC software that uses the same port.
Modbus TLS devices: put the device's CA certificate in a folder "modbus_tls" next to this script, named ca.pem.
Updates: this script updates itself. It installs only a newer version that is signed by STECH, tests it, and puts the old one back
if the new one cannot connect (see update history in the app's audit log). An administrator can switch this off on the Gateway page.
Do not share link.json - it contains this gateway's key. If this PC is lost, open the Gateway page in the app
and press "Reset key": the old key stops working at once.
"""


def readme_unix(name: str, platform: str) -> str:
    return f"""STECH iVIEW plant gateway - {name} ({platform})
=========================
1. Open a terminal in this folder and run:  ./stech-gateway     (nothing needs to be installed). Leave it running.
   macOS: if it says the program cannot be opened, run once:  xattr -d com.apple.quarantine stech-gateway
2. Open the STECH iVIEW app: the Gateway page shows "{name}" green when it is running.
3. Optional, once: run  sudo ./install-autostart.sh  (Linux)  or  ./install-autostart.sh  (macOS). The gateway then starts by itself
   with the computer and restarts if it stops. Log: gateway.log in this folder.

It reads only the devices assigned to this plant PC and sends them to the cloud. Keep the computer on and connected to the PLC network and the internet.
Serial cable: enter the port (for example /dev/ttyUSB0) in the app. The user running the gateway needs access to that port (Linux: group dialout).
Modbus TLS devices: put the device's CA certificate in a folder "modbus_tls" next to the program, named ca.pem.
Updates: the gateway updates itself with versions signed by STECH and puts the old one back if the new one cannot connect.
Do not share link.json - it contains this gateway's key. If this computer is lost, press "Reset key" on the Gateway page.
"""


def autostart_sh(task: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in task.lower()).strip("-")
    return """#!/bin/sh
cd "$(dirname "$0")" || exit 1
DIR=$(pwd)
if [ "$(uname)" = "Darwin" ]; then
  P="$HOME/Library/LaunchAgents/com.stech.__SLUG__.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>' '<plist version="1.0"><dict>' \
    '<key>Label</key><string>com.stech.__SLUG__</string>' \
    "<key>ProgramArguments</key><array><string>$DIR/stech-gateway</string></array>" \
    "<key>WorkingDirectory</key><string>$DIR</string>" \
    '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>' '</dict></plist>' > "$P"
  launchctl unload "$P" 2>/dev/null; launchctl load "$P" && echo "DONE. The gateway now starts by itself and a copy was started just now."
else
  if [ "$(id -u)" != "0" ]; then echo "FAILED - nothing was set up. Run it with sudo:  sudo ./install-autostart.sh"; exit 1; fi
  printf '%s\n' '[Unit]' 'Description=STECH iVIEW plant gateway' 'After=network-online.target' 'Wants=network-online.target' '' '[Service]' \
    "WorkingDirectory=$DIR" "ExecStart=$DIR/stech-gateway" 'Restart=always' 'RestartSec=5' '' '[Install]' 'WantedBy=multi-user.target' > /etc/systemd/system/__SLUG__.service
  systemctl daemon-reload && systemctl enable --now __SLUG__ && echo "DONE. The gateway now starts by itself and a copy was started just now."
fi
""".replace("__SLUG__", slug)


def program_for(platform: str) -> Path | None:
    """The ready-made gateway program (Python inside) for this system, built by the pipeline; None when this build has none."""
    return selfupdate.program_path(platform)


def build_link(company: str, gid: str = MAIN) -> dict:
    """The contents of link.json: what one gateway needs to reach the cloud (also handed to a gateway box that used a setup code)."""
    info = get_info(company, gid)
    if not info:
        raise AppError(404, "GATEWAY_NOT_FOUND", "This gateway no longer exists. Open the Gateway page and try again.")
    if not settings.PUBLIC_API_URL:
        raise AppError(409, "PUBLIC_API_URL_MISSING", "The server address is not set. On the server put PUBLIC_API_URL=https://your-domain in .env.")
    link = {"api": settings.PUBLIC_API_URL.rstrip("/"), "company": company, "gateway_id": gid, "gateway_name": info.name,
            "secret": info.secret, "mqtt": mqtt_block()}
    if selfupdate.public_key():
        link["update_key"] = selfupdate.public_key()  # the plant script installs only updates signed with the matching private key
    return link


def build_zip(company: str, gid: str = MAIN, platform: str = "windows") -> bytes:
    if platform not in PLATFORMS:
        raise AppError(400, "PLATFORM_UNKNOWN", "Choose Windows, Linux or macOS.")
    if not settings.PUBLIC_API_URL:
        raise AppError(409, "PUBLIC_API_URL_MISSING", "The server address is not set. On the server put PUBLIC_API_URL=https://your-domain in .env.")
    info = get_info(company, gid)
    if not info:
        raise AppError(404, "GATEWAY_NOT_FOUND", "This gateway no longer exists. Open the Gateway page and try again.")
    link = build_link(company, gid)
    prog = program_for(platform)
    if prog is None and platform != "windows":
        raise AppError(409, "PROGRAM_NOT_AVAILABLE", f"The {platform} gateway program is not in this server build yet. Choose Windows, or update the server first.")
    folder = "STECH_Gateway" if gid == MAIN else f"STECH_Gateway_{gid}"
    task = "STECH iVIEW Gateway" if gid == MAIN else f"STECH iVIEW Gateway {gid}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if prog is None:        # only the old way (Python on the PC) needs the script as a file; the program carries it inside
            z.write(SCRIPT_DIR / "plant_gateway.py", f"{folder}/plant_gateway.py")
        z.writestr(f"{folder}/link.json", json.dumps(link, indent=2))
        if platform == "windows":
            if prog is not None:    # ready-made program: nothing to install
                z.write(prog, f"{folder}/STECH-Gateway.exe")
            else:                   # this build has no program: the older way (needs Python on the PC)
                z.writestr(f"{folder}/START.bat", START_BAT.replace("\n", "\r\n"))
                z.writestr(f"{folder}/AUTOSTART.bat", autostart_bat(task, "START.bat auto").replace("\n", "\r\n"))
            z.writestr(f"{folder}/THIRD_PARTY_NOTICES.txt", _notices())
            z.writestr(f"{folder}/README.txt", readme(info.name, folder, prog is not None).replace("\n", "\r\n"))
        else:
            zi = zipfile.ZipInfo(f"{folder}/stech-gateway")
            zi.external_attr = 0o755 << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, prog.read_bytes())
            zs = zipfile.ZipInfo(f"{folder}/install-autostart.sh")
            zs.external_attr = 0o755 << 16
            z.writestr(zs, autostart_sh(task))
            z.writestr(f"{folder}/THIRD_PARTY_NOTICES.txt", _notices())
            z.writestr(f"{folder}/README.txt", readme_unix(info.name, platform))
    return buf.getvalue()
