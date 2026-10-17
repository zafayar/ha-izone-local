"""Constants for the iZone Local integration."""

from datetime import timedelta

DOMAIN = "izone_local"
MANUFACTURER = "iZone"

CONF_UID = "uid"

SCAN_INTERVAL = timedelta(seconds=15)

# Requested changes are reconciled with the bridge in a loop: sync, compare,
# send the next command, repeat. The bridge reports a change 0-2 s after
# accepting it (measured: zone open 0.5 s, power on 1 s, zone close 1.9 s).
CONFIRM_INTERVAL = 0.4  # how often to re-read while waiting
RESEND_AFTER = 2.5  # re-send a command the bridge hasn't reported by then
MAX_ATTEMPTS = 3  # sends per change before giving up
PENDING_TIMEOUT = 30  # backstop: give up on any change after this long
FAILED_SHOW = 10  # how long a failed change shows "failed"
# A zone whose sensor has dropped out can't take Auto or a temperature until
# the sensor is back; such a change waits this long for it.
SENSOR_WAIT = 12 * 3600
# After running a favourite the controller can take ~35 s to set the zones,
# and ignores separate zone commands meanwhile (measured). Follow-ups for a
# favourite wait this long.
FAVOURITE_SETTLE = 60

# Progress shown in each entity's "status" attribute, with its icon.
STATUS_READY = "ready"
STATUS_QUEUED = "queued"
STATUS_SENDING = "sending"  # shown as "sending (1 of 2)"
STATUS_CONFIRMING = "confirming"
STATUS_FAILED = "failed"
STATUS_WAITING_SENSOR = "waiting for sensor"
# Keyed by the status's first word ("sending (1 of 2)" -> "sending").
STATUS_ICONS = {
    "queued": "mdi:timer-sand",
    "sending": "mdi:sync",
    "confirming": "mdi:sync",
    "failed": "mdi:alert-circle-outline",
    "waiting": "mdi:thermometer-off",
}

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
FAN_QUIET = 6  # shown if the unit reports it; not offered
FAN_TURBO = 7

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

# Zone modes a user has switched on. Constant zones are left out: the system
# opens them itself whenever the unit runs, so they don't keep the unit on.
ZONE_MODES_ON = (ZONE_MODE_OPEN, ZONE_MODE_AUTO, ZONE_MODE_OVERRIDE)


# SensType for a zone with no temperature sensor (e.g. a bypass/constant zone).
# Such zones report SensorFault: 1 and Temp: 0, so skip their sensor entities.
SENS_TYPE_NONE = 255


def sensor_faulted(zone: dict) -> bool:
    """A zone whose sensor has dropped out (not one that has no sensor).

    The controller reports such a zone as closed, and resumes the mode and
    setpoint it remembers once the sensor is back.
    """
    return bool(zone.get("SensorFault")) and zone.get("SensType") != SENS_TYPE_NONE


def zone_is_on(zone: dict) -> bool:
    """Whether a zone keeps the AC running (a faulted zone is closed for now)."""
    return (
        not zone.get("Bypass")
        and zone.get("Mode") in ZONE_MODES_ON
        and not sensor_faulted(zone)
    )

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
