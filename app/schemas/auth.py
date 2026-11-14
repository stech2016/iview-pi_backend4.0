import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.core import geo, normalize
from app.core.constants import REGIONS

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
SPECIAL_RE = re.compile(r"[^A-Za-z0-9]")


def validate_password_strength(v: str) -> str:
    if len(v) < 10 or len(v) > 128:
        raise ValueError("Password must be 10-128 characters long")
    if not SPECIAL_RE.search(v):
        raise ValueError("Password must contain at least one special character (e.g. @ # $ % !)")
    if not re.search(r"\d", v):
        raise ValueError("Password must contain at least one digit")
    if not re.search(r"[A-Za-z]", v):
        raise ValueError("Password must contain at least one letter")
    try:   # zxcvbn (open source, Dropbox): rejects common, keyboard-pattern and dictionary passwords
        from zxcvbn import zxcvbn
        if zxcvbn(v[:72])["score"] < 2:
            raise ValueError("That password is too easy to guess. Use a longer one or add unusual words.")
    except ImportError:
        pass
    return v


class SignupIn(BaseModel):
    username: str
    email: EmailStr
    password: str
    company_name: str = Field(min_length=2, max_length=60)
    accept_terms: bool = False
    country: str = Field(default=geo.DEFAULT_COUNTRY, max_length=64)
    state: str = ""
    region: str = ""  # independent of state: any of North/South/East/West
    region_company: str | None = Field(default=None, max_length=120)   # v1.1: the regional company's own name
    city: str | None = Field(default=None, max_length=80)
    plant_name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=24)   # optional

    @field_validator("username")
    @classmethod
    def _username(cls, v):
        if not USERNAME_RE.match(v):
            raise ValueError("Username must be 3-32 characters: letters, numbers, dot, dash or underscore")
        return v.lower()

    @field_validator("password")
    @classmethod
    def _password(cls, v):
        return validate_password_strength(v)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        v = (v or "").strip()
        if not v:
            return None
        if not re.fullmatch(r"\+?[0-9][0-9 ()-]{5,22}", v):
            raise ValueError("Enter a valid phone number, for example +91 98765 43210")
        return v

    @field_validator("country", mode="before")
    @classmethod
    def _country_case(cls, v):
        return normalize.country(v)

    @field_validator("country")
    @classmethod
    def _country(cls, v):
        if not geo.is_country(v):
            raise ValueError("Select a valid country")
        return v

    @model_validator(mode="after")
    def _where(self):
        if self.state:
            self.state = normalize.state(self.state, self.country)
        self.region = normalize.region(self.region)
        if not self.accept_terms:
            raise ValueError("Tick the box to accept the terms of use")
        if not self.state:
            raise ValueError("Select your State / province")
        if not geo.is_state(self.state, self.country):
            raise ValueError("That State / province does not belong to the selected country")
        if self.region not in REGIONS:
            raise ValueError("Region must be one of North, South, East, West")
        return self


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    otp: str | None = Field(default=None, max_length=8)    # v1.1: 6-digit code, only when 2-step login is on
    remember: bool = False                                  # v1.1: "Keep me signed in"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    email: str
    company_name: str
    role: str
    state: str
    region: str
    status: str
    created_at: datetime
    region_company: str | None = None
    city: str | None = None
    plant_name: str | None = None
    phone: str | None = None
    country: str = "India"
    totp_enabled: bool = False
    job_title: str | None = None
    permissions: dict = Field(default_factory=dict)       # {"monitor": 2, "alarms": 2, "config": 1, "control": 0, "reports": 1, "users": 0}
    must_change_password: bool = False
    email_verified: bool = True
    last_login_at: datetime | None = None


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class SignupOut(BaseModel):
    message: str
    status: str
    company_name: str
    needs_activation: bool = False


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _pw(cls, v):
        return validate_password_strength(v)


class ActivateIn(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=12)


class EmailIn(BaseModel):
    email: EmailStr


class ForgotIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    """Either email + the 6-digit code from the e-mail, or (older e-mails) the long token."""
    token: str | None = Field(default=None, min_length=10)
    email: EmailStr | None = None
    code: str | None = Field(default=None, min_length=4, max_length=12)
    new_password: str

    @model_validator(mode="after")
    def _one_way(self):
        if not self.token and not (self.email and self.code):
            raise ValueError("Enter your email and the 6-digit code from the email.")
        return self

    @field_validator("new_password")
    @classmethod
    def _pw(cls, v):
        return validate_password_strength(v)


class MessageOut(BaseModel):
    message: str
    debug_reset_token: str | None = None
    debug_reset_code: str | None = None
