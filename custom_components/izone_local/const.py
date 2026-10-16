"""Constants for the iZone Local integration."""

from datetime import timedelta

DOMAIN = "izone_local"
MANUFACTURER = "iZone"

CONF_UID = "uid"

SCAN_INTERVAL = timedelta(seconds=15)

# SysMode_e
MODE_COOL = 1
MODE_HEAT = 2
MODE_VENT = 3
MODE_DRY = 4
MODE_AUTO = 5
MODE_EXHAUST = 6
MODE_PUMP_ONLY = 7

# SysFan_e
FAN_LOW = 1
FAN_MED = 2
FAN_HIGH = 3
FAN_AUTO = 4
FAN_TOP = 5

# ZoneMode_e
ZONE_MODE_OPEN = 1
ZONE_MODE_CLOSE = 2
ZONE_MODE_AUTO = 3
ZONE_MODE_OVERRIDE = 4
ZONE_MODE_CONSTANT = 5

# ZoneType_e
ZONE_TYPE_OPEN_CLOSE = 1
ZONE_TYPE_CONSTANT = 2
ZONE_TYPE_AUTO = 3

# SensType for a zone with no temperature sensor (e.g. a bypass/constant zone).
# Such zones report SensorFault: 1 and Temp: 0, so skip their sensor entities.
SENS_TYPE_NONE = 255

# CtrlZone value meaning "the unit's own setpoint is in control"
CTRL_ZONE_UNIT = 15

# OemMake_e (iZone_JSON_datastrings.h): the brand the system is sold under.
OEM_MAKES = {
    0: "iZone",
    1: "MyZone",
    2: "Westaflex",
    3: "Kelvinator",
    4: "Consolidated",
    5: "T-Zone",
    6: "RayZone",
    7: "Coolbreeze",
    8: "Rinnai",
}

# UnitBrand_e: the AC unit the system detected (AcUnitBrand). 12 is "Unknown".
UNIT_BRANDS = {
    1: "Panasonic/Toshiba",
    2: "Daikin",
    3: "Mitsubishi Electric",
    4: "LG (301)",
    5: "LG (310)",
    6: "Fujitsu",
    7: "Samsung",
    8: "Temperzone",
    9: "Mitsubishi Heavy Industries",
    10: "Gas heat with add-on cooling",
    11: "Generic",
    13: "Hitachi",
    14: "AA Gen III",
    15: "Fujitsu (Intesis)",
    16: "LG (485)",
    17: "York",
    18: "Haier",
    19: "LG Coolink",
    20: "Braemar",
    21: "Samsung",
    22: "Rinnai Networker",
    23: "Coolbreeze evaporative",
    24: "iZone split system",
    25: "Daikin split system",
    26: "Rinnai 20kW",
    27: "Rinnai split system",
    28: "Gree",
    29: "Haier",
    30: "Brivis",
    31: "Hisense",
    32: "Gree",
    33: "TCL",
    34: "Dominator",
}


def brand_name(system: dict) -> str:
    """The system's brand (e.g. "Rinnai"), falling back to "iZone"."""
    return OEM_MAKES.get(system.get("OemMake"), MANUFACTURER)


def system_name(system: dict) -> str:
    """The name shown on the wall controller (Tag1), or the brand."""
    tag = str(system.get("Tag1") or "").strip()
    return tag or brand_name(system)


def unit_model(system: dict) -> str:
    """The AC unit's model (e.g. "Rinnai 20kW"), or a generic name."""
    return UNIT_BRANDS.get(system.get("AcUnitBrand")) or (
        f"{brand_name(system)} air conditioning"
    )


MIN_TEMP = 15.0
MAX_TEMP = 30.0
TEMP_STEP = 0.5
