"""Fernet encryption for secrets stored in the master registry (cloud DB passwords, URLs)."""
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_fernet = Fernet(settings.FERNET_KEY.encode())


def encrypt(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken as exc:  # wrong master key
        raise RuntimeError("Unable to decrypt stored secret: FERNET_KEY mismatch") from exc
