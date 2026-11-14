"""Test set-up: needs PostgreSQL (user postgres/postgres on localhost) and Redis on localhost. Run:  python -m pytest tests -q"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-test-secret-key-0123456789")
os.environ.setdefault("FERNET_KEY", "sBI8QWZ4mxj7DooA2mT58V2DCREdcp40I5O5lQVCJEU=")
os.environ.setdefault("AUDIT_HMAC_KEY", "test-audit-key-0123456789")
os.environ.setdefault("MASTER_DB_NAME", "stech_master_test")
os.environ.setdefault("ENVIRONMENT", "development")

from app.core.rate_limit import limiter  # noqa: E402

limiter.enabled = False     # the tests log in more often than the real limit (10 per minute) allows
