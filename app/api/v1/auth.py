import re

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core import geo, terms
from app.core.constants import COMPANY_RE
from app.core.crypto import encrypt
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.rate_limit import limiter
from app.core.timeutil import utcnow
from app.core.security import (
    DUMMY_HASH, create_access_token, hash_password, make_reset_token, read_reset_token, verify_password,
)
from app.db import provision
from app.db.session import get_master_db
from app.deps import client_ip, get_current_user
from app.models.master import CompanyStorage, User
from app.schemas.auth import (ActivateIn, ChangePasswordIn, EmailIn, ForgotIn, LoginIn, LoginOut, MessageOut, ResetIn, SignupIn,
                              SignupOut, UserOut)
from app.services import audit, lockout, mail, resetcode, twofactor
from app.services.mail import send_activation_email, send_reset_email

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger("auth")


def normalize_company(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    if not COMPANY_RE.match(slug):
        raise AppError(422, "INVALID_COMPANY_NAME",
                       "Company name must start with a letter and contain 2-40 letters or numbers (spaces become underscores).")
    return slug


@router.get("/countries", response_model=list[str])
@limiter.limit("30/minute")
def countries(request: Request):
    return geo.countries()


@router.get("/states", response_model=list[str])
@limiter.limit("60/minute")
def states(request: Request, country: str = Query(min_length=2, max_length=64)):
    if not geo.is_country(country):
        raise AppError(404, "COUNTRY_NOT_FOUND", "Unknown country.")
    return geo.states(country)


@router.get("/terms")
def get_terms():
    """The terms of use shown (and accepted) at sign-up."""
    return {"version": settings.TERMS_VERSION, "text": terms.text()}


@router.post("/signup", response_model=SignupOut, status_code=201)
@limiter.limit("5/minute")
def signup(request: Request, body: SignupIn, tasks: BackgroundTasks, master: Session = Depends(get_master_db)):
    """Only the company owner signs up and becomes the Super admin of the new company. Everybody else is created by the Super admin
    (POST /users) and receives a one-time password."""
    company = normalize_company(body.company_name)
    email = body.email.lower()

    if master.scalar(select(User.id).where(User.username == body.username)):
        raise AppError(409, "USERNAME_TAKEN", "This username is already taken. Please choose another one.")
    if master.scalar(select(User.id).where(func.lower(User.email) == email)):
        raise AppError(409, "EMAIL_TAKEN", "An account with this email already exists. Use 'Forgot password' if you cannot log in.")

    has_storage = master.scalar(select(CompanyStorage.id).where(CompanyStorage.company_name == company)) is not None
    company_exists = has_storage or master.scalar(select(User.id).where(User.company_name == company).limit(1)) is not None
    activation = settings.EMAIL_ACTIVATION
    if activation and not mail.configured():
        raise AppError(503, "MAIL_NOT_SET_UP", "E-mail activation is on but this server has no mail account yet. Set BREVO_API_KEY in the server settings, "
                                              "or set EMAIL_ACTIVATION=false.")

    user = User(username=body.username, email=email, password_hash=hash_password(body.password),
                company_name=company, role="admin", state=body.state, region=body.region, country=body.country,
                region_company=body.region_company, city=body.city, plant_name=body.plant_name, phone=body.phone,
                job_title="Super admin", email_verified=not activation,
                terms_accepted_at=utcnow())

    if company_exists:
        raise AppError(409, "COMPANY_EXISTS",
                       f"The company name '{company}' is already registered. Company names are unique: choose a different name. "
                       "If this is your company, ask its Super admin to create your account.")
    db_name = f"stech_{company}"
    claimed = master.scalar(select(CompanyStorage.id).where(CompanyStorage.db_name == db_name))
    if claimed:
        raise AppError(409, "DB_NAME_CLAIMED", f"The database name '{db_name}' is already claimed by another company. Please choose a unique company name.")
    local = settings.local_url(db_name)
    provision.ensure_database(settings.local_admin_url, db_name)
    provision.provision_schema(local)
    user.status = "approved"
    master.add(user)
    master.add(CompanyStorage(company_name=company, mode="local", db_name=db_name,
                              encrypted_local_url=encrypt(local.render_as_string(hide_password=False))))
    message = f"Company '{company}' created."

    try:
        master.commit()
    except IntegrityError:
        master.rollback()
        raise AppError(409, "USER_EXISTS", "This username or email is already registered.")
    log.info("signup", company=company, role="admin", status=user.status)
    if activation:
        code = resetcode.issue(user.id, "activate", settings.ACTIVATION_CODE_MINUTES)
        tasks.add_task(send_activation_email, user.email, code)
        message += " We sent a 6-digit code to your e-mail. Enter it to activate your account."
    elif user.status == "approved":
        message += " You can log in now."
    return SignupOut(message=message, status=user.status, company_name=company, needs_activation=activation)


@router.post("/activate", response_model=MessageOut)
@limiter.limit("10/minute")
def activate(request: Request, body: ActivateIn, master: Session = Depends(get_master_db)):
    """Confirms the e-mail address with the 6-digit code that was e-mailed at sign-up."""
    bad = AppError(400, "INVALID_ACTIVATION_CODE", "That code is wrong or has expired. Ask for a new one.")
    user = master.scalar(select(User).where(func.lower(User.email) == str(body.email).lower()))
    if not user or user.email_verified or not resetcode.check(user.id, body.code, "activate"):
        raise bad
    user.email_verified = True
    master.commit()
    return MessageOut(message="Your e-mail is confirmed. You can log in now.")


@router.post("/resend-activation", response_model=MessageOut)
@limiter.limit("3/minute")
def resend_activation(request: Request, body: EmailIn, tasks: BackgroundTasks, master: Session = Depends(get_master_db)):
    generic = "If that address is waiting for activation, a new code was sent."
    user = master.scalar(select(User).where(func.lower(User.email) == str(body.email).lower()))
    if user and not user.email_verified and mail.configured():
        code = resetcode.issue(user.id, "activate", settings.ACTIVATION_CODE_MINUTES)
        tasks.add_task(send_activation_email, user.email, code)
    return MessageOut(message=generic)


@router.post("/login", response_model=LoginOut)
@limiter.limit("10/minute")
def login(request: Request, body: LoginIn, master: Session = Depends(get_master_db)):
    ident = body.username.strip().lower()
    lockout.check(ident, client_ip(request))
    # the Username box accepts the username OR the registered e-mail address
    user = master.scalar(select(User).where(User.username == ident))
    if not user and "@" in ident:
        user = master.scalar(select(User).where(func.lower(User.email) == ident))
    ok = verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if not user or not ok:
        lockout.failed(ident, client_ip(request))
        log.warning("login_failed", reason="bad_credentials", ip=client_ip(request))
        raise AppError(401, "INVALID_LOGIN", "Incorrect username or password. Check your details or use 'Forgot password'.")
    if user.status == "pending":
        raise AppError(403, "ACCOUNT_PENDING", "Your account is awaiting approval. Please try again later.")
    if user.status == "rejected":
        raise AppError(403, "ACCOUNT_REJECTED", "Your registration was declined. Contact your company administrator.")
    if user.status == "deactivated":
        raise AppError(403, "ACCOUNT_DEACTIVATED", "This account was switched off. Ask your Super admin to switch it on again.")
    if not user.email_verified:
        raise AppError(403, "EMAIL_NOT_VERIFIED", "Confirm your e-mail address first: enter the 6-digit code we e-mailed you.")
    if user.totp_enabled and not twofactor.check(user, body.otp):
        if not body.otp:
            raise AppError(401, "OTP_REQUIRED", "Enter the 6-digit code from your authenticator app.")
        lockout.failed(ident, client_ip(request))
        raise AppError(401, "OTP_INVALID", "That code is wrong or has expired. Try the newest code.")
    lockout.clear(ident, client_ip(request))
    user.last_login_at = utcnow()
    master.commit()
    token, expires_in = create_access_token(user, body.remember)
    audit.safe_audit(master, user, "auth.login", "session", client_ip(request))
    log.info("login_ok", user_id=user.id, company=user.company_name)
    return LoginOut(access_token=token, expires_in=expires_in, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/change-password", response_model=LoginOut)
@limiter.limit("10/minute")
def change_password(request: Request, body: ChangePasswordIn, user: User = Depends(get_current_user), master: Session = Depends(get_master_db)):
    """Replaces the password (also the one-time password a new person got). All other sessions end; a fresh token is returned."""
    if not verify_password(body.current_password, user.password_hash):
        raise AppError(400, "WRONG_PASSWORD", "The current password is not correct.")
    if body.current_password == body.new_password:
        raise AppError(422, "SAME_PASSWORD", "Choose a password that is different from the current one.")
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.token_version += 1
    master.commit()
    audit.safe_audit(master, user, "auth.password_changed", "account", client_ip(request))
    token, expires_in = create_access_token(user, False)
    return LoginOut(access_token=token, expires_in=expires_in, user=UserOut.model_validate(user))


@router.post("/logout", response_model=MessageOut)
def logout(request: Request, user: User = Depends(get_current_user), master: Session = Depends(get_master_db)):
    audit.safe_audit(master, user, "auth.logout", "session", client_ip(request))
    user.token_version += 1  # revokes every active token for this user
    master.commit()
    return MessageOut(message="You have been logged out.")


@router.post("/forgot-password", response_model=MessageOut)
@limiter.limit("3/minute")
async def forgot_password(request: Request, body: ForgotIn, tasks: BackgroundTasks, master: Session = Depends(get_master_db)):
    generic = "If that email is registered, password reset instructions have been sent."
    user = master.scalar(select(User).where(func.lower(User.email) == body.email.lower()))
    if not user or user.status != "approved":
        return MessageOut(message=generic)
    code = resetcode.issue(user.id)
    token = make_reset_token(user)   # goes into the e-mail link; the 6-digit code is the backup way (either one works once)
    tasks.add_task(send_reset_email, user.email, code, token)
    log.info("password_reset_requested", user_id=user.id)
    # Development convenience only: lets you test the flow without an e-mail account.
    return MessageOut(message=generic, debug_reset_code=code if settings.is_dev else None,
                      debug_reset_token=token if settings.is_dev else None)


@router.post("/reset-password", response_model=MessageOut)
@limiter.limit("5/minute")
def reset_password(request: Request, body: ResetIn, master: Session = Depends(get_master_db)):
    bad = AppError(400, "INVALID_RESET_CODE", "That code is wrong or has expired. Check the newest email, or request a new code.")
    if body.token:
        data = read_reset_token(body.token)
        user = master.get(User, data["uid"])
        if not user or user.token_version != data["tv"]:
            raise AppError(400, "INVALID_RESET_TOKEN", "This password reset link was already used or is no longer valid. Please request a new one.")
    else:
        user = master.scalar(select(User).where(func.lower(User.email) == str(body.email).lower()))
        if not user or user.status != "approved" or not resetcode.check(user.id, body.code or ""):
            raise bad
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1  # invalidates the reset token AND all sessions
    master.commit()
    resetcode.clear(user.id)  # the e-mail link and the code are one-time, whichever was used
    log.info("password_reset_done", user_id=user.id)
    return MessageOut(message="Password updated. You can now log in with your new password.")
