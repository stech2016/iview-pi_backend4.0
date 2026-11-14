"""What a person may do: a job title pre-ticks one level per area, and the Super admin can change any box.

Areas (each one level: 0 None, 1 Read, 2 Read and Write)
  monitor   Read: dashboard, devices, trends, plant map.   Write: also maintenance mode, tile main sensors, shift hand-over notes.
  alarms    Read: see alarms.                               Write: acknowledge, comment, shelve.
  config    Read: see the setup and ASK for a change (an administrator approves it).   Write: change devices and sensors directly.
  control   Read: see the command history.                  Write: send values / commands to the plant (the company must also allow writing).
  reports   Read: view and export reports.                  Write: also make read-only share links and QR labels.
  users     Read: see the people of own area.               Write: create, change, deactivate people of own area ("Manage users").
The Super admin (the company owner) always has Read and Write everywhere. Add an area or a job title = one line below.
"""
import json

from fastapi import Depends

from app.core.errors import AppError
from app.deps import get_current_user
from app.models.master import User

AREAS = ("monitor", "alarms", "config", "control", "reports", "users")
LEVEL_NAME = {0: "None", 1: "Read", 2: "Read and Write"}

#                       monitor alarms config control reports users
TITLES: dict[str, tuple[str, tuple[int, int, int, int, int, int]]] = {
    "Admin":           ("regional_admin", (2, 2, 2, 2, 2, 0)),
    "Engineer":        ("operator",       (2, 2, 2, 2, 1, 0)),
    "Supervisor":      ("operator",       (2, 2, 1, 2, 2, 0)),
    "Senior operator": ("operator",       (2, 2, 1, 2, 1, 0)),
    "Operator":        ("operator",       (2, 2, 1, 1, 1, 0)),
    "Guest":           ("guest",          (1, 1, 0, 0, 0, 0)),
}
# people created before v1.1.5 have no job title: their role decides (a Regional admin keeps managing the people of the own area)
LEGACY = {"admin": (2, 2, 2, 2, 2, 2), "regional_admin": (2, 2, 2, 2, 2, 2), "operator": TITLES["Operator"][1],
          "guest": TITLES["Guest"][1]}


def preset(title: str) -> dict[str, int]:
    return dict(zip(AREAS, TITLES[title][1]))


def role_of(title: str) -> str:
    return TITLES[title][0]


def effective(user: User) -> dict[str, int]:
    """The level of every area for this person."""
    if user.role == "admin":
        return {a: 2 for a in AREAS}
    if user.perms:
        try:
            raw = json.loads(user.perms)
            return {a: min(2, max(0, int(raw.get(a, 0)))) for a in AREAS}
        except (ValueError, TypeError, AttributeError):
            pass
    return dict(zip(AREAS, LEGACY.get(user.role, (0,) * 6)))


def level(user: User, area: str) -> int:
    return effective(user)[area]


def can(user: User, area: str, need: int = 1) -> bool:
    return level(user, area) >= need


def clean(raw: dict | None) -> dict[str, int]:
    """Levels typed by a person -> the six areas, each 0 / 1 / 2. Unknown areas or levels are refused."""
    raw = raw or {}
    unknown = [k for k in raw if k not in AREAS]
    if unknown:
        raise AppError(422, "UNKNOWN_AREA", f"There is no area called '{unknown[0]}'.")
    out = {}
    for a in AREAS:
        v = raw.get(a, 0)
        if isinstance(v, bool) or v not in (0, 1, 2):
            raise AppError(422, "BAD_LEVEL", "Each area must be None (0), Read (1) or Read and Write (2).")
        out[a] = v
    return out


def store(levels: dict[str, int]) -> str:
    return json.dumps({a: levels[a] for a in AREAS})


def require(area: str, need: int = 1):
    """FastAPI dependency: the caller must have at least this level in this area."""
    def dep(user: User = Depends(get_current_user)) -> User:
        if not can(user, area, need):
            what = "see this" if need <= 1 else "do this"
            raise AppError(403, "NO_PERMISSION",
                           f"Your account cannot {what}: the '{area}' area is set to {LEVEL_NAME[level(user, area)]} and needs {LEVEL_NAME[need]}. "
                           "Ask your Super admin to change it.")
        return user
    return dep
