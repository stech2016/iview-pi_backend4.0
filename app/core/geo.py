"""Countries and their States / provinces for the sign-up and device lists (free ISO 3166 data from the open-source pycountry,
LGPL-2.1). India keeps its own curated list; other names have their accents removed so they can be typed and searched easily."""
import unicodedata
from functools import lru_cache

import pycountry

from app.core.constants import INDIAN_STATES

DEFAULT_COUNTRY = "India"
_TOP_LEVEL_SKIP = {"Dependency", "Outlying area"}   # keep it to real first-level divisions


def _plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).strip()


@lru_cache(maxsize=1)
def countries() -> list[str]:
    names = sorted({_plain(c.name) for c in pycountry.countries})
    return names


@lru_cache(maxsize=1)
def _by_country() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    subs: dict[str, list] = {}
    for s in pycountry.subdivisions:
        subs.setdefault(s.country_code, []).append(s)
    for c in pycountry.countries:
        name = _plain(c.name)
        rows = subs.get(c.alpha_2, [])
        top = [s for s in rows if not getattr(s, "parent_code", None)]
        names = sorted({_plain(s.name) for s in top})
        out[name] = names or [name]     # no divisions listed: the country itself is the only choice
    out[DEFAULT_COUNTRY] = list(INDIAN_STATES)
    return out


def states(country: str) -> list[str]:
    return list(_by_country().get(country, []))


@lru_cache(maxsize=1)
def _all_states() -> frozenset:
    return frozenset(s for v in _by_country().values() for s in v)


def is_country(name: str) -> bool:
    return name in _by_country()


def is_state(name: str, country: str | None = None) -> bool:
    """A State of this country (when given) or of any country."""
    if country:
        return name in _by_country().get(country, ())
    return name in _all_states()
