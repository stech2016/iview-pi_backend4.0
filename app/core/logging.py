import logging
import sys

import structlog

SENSITIVE_KEYS = {
    "password", "new_password", "cloud_password", "token", "access_token", "reset_token",
    "authorization", "secret", "secret_key", "fernet_key", "private_key", "value", "values",
    "readings", "sec-websocket-protocol",
}
REDACTED = "***REDACTED***"


def _scrub(obj):
    if isinstance(obj, dict):
        return {k: (REDACTED if str(k).lower() in SENSITIVE_KEYS else _scrub(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(i) for i in obj]
    return obj


def _redact_processor(_, __, event_dict):
    return _scrub(event_dict)


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),  # UTC only
            structlog.processors.format_exc_info,
            _redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "stech"):
    return structlog.get_logger(name)
