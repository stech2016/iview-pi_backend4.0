"""Run ONCE on your own PC:  python scripts/gen_signing_key.py
Prints the private key (put it in GitHub -> repo Settings -> Secrets and variables -> Actions -> New repository secret,
name PLANT_SCRIPT_SIGNING_KEY) and keep a copy in your password manager. Never put it on the server or in the repo."""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

k = Ed25519PrivateKey.generate()
raw = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
print("PRIVATE KEY (secret PLANT_SCRIPT_SIGNING_KEY):")
print(base64.b64encode(raw).decode())
