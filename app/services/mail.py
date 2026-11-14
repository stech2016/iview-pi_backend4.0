import asyncio
import base64
import json
import os
import tempfile
import urllib.error
import urllib.request

from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("mail")



def _conf() -> ConnectionConfig:
    return ConnectionConfig(
        MAIL_USERNAME=settings.MAIL_USERNAME, MAIL_PASSWORD=settings.MAIL_PASSWORD,
        MAIL_FROM=settings.MAIL_FROM, MAIL_PORT=settings.MAIL_PORT, MAIL_SERVER=settings.MAIL_SERVER,
        MAIL_STARTTLS=True, MAIL_SSL_TLS=False, USE_CREDENTIALS=True, VALIDATE_CERTS=True,
        MAIL_FROM_NAME="STECH iVIEW",
    )


_sender_cache: str | None = None


def _brevo_get(path: str) -> dict:
    req = urllib.request.Request(settings.BREVO_API_URL.rstrip("/") + path, headers={
        "api-key": settings.BREVO_API_KEY.strip(), "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310
        return json.loads(resp.read().decode())


def _sender_email() -> str:
    """MAIL_FROM if you set one; otherwise the first active sender of the Brevo account (found with the API key)."""
    global _sender_cache
    if settings.MAIL_FROM and not settings.MAIL_FROM.endswith(".example"):
        return settings.MAIL_FROM
    if _sender_cache:
        return _sender_cache
    try:
        senders = [x for x in _brevo_get("/senders").get("senders", []) if x.get("active", True)]
        if senders:
            _sender_cache = senders[0]["email"]
    except Exception:
        pass
    if not _sender_cache:
        _sender_cache = _brevo_get("/account")["email"]  # the account owner's own address
    return _sender_cache


def _send_via_brevo_api(to: str, subject: str, html: str, attachment: tuple[str, bytes, str] | None = None) -> None:
    """Brevo transactional e-mail over HTTPS. Needs only the API key (header `api-key`)."""
    payload = {"sender": {"name": "STECH iVIEW", "email": _sender_email()},
               "to": [{"email": to}], "subject": subject, "htmlContent": html}
    if attachment:
        payload["attachment"] = [{"name": attachment[0], "content": base64.b64encode(attachment[1]).decode()}]
    req = urllib.request.Request(
        settings.BREVO_API_URL.rstrip("/") + "/smtp/email", data=json.dumps(payload).encode(), method="POST",
        headers={"api-key": settings.BREVO_API_KEY.strip(), "content-type": "application/json", "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310
        resp.read()


def configured() -> bool:
    return bool(settings.BREVO_API_KEY.strip() or settings.MAIL_USERNAME)


def send_now(to: str, subject: str, html: str, attachment: tuple[str, bytes, str] | None = None) -> None:
    """Send one e-mail (blocking). Raises on failure. attachment = (file name, bytes, media type), used by the scheduled reports."""
    if settings.BREVO_API_KEY.strip():
        _send_via_brevo_api(to, subject, html, attachment)
        return
    with tempfile.TemporaryDirectory() as folder:
        files = []
        if attachment:
            path = os.path.join(folder, os.path.basename(attachment[0]))
            with open(path, "wb") as f:
                f.write(attachment[1])
            files = [path]
        asyncio.run(FastMail(_conf()).send_message(MessageSchema(subject=subject, recipients=[to], body=html, subtype=MessageType.html, attachments=files)))


def brevo_reason(exc: urllib.error.HTTPError) -> str:
    """The plain reason Brevo gives (for example: unrecognised IP address, sender not valid)."""
    try:
        return json.loads(exc.read().decode()).get("message", "")[:300]
    except Exception:
        return ""


async def send_reset_email(to: str, code: str | None, token: str | None = None) -> None:
    if not configured():
        log.warning("mail_not_configured", hint="Set BREVO_API_KEY (and MAIL_FROM) in .env to send recovery emails")
        return
    minutes = settings.RESET_CODE_MINUTES
    subject = "STECH iVIEW - reset your password"
    base = (settings.PUBLIC_API_URL or settings.FRONTEND_URL).rstrip("/")
    link = f"{base}/reset-password?token={token}" if token and "localhost" not in base and base else None
    parts = ["<h3>STECH iVIEW password reset</h3>"]
    if link:
        parts.append("<p>Tap the button to choose a new password (it opens a page in your browser):</p>"
                     f"<p><a href=\"{link}\" style=\"background:#5E94C7;color:#fff;padding:12px 22px;border-radius:8px;"
                     "text-decoration:none;font-weight:bold;display:inline-block\">Reset my password</a></p>"
                     f"<p style=\"font-size:12px;color:#666\">Button not working? Copy this address into your browser:<br>{link}</p>")
    if code:
        parts.append("<p>Or, in the app: Forgot password &rarr; <b>I have a code</b>, and type this code with your email:</p>"
                     f"<p style=\"font-size:30px;letter-spacing:6px;font-weight:bold\">{code}</p>")
    elif token and not link:
        parts.append(f"<p>Copy this token into the app (Forgot password &rarr; I have a code):</p><p><code>{token}</code></p>")
    parts.append(f"<p>The link and the code work for {minutes} minutes and only once. "
                 "If you did not ask for this, ignore this email. Your password stays the same.</p>")
    html = "".join(parts)
    try:
        await asyncio.to_thread(send_now, to, subject, html)
        log.info("reset_email_sent")
    except urllib.error.HTTPError as exc:
        # 401 = wrong API key or this server's IP is not allowed in Brevo; 400 = sender e-mail not verified in Brevo
        log.error("reset_email_failed", error="BrevoHTTP", status=exc.code, reason=brevo_reason(exc))
    except Exception as exc:
        log.error("reset_email_failed", error=type(exc).__name__)


async def send_activation_email(to: str, code: str | None) -> None:
    if not configured() or not code:
        return
    html = ("<h3>STECH iVIEW - confirm your e-mail</h3><p>Type this code in the app to activate your account:</p>"
            f"<p style=\"font-size:30px;letter-spacing:6px;font-weight:bold\">{code}</p>"
            f"<p>The code works for {settings.ACTIVATION_CODE_MINUTES} minutes and only once. If you did not sign up, ignore this e-mail.</p>")
    try:
        await asyncio.to_thread(send_now, to, "STECH iVIEW - your activation code", html)
    except Exception as exc:
        log.error("activation_email_failed", error=type(exc).__name__)


def send_new_user_email(to: str, username: str, password: str, company: str, by: str) -> bool:
    """Login details for a person the Super admin (or an admin) just created. True when the mail was handed to the mail service."""
    if not configured():
        return False
    base = (settings.PUBLIC_API_URL or settings.FRONTEND_URL).rstrip("/")
    html = (f"<h3>Your STECH iVIEW account</h3><p>{by} created an account for you in <b>{company}</b>.</p>"
            f"<p>Username: <b>{username}</b><br>One-time password: <b style=\"font-family:monospace\">{password}</b></p>"
            "<p>Log in once and choose your own password. The one-time password works only for that.</p>"
            + (f"<p>Server address: {base}</p>" if base and "localhost" not in base else ""))
    try:
        send_now(to, "STECH iVIEW - your login details", html)
        return True
    except Exception as exc:
        log.error("new_user_email_failed", error=type(exc).__name__)
        return False
