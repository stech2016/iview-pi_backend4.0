"""Checks the e-mail setup and says exactly what is wrong.   Run on the server:
   sudo docker compose exec backend python scripts/test_mail.py you@example.com
"""
import sys
import urllib.error

sys.path.insert(0, ".")
from app.core.config import settings  # noqa: E402
from app.services import mail  # noqa: E402

if len(sys.argv) < 2:
    sys.exit("Usage: python scripts/test_mail.py you@example.com")

if not mail.configured():
    sys.exit("NOT SET UP: BREVO_API_KEY is empty in .env. Add it (see README: 'E-mail with Brevo'), then run: sudo docker compose up -d --force-recreate backend ingest")
print("Brevo key found, length", len(settings.BREVO_API_KEY.strip()), "(not shown)")
try:
    print("Sender used:", mail._sender_email())
    mail.send_now(sys.argv[1], "STECH iVIEW test e-mail", "<p>If you can read this, forgot-password e-mails will work.</p>")
    print("SENT. Check the inbox (and Spam) of", sys.argv[1])
except urllib.error.HTTPError as exc:
    why = mail.brevo_reason(exc)
    print(f"FAILED: Brevo answered {exc.code}: {why}")
    if exc.code == 401:
        print("-> The key is wrong, OR Brevo blocks this server's IP: Brevo > Settings > Security > Authorised IPs > add this server's IP (or Deactivate blocking).")
    if exc.code == 400:
        print("-> The sender e-mail is not verified in Brevo. Brevo > Senders, Domains > Add a sender > click the link in the mail, then set MAIL_FROM to that address in .env.")
    sys.exit(1)
except Exception as exc:
    print("FAILED:", type(exc).__name__, exc)
    sys.exit(1)
