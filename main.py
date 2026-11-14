"""Start the STECH iVIEW backend:   python main.py

(The FastAPI app itself lives in app/main.py; this file just launches it with uvicorn.)
"""
import os

import uvicorn

from app.core.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),   # 0.0.0.0 = reachable from your phone on the same Wi-Fi
        port=int(os.getenv("PORT", "8000")),
        reload=settings.is_dev,               # auto-restart on code changes (development only)
        reload_dirs=["app"] if settings.is_dev else None,
        proxy_headers=True,                   # correct client IP behind Tailscale Funnel / nginx
        forwarded_allow_ips="*",
        access_log=False,                     # our structlog middleware logs requests (no secrets)
    )
