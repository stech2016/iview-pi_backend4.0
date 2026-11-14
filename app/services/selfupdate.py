"""Signed self-update of the plant script.

The build pipeline (scripts/sign_release.py, run in GitHub Actions with the private key kept as a GitHub secret) writes
plant_gateway/release.json next to plant_gateway.py and plant_gateway/update_pubkey.txt. This server only HANDS OUT
what was signed there: it holds no private key, so even a hacked server cannot make a plant PC accept a changed script.
"""
import base64
import hashlib
import json
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "plant_gateway"
PLATFORMS = ("windows", "linux", "macos")
BIN_NAMES = {"windows": "stech-gateway.exe", "linux": "stech-gateway", "macos": "stech-gateway"}


def program_message(platform: str, version: str, sha256: str) -> bytes:
    return f"STECH-PLANT-PROGRAM\n{platform}\n{version}\n{sha256}".encode()


def program_path(platform: str) -> Path | None:
    f = SCRIPT_DIR / "bin" / platform / BIN_NAMES[platform]
    return f if f.is_file() else None


_sha_cache: dict = {}


def _sha_of(f: Path) -> str:
    key = (str(f), f.stat().st_mtime_ns, f.stat().st_size)
    if key not in _sha_cache:
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        _sha_cache.clear()
        _sha_cache[key] = h.hexdigest()
    return _sha_cache[key]


def program_release(platform: str) -> dict | None:
    """{'version','sha256','signature','path','size'} of the signed ready-made program for this system, or None."""
    if platform not in PLATFORMS:
        return None
    f = program_path(platform)
    try:
        meta = json.loads((SCRIPT_DIR / "release.json").read_text()).get("programs", {}).get(platform)
    except (OSError, ValueError):
        return None
    if not f or not meta or meta.get("sha256") != _sha_of(f):  # the file changed after it was signed: offer nothing
        return None
    return {"version": meta["version"], "sha256": meta["sha256"], "signature": meta["signature"], "path": f, "size": f.stat().st_size}


def message(version: str, sha256: str) -> bytes:
    return f"STECH-PLANT-SCRIPT\n{version}\n{sha256}".encode()


def vtuple(v) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v)))


def public_key() -> str | None:
    """Base64 of the 32-byte Ed25519 public key, or None when this build was not signed."""
    f = SCRIPT_DIR / "update_pubkey.txt"
    return f.read_text().strip() if f.is_file() else None


def release() -> dict | None:
    """{'version','sha256','signature','script': bytes} for the current script, or None when unsigned / inconsistent."""
    try:
        meta = json.loads((SCRIPT_DIR / "release.json").read_text())
        body = (SCRIPT_DIR / "plant_gateway.py").read_bytes()
    except (OSError, ValueError):
        return None
    sha = hashlib.sha256(body).hexdigest()
    if meta.get("sha256") != sha:  # the script changed after it was signed: offer nothing
        return None
    return {"version": meta["version"], "sha256": sha, "signature": meta["signature"], "script": body}


def encode(rel: dict) -> dict:
    return {"version": rel["version"], "sha256": rel["sha256"], "signature": rel["signature"],
            "script": base64.b64encode(rel["script"]).decode()}
