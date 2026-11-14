r"""Replace the three secret keys in backend/.env WITHOUT breaking anything.   Run:  python scripts/rotate_secrets.py

Use it when a key was shown/pasted somewhere it should not have been. It:
  1. asks you to close the running backend first, then asks for YES
  2. saves a copy of the current file as  .env.backup-<date-time>   (your old settings are never lost)
  3. creates new SECRET_KEY, FERNET_KEY and AUDIT_HMAC_KEY, keeping every other line of .env as it is
  4. re-encrypts the passwords stored in the master registry with the new FERNET_KEY (otherwise the app
     could not open your company databases any more)
  5. re-signs each company's audit log with the new AUDIT_HMAC_KEY (so "verify audit chain" stays valid)
Everyone is logged out afterwards (old login tokens were signed with the old SECRET_KEY) - that is expected.
"""
import hashlib
import os
import hmac
import json
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

os.chdir(Path(__file__).resolve().parents[1])  # settings read .env from the backend folder
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.fernet import Fernet  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402

from app.core.config import settings  # noqa: E402  (reads the CURRENT .env = old keys)
from app.core.constants import GENESIS_HASH  # noqa: E402
from app.db.session import MasterSession, primary_url  # noqa: E402
from app.models.master import CompanyStorage  # noqa: E402

ENV = Path(__file__).resolve().parents[1] / ".env"


def seal(key: str, row, prev: str) -> str:
    payload = json.dumps([row.user_id, row.company_name, row.action, row.resource,
                          row.timestamp.astimezone(timezone.utc).isoformat(), row.ip_address, prev],
                         separators=(",", ":"), ensure_ascii=True)
    return hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def main():
    if not ENV.exists():
        raise SystemExit(f"{ENV} not found. Run this from the backend folder, where your .env is.")
    print("Close the running backend (Ctrl+C in its window) before continuing.")
    if input("Type YES to rotate the secret keys now: ").strip() != "YES":
        raise SystemExit("Cancelled - nothing was changed.")

    new_secret, new_fernet, new_audit = secrets.token_hex(32), Fernet.generate_key().decode(), secrets.token_hex(32)
    old_f, new_f = Fernet(settings.FERNET_KEY.encode()), Fernet(new_fernet.encode())

    text_env = ENV.read_text()
    for key, val in {"SECRET_KEY": new_secret, "FERNET_KEY": new_fernet, "AUDIT_HMAC_KEY": new_audit}.items():
        if re.search(rf"^{key}=.*$", text_env, flags=re.M):
            text_env = re.sub(rf"^{key}=.*$", lambda _m, k=key, v=val: f"{k}={v}", text_env, flags=re.M)
        else:
            text_env += f"\n{key}={val}\n"

    backup = ENV.with_name(f".env.backup-{datetime.now():%Y%m%d-%H%M%S}")
    backup.write_text(ENV.read_text())
    print(f"1/4 old .env saved as {backup.name}")

    with MasterSession() as master:
        companies = list(master.scalars(select(CompanyStorage)))
        # 2) audit logs: re-sign every company's chain with the new key
        for st in companies:
            if not st.db_name:
                continue
            engine = create_engine(primary_url(st))  # still decrypted with the OLD key here
            try:
                with engine.begin() as conn:
                    rows = conn.execute(text("SELECT id, user_id, company_name, action, resource, timestamp, ip_address "
                                             "FROM audit_logs ORDER BY id")).all()
                    prev = GENESIS_HASH
                    for r in rows:
                        h = seal(new_audit, r, prev)
                        conn.execute(text("UPDATE audit_logs SET previous_hash=:p, row_hash=:h WHERE id=:i"), {"p": prev, "h": h, "i": r.id})
                        prev = h
                print(f"2/4 audit log re-signed for {st.company_name} ({len(rows)} rows)")
            except Exception as exc:
                print(f"    skipped audit re-sign for {st.company_name}: {type(exc).__name__}")
            finally:
                engine.dispose()
        # 3) stored passwords: old key -> new key
        for st in companies:
            for col in ("encrypted_local_url", "encrypted_cloud_password"):
                token = getattr(st, col)
                if token:
                    setattr(st, col, new_f.encrypt(old_f.decrypt(token.encode())).decode())
        master.commit()
        print(f"3/4 stored passwords re-encrypted for {len(companies)} company record(s)")

    ENV.write_text(text_env)
    print("4/4 new keys written to .env.  Start the backend again; everyone must log in once more.")
    print(f"Keep {backup.name} private or delete it (it still contains the OLD keys).")


if __name__ == "__main__":
    main()
