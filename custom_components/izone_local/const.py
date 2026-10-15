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

MIN_TEMP = 15.0
MAX_TEMP = 30.0
TEMP_STEP = 0.5
