"""2-step login (authenticator app code). Free: pyotp (MIT). The secret is stored encrypted."""
import pyotp

from app.core.config import settings
from app.core.crypto import decrypt, encrypt


def new_secret() -> str:
    return pyotp.random_base32()


def uri(user, secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name=settings.TOTP_ISSUER)


def store(user, secret: str) -> None:
    user.totp_secret = encrypt(secret)


def _verify(secret: str, code: str | None) -> bool:
    return bool(code) and pyotp.TOTP(secret).verify(code.strip(), valid_window=1)


def check(user, code: str | None) -> bool:
    return bool(user.totp_secret) and _verify(decrypt(user.totp_secret), code)


def check_secret(secret: str, code: str | None) -> bool:
    return _verify(secret, code)
