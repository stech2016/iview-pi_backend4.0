"""The viewer's own time zone. The app sends the offset of the phone / PC it runs on with every request (header X-Tz-Offset, minutes
east of UTC, e.g. 330 for India). Everything the SERVER writes as text (reports, PDF headers, file names) then uses that offset, so a
report shows the time of the person who asked for it - wherever in the world. No header (a script, an old app) = the plant default."""
from contextvars import ContextVar

_offset: ContextVar[int | None] = ContextVar("viewer_tz_offset", default=None)


def set_offset(minutes: int | None):
    return _offset.set(minutes)


def offset_minutes(default: int) -> int:
    v = _offset.get()
    return default if v is None else v


class TimezoneMiddleware:
    """Pure ASGI middleware (cheap): reads X-Tz-Offset once per request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            for key, value in scope.get("headers", ()):
                if key == b"x-tz-offset":
                    try:
                        minutes = int(value)
                        if -840 <= minutes <= 840:
                            set_offset(minutes)
                    except ValueError:
                        pass
                    break
        await self.app(scope, receive, send)
