from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutil import utcnow
from app.db.base import MasterBase


class User(MasterBase):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(100))
    company_name: Mapped[str] = mapped_column(String(63), index=True)
    role: Mapped[str] = mapped_column(String(16))  # admin | operator | guest
    state: Mapped[str] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(10))  # North | South | East | West
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | approved | rejected
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # v1.1: who this person is inside the company (shown on the Super admin's region tile and in the sign-up alert)
    region_company: Mapped[str | None] = mapped_column(String(80), nullable=True)   # the regional company's own name, e.g. "Chennai Water Supply Co"
    city: Mapped[str | None] = mapped_column(String(80), nullable=True)
    plant_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)            # for the optional SMS / voice add-on (+91...)
    totp_secret: Mapped[str | None] = mapped_column(Text, nullable=True)            # 2-step login secret, Fernet-encrypted
    totp_enabled: Mapped[bool] = mapped_column(default=False, server_default="false")
    # v1.1.4: country
    country: Mapped[str] = mapped_column(String(64), default="India", server_default="India")
    # v1.1.5: the Super admin creates every other person. Job title + one level per area (see app/services/perms.py)
    job_title: Mapped[str | None] = mapped_column(String(24), nullable=True)
    perms: Mapped[str | None] = mapped_column(Text, nullable=True)                       # JSON {"monitor": 2, "control": 0, ...}
    must_change_password: Mapped[bool] = mapped_column(default=False, server_default="false")   # a one-time password must be replaced at first login
    created_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    email_verified: Mapped[bool] = mapped_column(default=True, server_default="true")
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


    @property
    def permissions(self) -> dict:
        """The level (0 None, 1 Read, 2 Read and Write) of every area for this person."""
        from app.services import perms
        return perms.effective(self)


class CompanyStorage(MasterBase):
    """Master Registry: company -> storage mode, DB name, encrypted connection secrets."""

    __tablename__ = "company_storage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    mode: Mapped[str] = mapped_column(String(10), default="local")  # local | cloud | hybrid
    db_name: Mapped[str | None] = mapped_column(String(63), unique=True, nullable=True)
    encrypted_local_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet
    cloud_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cloud_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cloud_user: Mapped[str | None] = mapped_column(String(128), nullable=True)
    encrypted_cloud_password: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RemoteAccess(MasterBase):
    """Tailscale Funnel public address that remote locations use to reach this company's backend."""

    __tablename__ = "remote_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    funnel_url: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AlarmSettings(MasterBase):
    """Per company: minutes an UNACKNOWLEDGED alarm of each priority may wait before it is escalated to the admins (0 = never)."""

    __tablename__ = "alarm_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    critical: Mapped[int] = mapped_column(Integer, default=2)
    major: Mapped[int] = mapped_column(Integer, default=5)
    minor: Mapped[int] = mapped_column(Integer, default=0)
    advisory: Mapped[int] = mapped_column(Integer, default=0)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Gateway(MasterBase):
    """One plant script of a company. 'main' is the original company-wide script; every other gateway belongs to ONE
    State + Region (site_state / site_region) and reads only the devices of that State + Region.
    key_version: raise it to cancel the key inside an already downloaded script (lost / stolen plant PC)."""

    __tablename__ = "gateways"
    __table_args__ = (Index("uq_gateway_company_gid", "company_name", "gateway_id", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(63), index=True)
    gateway_id: Mapped[str] = mapped_column(String(32), default="main", server_default="main")
    name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    site_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    site_region: Mapped[str | None] = mapped_column(String(10), nullable=True)
    key_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    auto_update: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")  # signed self-update of the plant script
    state: Mapped[str] = mapped_column(String(10), default="never")  # never | online | offline
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlantLink(MasterBase):
    """V2.0: one customer-owned gateway (any brand) of ONE State + Region. The cloud makes its broker login (plant_id = user name =
    client id = Sparkplug group id) and shows the password once. The password itself lives only in the broker (Dynamic Security)."""

    __tablename__ = "plant_links"
    __table_args__ = (Index("uq_plant_link_plant_id", "plant_id", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(63), index=True)
    plant_id: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(80))
    site_state: Mapped[str] = mapped_column(String(64))
    site_region: Mapped[str] = mapped_column(String(10))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    cmd_format: Mapped[str] = mapped_column(String(10), default="auto", server_default="auto")   # auto | json | sparkplug  (how write commands are sent)
    last_format: Mapped[str | None] = mapped_column(String(10), nullable=True)                   # json | sparkplug: what the gateway sent last
    sp_edge: Mapped[str | None] = mapped_column(String(64), nullable=True)                       # Sparkplug edge node id learned from its messages
    state: Mapped[str] = mapped_column(String(10), default="never", server_default="never")    # never | online | offline
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class StoragePolicy(MasterBase):
    """Per company: how long readings are kept (keep_days NULL = until the admin deletes them) and the total space the
    cloud owner gave this company (quota_gb NULL = no limit shown). The cloud owner sets quota_gb with scripts/set_quota.py."""

    __tablename__ = "storage_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    keep_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_purge_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StorageShare(MasterBase):
    """The Super admin splits the company's space between regions: this is one region's share."""

    __tablename__ = "storage_shares"
    __table_args__ = (Index("uq_share_company_region", "company_name", "state", "region", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(63), index=True)
    state: Mapped[str] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(10))
    share_gb: Mapped[float] = mapped_column(Float)


class OnCallStep(MasterBase):
    """On-call roster of one State + Region: step 1 is told first; if nobody acknowledges the alarm within wait_minutes
    the next step is told, and so on."""

    __tablename__ = "oncall_steps"
    __table_args__ = (Index("uq_oncall_step", "company_name", "state", "region", "step_no", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(63), index=True)
    state: Mapped[str] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(10))
    step_no: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(Integer)
    wait_minutes: Mapped[int] = mapped_column(Integer, default=10)


class ReportSchedule(MasterBase):
    """A report e-mailed by itself: every day, or every week on one weekday, at one local hour."""

    __tablename__ = "report_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(63), index=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(12))        # dashboard | erp
    fmt: Mapped[str] = mapped_column(String(4))          # csv | xlsx | pdf
    frequency: Mapped[str] = mapped_column(String(8))    # daily | weekly
    hour: Mapped[int] = mapped_column(Integer)           # 0-23, plant local time
    weekday: Mapped[int] = mapped_column(Integer, default=0)   # 0 = Monday ... 6 = Sunday (weekly only)
    recipients: Mapped[str] = mapped_column(Text)        # comma-separated e-mail addresses
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CompanySettings(MasterBase):
    """Per company overrides of the settings listed in app/services/csettings.py (JSON {key: value}). Missing key = the server default."""

    __tablename__ = "company_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    values: Mapped[str] = mapped_column(Text, default="{}")
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ShareLink(MasterBase):
    """A read-only link anybody can open without logging in (a live page). Only the SHA-256 of the secret is stored."""

    __tablename__ = "share_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    company_name: Mapped[str] = mapped_column(String(63), index=True)
    name: Mapped[str] = mapped_column(String(80))
    scope: Mapped[str] = mapped_column(String(12))                 # device | region
    device_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    region: Mapped[str | None] = mapped_column(String(10), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Handover(MasterBase):
    """Shift handover of one State + Region: the outgoing person writes a note, iVIEW attaches the plant summary,
    the incoming person signs it."""

    __tablename__ = "handovers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(63), index=True)
    state: Mapped[str] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(10))
    from_user: Mapped[str] = mapped_column(String(64))
    from_user_id: Mapped[int] = mapped_column(Integer)
    shift_label: Mapped[str | None] = mapped_column(String(40), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="{}")      # JSON snapshot of the plant at hand-over time
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    signed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signed_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sign_note: Mapped[str | None] = mapped_column(Text, nullable=True)
