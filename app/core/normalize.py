"""Typed or imported names are matched ignoring capital letters, spaces, dashes, dots and underscores, then replaced by the standard name.

    "modbus_tcp", "MODBUS TCP", "Modbus-Tcp"  ->  "Modbus TCP"      "opcua"  ->  "OPC-UA"      "south" -> "South"

What is stored and shown is always the standard name. A value that matches nothing is returned unchanged, so the normal "must be one of ..." message appears.
"""
import re

from app.core import geo
from app.core.constants import PROTOCOLS, REGIONS

_ALIASES = {"none": "N", "even": "E", "odd": "O", "bigendian": "big", "littleendian": "little"}
SEVERITIES = ["critical", "major", "minor", "advisory"]
REGISTER_TYPES = ["coil", "discrete", "input", "holding"]
ORDERS = ["big", "little"]
PARITIES = ["N", "E", "O"]


def _key(text: str) -> str:
    return re.sub(r"[\s_\-./]+", "", text).lower()


def canon(value, choices):
    """The standard spelling of value when it matches one of the choices (any case, any separators); otherwise value unchanged."""
    if not isinstance(value, str):
        return value
    k = _key(value)
    for c in choices:
        if _key(c) == k:
            return c
    return value.strip()


def protocol(v):
    return canon(v, PROTOCOLS)


def region(v):
    return canon(v, REGIONS)


def severity(v):
    return canon(v, SEVERITIES)


def register_type(v):
    return canon(v, REGISTER_TYPES)


def order(v):
    if isinstance(v, str) and _key(v) in _ALIASES:
        return _ALIASES[_key(v)]
    return canon(v, ORDERS)


def parity(v):
    if isinstance(v, str) and _key(v) in _ALIASES:
        return _ALIASES[_key(v)]
    return canon(v, PARITIES)


def data_type(v):
    from app.schemas.device import DATA_TYPES
    return canon(v, sorted(DATA_TYPES))


def country(v):
    return canon(v, geo.countries())


def state(v, country_name: str | None = None):
    """A State of the given country when it is known, otherwise of any country."""
    if not isinstance(v, str):
        return v
    pool = geo.states(country_name) if country_name and geo.is_country(country_name) else sorted(geo._all_states())
    return canon(v, pool)


def place(country_name: str, state_name: str, region_name: str) -> tuple[str, str, str]:
    c = country(country_name)
    return c, state(state_name, c), region(region_name)
