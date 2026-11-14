"""SMS and voice call through the client's own Twilio account. Used only when SMS_PROVIDER=twilio.
.env: SMS_ACCOUNT (Account SID), SMS_TOKEN (Auth Token), SMS_FROM (a Twilio phone number). The client pays Twilio, not us."""
from xml.sax.saxutils import escape

import httpx

from app.core.config import settings


def _base() -> str:
    return (settings.SMS_API_URL or "https://api.twilio.com").rstrip("/") + f"/2010-04-01/Accounts/{settings.SMS_ACCOUNT}"


def _post(path: str, data: dict) -> None:
    httpx.post(_base() + path, data=data, auth=(settings.SMS_ACCOUNT, settings.SMS_TOKEN), timeout=15).raise_for_status()


def send_sms(to: str, text: str) -> None:
    _post("/Messages.json", {"To": to, "From": settings.SMS_FROM, "Body": text})


def call(to: str, text: str) -> None:
    _post("/Calls.json", {"To": to, "From": settings.SMS_FROM, "Twiml": f"<Response><Say>{escape(text)}</Say></Response>"})
