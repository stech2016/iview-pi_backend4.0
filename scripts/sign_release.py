"""Build step (GitHub Actions): signs plant_gateway/plant_gateway.py with the key in env PLANT_SCRIPT_SIGNING_KEY.
Writes plant_gateway/release.json and plant_gateway/update_pubkey.txt. Without the secret nothing is written and the
cloud simply offers no automatic update (the build still succeeds)."""
import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

D = Path(__file__).resolve().parents[1] / "plant_gateway"


def main() -> int:
    b64 = os.environ.get("PLANT_SCRIPT_SIGNING_KEY", "").strip()
    if not b64:
        print("PLANT_SCRIPT_SIGNING_KEY not set - release NOT signed, automatic plant-script updates stay off")
        return 0
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(b64))
    body = (D / "plant_gateway.py").read_bytes()
    version = re.search(rb'^VERSION = "([^"]+)"', body, re.M).group(1).decode()
    sha = hashlib.sha256(body).hexdigest()
    sig = key.sign(f"STECH-PLANT-SCRIPT\n{version}\n{sha}".encode())
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    launcher = re.search(rb'^LAUNCHER_VERSION = "([^"]+)"', (D / "launcher.py").read_bytes(), re.M).group(1).decode()
    programs = {}
    for platform, name in (("windows", "stech-gateway.exe"), ("linux", "stech-gateway"), ("macos", "stech-gateway")):
        f = D / "bin" / platform / name
        if f.is_file():
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            psig = key.sign(f"STECH-PLANT-PROGRAM\n{platform}\n{launcher}\n{h}".encode())
            programs[platform] = {"version": launcher, "sha256": h, "signature": base64.b64encode(psig).decode()}
    (D / "release.json").write_text(json.dumps({"version": version, "sha256": sha, "signature": base64.b64encode(sig).decode(), "programs": programs}))
    (D / "update_pubkey.txt").write_text(base64.b64encode(pub).decode())
    print(f"signed plant_gateway {version} ({sha[:12]}), programs: {', '.join(programs) or 'none'} (program version {launcher})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
