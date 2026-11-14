"""The remote address = the public HTTPS address remote locations use to reach this backend. ANY provider works
(Tailscale Funnel, Headscale + proxy, Cloudflare Tunnel, ngrok, your own domain...). Only its shape is checked here.

Example with Tailscale (own port, leaves other funnels alone):  tailscale funnel --bg --https=8443 8000
Save the printed address here (or in REMOTE_ADDRESS in .env); the app uses it as the server address for remote sites.
"""
import ipaddress
import json
import re
from urllib.parse import urlsplit
import urllib.request

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.session import get_master_db
from app.deps import client_ip, get_company_db, get_current_user, require_roles
from app.models.master import RemoteAccess, User
from app.schemas.remote import RemoteAccessIn, RemoteAccessOut, RemoteTestOut
from app.services import audit

router = APIRouter(prefix="/remote-access", tags=["remote-access"])
log = get_logger("remote_access")

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
INVALID = AppError(
    422, "INVALID_REMOTE_ADDRESS",
    "Enter the remote address exactly as your provider shows it, for example https://plant1.example.com or "
    "https://stech.tail1234.ts.net:8443 (address and optional :port only, no path).",
)


def _clean(url: str) -> str:
    """Accepts the address of ANY provider. Checks only the shape (and the optional limits in .env), so changing the
    provider needs no code change."""
    url = url.strip().rstrip("/").lower()
    try:
        u = urlsplit(url)
        host, port = u.hostname or "", u.port
    except ValueError:
        raise INVALID
    schemes = ("https", "http") if settings.REMOTE_ALLOW_HTTP else ("https",)
    if u.scheme not in schemes or not host or u.path or u.query or u.fragment or u.username or u.password:
        raise INVALID
    if not _HOST_RE.match(host):
        raise INVALID
    if not settings.REMOTE_ALLOW_HTTP:  # normal mode: no own-machine / private-network targets (test mode allows them)
        try:
            ip = ipaddress.ip_address(host)
            private = not ip.is_global
        except ValueError:
            private = host == "localhost" or host.endswith((".localhost", ".local", ".internal"))
        if private:
            raise INVALID
    if port is not None and not 1 <= port <= 65535:
        raise INVALID
    allowed = [x.strip().lower() for x in settings.REMOTE_ALLOWED_HOSTS.split(",") if x.strip()]
    if allowed and not any(host == x.lstrip(".") or host.endswith("." + x.lstrip(".")) for x in allowed):
        raise AppError(422, "REMOTE_HOST_NOT_ALLOWED", "This server only accepts addresses ending in: " + ", ".join(allowed))
    return url


def _out(url: str | None, source: str) -> RemoteAccessOut:
    return RemoteAccessOut(funnel_url=url, api_base_url=f"{url}{settings.API_PREFIX}" if url else None, source=source)


@router.get("", response_model=RemoteAccessOut)
def get_remote(user: User = Depends(get_current_user), master: Session = Depends(get_master_db)):
    row = master.scalar(select(RemoteAccess).where(RemoteAccess.company_name == user.company_name))
    if row:
        return _out(row.funnel_url, "company")
    if settings.REMOTE_ADDRESS:
        return _out(settings.REMOTE_ADDRESS, "server_default")
    return _out(None, "none")


@router.put("", response_model=RemoteAccessOut)
def set_remote(body: RemoteAccessIn, request: Request, user: User = Depends(require_roles("admin")),
               master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    url = _clean(body.funnel_url)
    row = master.scalar(select(RemoteAccess).where(RemoteAccess.company_name == user.company_name))
    if row:
        row.funnel_url = url
    else:
        master.add(RemoteAccess(company_name=user.company_name, funnel_url=url))
    master.commit()
    log.info("remote_access_updated", company=user.company_name)
    audit.append(db, user.id, user.company_name, "remote_access.update", url, client_ip(request))
    return _out(url, "company")


@router.delete("", response_model=RemoteAccessOut)
def clear_remote(request: Request, user: User = Depends(require_roles("admin")),
                 master: Session = Depends(get_master_db), db: Session = Depends(get_company_db)):
    row = master.scalar(select(RemoteAccess).where(RemoteAccess.company_name == user.company_name))
    if row:
        master.delete(row)
        master.commit()
        audit.append(db, user.id, user.company_name, "remote_access.clear", "funnel", client_ip(request))
    return _out(None, "none")


@router.post("/test", response_model=RemoteTestOut)
def test_remote(body: RemoteAccessIn, user: User = Depends(require_roles("admin"))):
    """Checks that the remote address really reaches THIS backend (any provider)."""
    url = _clean(body.funnel_url)
    try:
        with urllib.request.urlopen(f"{url}{settings.API_PREFIX}/healthz", timeout=6) as r:  # nosec B310
            ok = r.status == 200 and json.load(r).get("status") == "ok"
        if ok:
            return RemoteTestOut(reachable=True, detail="Funnel address reached the STECH iVIEW backend.")
        return RemoteTestOut(reachable=False, detail="The address answered, but not like STECH iVIEW. Check the funnel target port.")
    except Exception:
        return RemoteTestOut(
            reachable=False,
            detail="Could not reach that address. Make sure your remote-access tool (Tailscale Funnel, tunnel, reverse proxy...) "
                   "is running on the server and points to this backend.",
        )
