from fastapi import APIRouter

from app.api.v1 import (account, alarms, alert_rules, audit, auth, company_settings, control, devices, gateway, health, oncall, plant, plants, setup, share,
                        remote_access, reports, retention, storage, tiles, users, ws, downtime, batches, screens, energy)

api_router = APIRouter()
for module in (auth, account, users, storage, retention, devices, tiles, control, company_settings, alarms, alert_rules, oncall, reports, audit,
               remote_access, gateway, plants, health, plant, setup, share, downtime, batches, screens, energy, ws):
    api_router.include_router(module.router)
api_router.include_router(share.public)
