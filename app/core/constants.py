"""Shared constants. The Flutter app mirrors these lists (lib/utils/constants.dart)."""
import re

REGIONS = ["North", "South", "East", "West"]
ROLES = ["admin", "regional_admin", "operator", "guest"]  # admin = Super admin (whole company)
# Every way Modbus is used: over Ethernet (TCP, RTU over TCP, UDP, TLS) and over a serial cable (RTU, ASCII on RS-485 / RS-422 / RS-232).
MODBUS_NETWORK = ["Modbus TCP", "Modbus RTU over TCP", "Modbus UDP", "Modbus TLS"]
MODBUS_SERIAL = ["Modbus RTU", "Modbus ASCII"]
MODBUS_ALL = MODBUS_NETWORK + MODBUS_SERIAL
PROTOCOLS = MODBUS_ALL + ["MQTT", "OPC-UA"]
SEVERITIES = ["critical", "major", "minor", "advisory"]  # ISA-18.2 style priorities
STORAGE_MODES = ["local", "cloud"]

INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat",
    "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh",
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab",
    "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh",
    "Uttarakhand", "West Bengal", "Andaman and Nicobar Islands", "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir", "Ladakh",
    "Lakshadweep", "Puducherry",
]

DB_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
COMPANY_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
RESERVED_DBS = {"postgres", "template0", "template1"}

EXPORT_MAX_DAYS = 365
EXPORT_MAX_BYTES = 100 * 1024 * 1024
EXPORT_LIMIT_MESSAGE = (
    "The requested export exceeds limits (max date range: 1 year, max size: 100 MB). "
    "Please narrow your date filter and try again."
)
GENESIS_HASH = "0" * 64
