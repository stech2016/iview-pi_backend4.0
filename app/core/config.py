from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENVIRONMENT: str = "development"
    APP_VERSION: str = "2.0.0"
    MIN_APP_VERSION: str = "1.0.0"    # the oldest app the backend still supports; shown to the app at /api/v1/app-info
    # In-app update notice: the app compares ITS version with these and shows "New version - Update". Set them in .env when a new app is published.
    APP_LATEST_VERSION: str = ""       # newest app build (empty = same as APP_VERSION). An app older than MIN_APP_VERSION must update before it can be used.
    APP_DOWNLOAD_WINDOWS: str = ""     # link to the Windows installer
    APP_DOWNLOAD_ANDROID: str = ""     # Play Store link or APK link
    APP_DOWNLOAD_IOS: str = ""         # App Store / TestFlight link
    APP_DOWNLOAD_MACOS: str = ""
    APP_DOWNLOAD_LINUX: str = ""
    LOG_LEVEL: str = "INFO"
    APP_NAME: str = "STECH iVIEW"
    API_PREFIX: str = "/api/v1"

    SECRET_KEY: str
    FERNET_KEY: str
    AUDIT_HMAC_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REMEMBER_DAYS: int = 30           # 'Keep me signed in' on the login screen: the session lasts this many days
    TOTP_ISSUER: str = "STECH iVIEW"   # name shown in the authenticator app (2-step login)

    MASTER_DB_NAME: str = "stech_master"
    PG_HOST: str = "localhost"
    PG_PORT: int = 5432
    PG_ADMIN_USER: str = "postgres"
    PG_ADMIN_PASSWORD: str = "postgres"
    PG_SSLMODE: str = ""  # "" = none (database on the same server). Managed cloud PostgreSQL (Azure/AWS/GCP...): require
    TELEMETRY_COMPRESS_AFTER: str = "7 days"
    TELEMETRY_RETENTION: str = "5 years"

    SPARK_MINUTES: int = 15            # the small line on each dashboard tile shows this many minutes
    # v1.1.5 defaults. Every one can be changed per company on the Settings screen (see services/csettings.py and README_CONFIG.md)
    PLANT_POLL_SECONDS: float = 1.0
    COMMAND_EXPIRY_SECONDS: int = 10
    STUCK_SENSOR_MINUTES: int = 30
    SHELVE_MAX_MINUTES: int = 480
    MAINTENANCE_MAX_HOURS: int = 24
    ALARMS_PER_HOUR_TARGET: int = 12
    ALARMS_PER_HOUR_LIMIT: int = 18
    STALE_ALARM_HOURS: int = 24
    LATE_READING_SECONDS: int = 300   # a reading older than this when it arrives (gateway was cut off) is recorded in the alarm list but pages nobody
    TREND_DEFAULT: str = "8h"
    SHARE_LINK_MAX_DAYS: int = 30
    WRITE_CONFIRM_TOLERANCE_PCT: float = 0.1
    EMAIL_ACTIVATION: bool = False     # true = the company owner must confirm the e-mail address with a 6-digit code before the first login (needs MAIL / BREVO settings)
    ACTIVATION_CODE_MINUTES: int = 30
    TEMP_PASSWORD_E_MAIL: bool = True  # e-mail the one-time password to a new user when mail is set up
    TERMS_VERSION: str = "1.0"         # raise it when the terms text changes; the owner accepts it at sign-up
    TERMS_FILE: str = ""               # your own terms text (a .txt file); empty = the short default text
    POLL_INTERVAL_SECONDS: int = 10
    POLL_TIMEOUT_SECONDS: int = 3
    MODBUS_UNIT_ID: int = 1
    MODBUS_WORD_ORDER: str = "big"  # 32/64-bit values: "big" = high word first (most PLCs), "little" = words swapped

    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "amqp://stech:stech@localhost:5672//"

    MQTT_HOST: str = "localhost"
    MQTT_PORT: int = 1883
    MQTT_USERNAME: str = ""
    MQTT_PASSWORD: str = ""
    # Plant gateways (the small script at each plant) get these from the app automatically after one login.
    MQTT_PUBLIC_HOST: str = ""       # where plants reach the cloud broker, e.g. mqtt.mycompany.com  (empty = feature off)
    MQTT_PUBLIC_PORT: int = 443      # plants connect through the same https door as the app (works behind any firewall)
    MQTT_PUBLIC_TLS: bool = True
    MQTT_PUBLIC_TRANSPORT: str = "websockets"  # "websockets" (through Caddy on 443, default) or "tcp" (direct MQTT port)
    MQTT_PUBLIC_WS_PATH: str = "/mqtt"
    ENROLL_CODE_MINUTES: int = 1440  # STGW4.0 gateway box: how long a one-time setup code stays valid (24 hours)
    MQTT_GATEWAY_USERNAME: str = "stech_gateway"
    MQTT_GATEWAY_PASSWORD: str = ""
    PUBLIC_API_URL: str = ""         # the app's cloud address, e.g. https://iview.mycompany.com  (put inside the plant script's link file)
    GATEWAY_OFFLINE_SECONDS: int = 10  # no heartbeat / no answer for this long = the Offline alarm (a company can change it in Settings). Critical plants 5-10 s, remote sites 30-60 s
    HUB_POLL_SECONDS: int = 5          # Super admin server: how often it checks every region in the background (offline alerts)
    HUB_MAX_REGIONS: int = 12
    DEVICE_STALE_SECONDS: int = 5      # no heartbeat for this long = values turn grey ("stale 7 s") and the write buttons lock; "Offline" follows after GATEWAY_OFFLINE_SECONDS
    # V2.0: customer-owned gateways (any brand) connect straight to the broker on this port (TLS) with a login made for THEIR plant.
    MQTT_ADMIN_USERNAME: str = "stech_admin"   # the broker's Dynamic Security administrator: the cloud makes / removes the plant logins with it
    MQTT_ADMIN_PASSWORD: str = ""
    MQTT_TLS_PORT: int = 8883                  # the port the customer types into the gateway page
    MQTT_TLS_HOST: str = ""                    # the address the customer types (empty = MQTT_PUBLIC_HOST, then the DOMAIN of the server)
    PLANT_LINK_OFFLINE_SECONDS: int = 300      # a customer gateway that sent nothing for this long is shown Offline (Sparkplug NDEATH / MQTT will message: at once)
    MQTT_CA_FILE: str = ""           # path of the broker certificate (server.crt); sent to gateways so they trust it by themselves

    CORS_ORIGINS: str = "http://localhost:3000"
    FRONTEND_URL: str = "http://localhost:3000"

    MAIL_SERVER: str = "smtp-relay.brevo.com"
    MAIL_PORT: int = 587
    MAIL_USERNAME: str = ""
    MAIL_PASSWORD: str = ""
    MAIL_FROM: str = "no-reply@stech-iview.example"
    BREVO_API_KEY: str = ""  # Brevo API key (xkeysib-...): if set, mail is sent through Brevo's HTTPS API, no SMTP login needed
    BREVO_API_URL: str = "https://api.brevo.com/v3"
    RESET_CODE_MINUTES: int = 15      # the 6-digit code in the "forgot password" e-mail works this long

    # Phone notifications (Firebase Cloud Messaging, free). The service-account file of your Firebase project, written on ONE line
    # with:  base64 -w0 firebase-key.json   Empty = phone notifications are off (the in-app alarm banner still works).
    FCM_SERVICE_ACCOUNT_B64: str = ""
    FCM_API_URL: str = "https://fcm.googleapis.com"
    FCM_TOKEN_URL: str = ""           # empty = the token address inside the service-account file

    # Extra alert channel (optional, off while empty)
    SMS_PROVIDER: str = ""           # SMS and voice add-on, OFF by default. "twilio" is built in (one file per provider: app/services/channels/)
    SMS_ACCOUNT: str = ""            # the client's own account id with that provider
    SMS_TOKEN: str = ""              # ... and its secret token
    SMS_FROM: str = ""               # the sender phone number given by the provider
    SMS_API_URL: str = ""            # empty = the provider's normal address
    PLANT_UTC_OFFSET_MINUTES: int = 330   # plant local time for reports and the nightly clean-up (330 = IST)
    PURGE_HOUR: int = 2               # the nightly clean-up of old data runs at this local hour

    # THE remote address of this server - any provider (Tailscale Funnel, Headscale, Cloudflare Tunnel, ngrok, your own domain...).
    # Example: https://plant1.example.com   or   https://stech.tail1234.ts.net:8443
    REMOTE_ADDRESS: str = ""
    TAILSCALE_FUNNEL_URL: str = ""    # old name of REMOTE_ADDRESS, still read so an existing .env keeps working
    REMOTE_ALLOW_HTTP: bool = False   # true = also accept plain http:// addresses (testing on your own network only)
    REMOTE_ALLOWED_HOSTS: str = ""    # empty = any host. Optional lock, e.g. ".ts.net,.mycompany.com" (comma separated endings)

    @model_validator(mode="after")
    def _one_address_line(self):
        # ONE line is enough for the remote address: REMOTE_ADDRESS also becomes the address used in reset-password links
        if not self.REMOTE_ADDRESS and self.TAILSCALE_FUNNEL_URL:
            self.REMOTE_ADDRESS = self.TAILSCALE_FUNNEL_URL
        self.REMOTE_ADDRESS = self.REMOTE_ADDRESS.strip().rstrip("/")
        if not self.PUBLIC_API_URL and self.REMOTE_ADDRESS:
            self.PUBLIC_API_URL = self.REMOTE_ADDRESS
        return self

    @property
    def is_dev(self) -> bool:
        return self.ENVIRONMENT.lower() == "development"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    def local_url(self, database: str) -> URL:
        return URL.create(
            "postgresql+psycopg2", username=self.PG_ADMIN_USER, password=self.PG_ADMIN_PASSWORD,
            host=self.PG_HOST, port=self.PG_PORT, database=database,
            query={"sslmode": self.PG_SSLMODE} if self.PG_SSLMODE else {},
        )

    @property
    def master_url(self) -> URL:
        return self.local_url(self.MASTER_DB_NAME)

    @property
    def local_admin_url(self) -> URL:
        return self.local_url("postgres")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
