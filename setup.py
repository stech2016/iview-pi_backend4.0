#!/usr/bin/env python3
"""STECH iVIEW cloud setup. Run once on the server:   python3 setup.py
Asks 3 short things, creates the secret keys and writes .env. Needs nothing installed except Python 3 (and Docker to run)."""
import base64
import os
import re
import secrets
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"


def ask(question: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    answer = input(f"{question}{shown}: ").strip()
    return answer or default


def main() -> int:
    if ENV.exists():
        print(".env already exists - not touched (changing the secret keys would log everybody out and lock stored data).")
        print("To change a value, edit .env with a text editor, then run:  docker compose up -d")
        return 0
    print("\nSTECH iVIEW cloud setup\n=======================")
    domain = ""
    while not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}", domain):
        domain = ask("1/3  Your server's web address, without https://  (example: iview.mycompany.com)").lower()
    print("\n2/3  Database")
    print("     1 = on this same server (simplest, nothing else to buy)")
    print("     2 = a managed cloud database (Azure / AWS / Google / DigitalOcean PostgreSQL)")
    pg = {"host": "db", "port": "5432", "user": "postgres", "password": secrets.token_urlsafe(24), "ssl": "", "profile": "localdb"}
    if ask("     Choose", "1") == "2":
        pg = {"host": ask("     Database host (from your cloud's database page)"), "port": ask("     Port", "5432"),
              "user": ask("     Admin user name"), "password": ask("     Admin password"), "ssl": "require", "profile": ""}
        if not pg["host"] or not pg["user"] or not pg["password"]:
            print("Host, user and password are needed. Run setup.py again.")
            return 1
    print("\n3/3  E-mail for 'forgot password' (free Brevo account). Press Enter to skip and add it later.")
    brevo = ask("     Brevo API key")
    mail_from = ask("     Sender e-mail verified in Brevo") if brevo else ""

    lines = [
        "# Made by setup.py. Keep this file private. NEVER change the three keys below once people use the app.",
        "ENVIRONMENT=production", "LOG_LEVEL=INFO", "",
        f"DOMAIN={domain}", f"PUBLIC_API_URL=https://{domain}", f"FRONTEND_URL=https://{domain}", f"CORS_ORIGINS=https://{domain}", "",
        f"SECRET_KEY={secrets.token_hex(32)}",
        f"FERNET_KEY={base64.urlsafe_b64encode(os.urandom(32)).decode()}",
        f"AUDIT_HMAC_KEY={secrets.token_hex(32)}", "",
        "# --- DATABASE (change these 4 lines to move to another cloud database) ---",
        f"PG_HOST={pg['host']}", f"PG_PORT={pg['port']}", f"PG_ADMIN_USER={pg['user']}", f"PG_ADMIN_PASSWORD={pg['password']}",
        f"PG_SSLMODE={pg['ssl']}", "MASTER_DB_NAME=stech_master", f"COMPOSE_PROFILES={pg['profile']}", "",
        "# --- inside the server (no need to touch) ---",
        "REDIS_URL=redis://valkey:6379/0", "MQTT_HOST=mosquitto", "MQTT_PORT=1883", "MQTT_USERNAME=stech_backend",
        f"MQTT_PASSWORD={secrets.token_urlsafe(24)}",
        f"MQTT_PUBLIC_HOST={domain}", "MQTT_PUBLIC_PORT=443", "MQTT_PUBLIC_TLS=true", "MQTT_PUBLIC_TRANSPORT=websockets", "MQTT_PUBLIC_WS_PATH=/mqtt",
        "MQTT_GATEWAY_USERNAME=stech_gateway", f"MQTT_GATEWAY_PASSWORD={secrets.token_urlsafe(24)}", "",
        "# --- V2.0: customers' own gateways (any brand) connect to this address on port 8883 (TLS). The cloud makes one login per gateway. ---",
        f"MQTT_TLS_HOST={domain}", "MQTT_TLS_PORT=8883", "MQTT_ADMIN_USERNAME=stech_admin", f"MQTT_ADMIN_PASSWORD={secrets.token_urlsafe(24)}", "",
        "# --- e-mail (forgot password) ---",
        f"BREVO_API_KEY={brevo}", f"MAIL_FROM={mail_from or 'no-reply@stech-iview.example'}", "",
        "# --- PLC reading defaults for the plant script ---",
        "MODBUS_UNIT_ID=1", "MODBUS_WORD_ORDER=big", "",
    ]
    ENV.write_text("\n".join(lines))
    try:
        ENV.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    print("\n.env created.\n")
    print("Next:")
    print(f"  1. Make sure the name {domain} points to this server's public IP address (DNS), and ports 80, 443 and 8883 are open.")
    print("  2. Start everything:   docker compose up -d --build")
    print(f"  3. Check:              https://{domain}/api/v1/healthz   (should show ok)")
    print("  4. In the app, type this address on the first screen:  " + f"https://{domain}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
