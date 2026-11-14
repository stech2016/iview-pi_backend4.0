"""Free, open-source security checks. Run before every release:  python scripts/security_check.py

  bandit      scans the Python code for risky patterns (hard-coded secrets, unsafe calls, weak crypto)
  pip-audit   checks every installed library against the public vulnerability database (PyPI / OSV)
Install once:  pip install bandit pip-audit
"""
import subprocess
import sys

steps = [
    ("Code scan (bandit)", [sys.executable, "-m", "bandit", "-q", "-r", "app", "-ll", "-x", "app/alembic"]),
    ("Library vulnerabilities (pip-audit)", [sys.executable, "-m", "pip_audit", "-r", "requirements.txt", "--progress-spinner", "off"]),
]
bad = 0
for name, cmd in steps:
    print(f"\n=== {name} ===")
    bad += subprocess.run(cmd).returncode != 0
print("\nALL CLEAR" if not bad else f"\n{bad} check(s) reported findings: read the lines above")
sys.exit(1 if bad else 0)
