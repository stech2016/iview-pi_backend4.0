import bcrypt
import jwt
from datetime import timedelta
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import settings
from app.core.errors import AppError
from app.core.timeutil import utcnow

ALGO = "HS256"
RESET_MAX_AGE = 3600


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt(12)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False


DUMMY_HASH = hash_password("timing-equaliser")


def create_access_token(user, remember: bool = False) -> tuple[str, int]:
    expires = timedelta(days=settings.REMEMBER_DAYS) if remember else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    now = utcnow()
    payload = {
        "sub": str(user.id), "tv": user.token_version, "role": user.role,
        "company": user.company_name, "iat": now, "exp": now + expires,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGO), int(expires.total_seconds())


def renewed_token(token: str) -> str | None:
    """A fresh normal-length token for a still-valid short-lived one that is past half of its life; None otherwise."""
    try:
        p = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGO])
    except jwt.PyJWTError:
        return None
    life = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    now = utcnow().timestamp()
    iat, exp = float(p["iat"]), float(p["exp"])
    if exp - iat > life + 5:        # a 'keep me signed in' token: not renewed
        return None
    if exp - now > life / 2:        # more than half left: nothing to do yet
        return None
    n = utcnow()
    return jwt.encode({**p, "iat": n, "exp": n + timedelta(seconds=life)}, settings.SECRET_KEY, algorithm=ALGO)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGO])
    except jwt.ExpiredSignatureError:
        raise AppError(401, "TOKEN_EXPIRED", "Your session has expired. Please log in again.")
    except jwt.PyJWTError:
        raise AppError(401, "TOKEN_INVALID", "Your session is invalid. Please log in again.")


_reset = URLSafeTimedSerializer(settings.SECRET_KEY, salt="stech-password-reset")


def make_reset_token(user) -> str:
    return _reset.dumps({"uid": user.id, "tv": user.token_version})


def read_reset_token(token: str) -> dict:
    try:
        return _reset.loads(token, max_age=RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        raise AppError(400, "INVALID_RESET_TOKEN", "This password reset link is invalid or has expired. Please request a new one.")
