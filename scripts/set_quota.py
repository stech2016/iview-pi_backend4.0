"""Siemas sets how many GB a client's plan includes.   python scripts/set_quota.py <company> <gb>   (gb 0 = no limit)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import MasterSession  # noqa: E402
from app.services import retention  # noqa: E402

if len(sys.argv) != 3:
    sys.exit(__doc__)
with MasterSession() as master:
    p = retention.policy(master, sys.argv[1].strip().lower(), create=True)
    p.quota_gb = float(sys.argv[2]) or None
    p.updated_by = "siemas"
    master.commit()
    print(f"{p.company_name}: quota {p.quota_gb or 'no limit'} GB")
