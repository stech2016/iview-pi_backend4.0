"""My account: profile (e-mail, phone), password, and 2-step login (authenticator app)."""
import re

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.security import verify_password
from app.db.session import get_master_db
from app.deps import client_ip, get_current_user
from app.models.master import User
from app.schemas.auth import MessageOut, UserOut
from app.services import audit, twofactor

router = APIRouter(prefix="/account", tags=["account"])


class ProfileIn(BaseModel):
    """Only the things a person may change about himself. A new e-mail address needs the current password."""
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=24)
    current_password: str | None = Field(default=None, max_length=128)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        v = (v or "").strip()
        if not v:
            return None
        if not re.fullmatch(r"\+?[0-9][0-9 ()-]{5,22}", v):
            raise ValueError("Enter a valid phone number, for example +91 98765 43210")
        return v


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class DisableIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=6, max_length=8)


@router.put("/profile", response_model=UserOut)
def set_profile(body: ProfileIn, request: Request, user: User = Depends(get_current_user), master: Session = Depends(get_master_db)):
    changed = []
    if body.email is not None and body.email.lower() != user.email.lower():
        if not body.current_password or not verify_password(body.current_password, user.password_hash):
            raise AppError(400, "WRONG_PASSWORD", "To change your e-mail address, type your current password.")
        if master.scalar(select(User.id).where(func.lower(User.email) == body.email.lower(), User.id != user.id)):
            raise AppError(409, "EMAIL_TAKEN", "An account with this e-mail address already exists.")
        user.email = body.email.lower()
        changed.append("email")
    if "phone" in body.model_fields_set and body.phone != user.phone:
        user.phone = body.phone
        changed.append("phone")
    master.commit()
    if changed:
        audit.safe_audit(master, user, "account.profile", ",".join(changed), client_ip(request))
    return user


@router.post("/2fa/setup")
def setup_2fa(user: User = Depends(get_current_user), master: Session = Depends(get_master_db)):
    """Returns the secret and the address the app turns into a QR code. 2-step login starts only after /2fa/enable."""
    if user.totp_enabled:
        raise AppError(409, "2FA_ALREADY_ON", "2-step login is already on.")
    secret = twofactor.new_secret()
    twofactor.store(user, secret)
    master.commit()
    return {"secret": secret, "uri": twofactor.uri(user, secret)}


@router.post("/2fa/enable", response_model=MessageOut)
def enable_2fa(body: CodeIn, request: Request, user: User = Depends(get_current_user), master: Session = Depends(get_master_db)):
    if user.totp_enabled or not user.totp_secret:
        raise AppError(409, "2FA_NOT_READY", "Start the 2-step setup first.")
    if not twofactor.check(user, body.code):
        raise AppError(400, "OTP_INVALID", "That code is wrong or has expired. Try the newest code.")
    user.totp_enabled = True
    master.commit()
    audit.safe_audit(master, user, "auth.2fa_on", "account", client_ip(request))
    return MessageOut(message="2-step login is on.")


@router.post("/2fa/disable", response_model=MessageOut)
def disable_2fa(body: DisableIn, request: Request, user: User = Depends(get_current_user), master: Session = Depends(get_master_db)):
    if not user.totp_enabled:
        raise AppError(409, "2FA_OFF", "2-step login is already off.")
    if not verify_password(body.password, user.password_hash) or not twofactor.check(user, body.code):
        raise AppError(400, "2FA_CHECK_FAILED", "The password or the code is wrong.")
    user.totp_enabled, user.totp_secret = False, None
    master.commit()
    audit.safe_audit(master, user, "auth.2fa_off", "account", client_ip(request))
    return MessageOut(message="2-step login is off.")
