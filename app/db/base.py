from sqlalchemy.orm import DeclarativeBase


class MasterBase(DeclarativeBase):
    """Master registry (users, company storage registry, remote-access address)."""


class TenantBase(DeclarativeBase):
    """Per-company isolated database (devices, telemetry, alarms, audit...)."""
