"""Extra way to tell a person about an alarm, next to the phone notification (push) and the in-app list.

  twilio_sms.py the SMS and voice add-on. OFF until SMS_PROVIDER=twilio and the client's own account is in .env.

To add another SMS / voice provider: copy twilio_sms.py to a new file, keep the two functions send_sms() and call(),
and add one line to PROVIDERS below. Nothing else changes.
"""
import threading

from app.core.config import settings
from app.core.logging import get_logger
from app.services.channels import twilio_sms

log = get_logger("channels")
PROVIDERS = {"twilio": twilio_sms}


def _run(people: list[dict], title: str, body: str, severity: str) -> None:
    provider = PROVIDERS.get(settings.SMS_PROVIDER.strip().lower())
    for p in people:
        try:
            if provider and p.get("phone") and severity in ("critical", "major"):
                provider.send_sms(p["phone"], f"{title}. {body}"[:300])
            if provider and p.get("phone") and severity == "critical":
                provider.call(p["phone"], f"{title}. {body}"[:300])
        except Exception as exc:   # one broken channel must never stop the others
            log.warning("channel_failed", error=type(exc).__name__)


def deliver(people: list[dict], title: str, body: str, severity: str = "major") -> None:
    """people: [{"phone": ...}, ...]. Sends in the background; never slows alarm handling."""
    people = [p for p in people if p.get("phone")]
    if people:
        threading.Thread(target=_run, args=(people, title, body, severity), daemon=True).start()
