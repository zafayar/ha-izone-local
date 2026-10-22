"""Temperature and status sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import from_centi
from .const import (
    CTRL_ZONE_UNIT,
    SENS_TYPE_NONE,
    SENS_TYPE_WIRELESS,
    firmware_label,
    sensor_faulted,
    zone_name,
)
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity, IZoneZoneEntity

# Each platform's updates are read by the coordinator, and commands are
# queued by the client, so entities needn't be limited here.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class IZoneSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    needs_sensor: bool = False  # skip on zones with no temperature sensor
    wireless_only: bool = False  # only zones with a battery-powered sensor


BATTERY_LEVELS = {0: "full", 1: "half", 2: "empty"}  # BatteryLevel_e
SIGNAL_LEVELS = {0: "full", 1: "half", 2: "quarter", 3: "none"}  # RfSignalLevel_e


def _temp(key: str) -> Callable[[dict[str, Any]], float | None]:
    return lambda d: from_centi(d.get(key)) if d.get(key) else None


SYSTEM_SENSORS = (
    IZoneSensorDescription(
        key="supply_temperature",
        translation_key="supply_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=_temp("Supply"),
    ),
    IZoneSensorDescription(
        key="return_temperature",
        translation_key="return_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=_temp("Temp"),
    ),
    IZoneSensorDescription(
        key="ac_error_code",
        translation_key="ac_error_code",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (d.get("ACError") or "").strip() or None,
    ),
    IZoneSensorDescription(
        key="run_hours",
        translation_key="run_hours",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Not in the published spec, but reported by the controller.
        value_fn=lambda d: d.get("AcRunH"),
    ),
)

ZONE_SENSORS = (
    IZoneSensorDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=_temp("Temp"),
        needs_sensor=True,
    ),
    IZoneSensorDescription(
        key="damper_position",
        translation_key="damper_position",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda d: d.get("DmpPos"),
    ),
    IZoneSensorDescription(
        key="sensor_battery",
        translation_key="sensor_battery",
        device_class=SensorDeviceClass.ENUM,
        options=list(BATTERY_LEVELS.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: BATTERY_LEVELS.get(d.get("BattVolt")),
        wireless_only=True,
    ),
    IZoneSensorDescription(
        key="sensor_signal",
        translation_key="sensor_signal",
        device_class=SensorDeviceClass.ENUM,
        options=list(SIGNAL_LEVELS.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        # A weakening signal is the early warning before a sensor drops out.
        value_fn=lambda d: SIGNAL_LEVELS.get(d.get("RfSignal")),
        wireless_only=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        IZoneSystemSensor(coordinator, desc) for desc in SYSTEM_SENSORS
    ]
    entities.append(IZoneControlZoneSensor(coordinator))
    entities.extend(
        IZoneFirmwareSensor(coordinator, module) for module in coordinator.firmware
    )
    for index, zone in coordinator.data.zones.items():
        has_sensor = zone.get("SensType") != SENS_TYPE_NONE
        wireless = zone.get("SensType") == SENS_TYPE_WIRELESS
        entities.extend(
            IZoneZoneSensor(coordinator, index, desc)
            for desc in ZONE_SENSORS
            if (has_sensor or not desc.needs_sensor)
            and (wireless or not desc.wireless_only)
        )
    async_add_entities(entities)


class IZoneSystemSensor(IZoneEntity, SensorEntity):
    entity_description: IZoneSensorDescription

    def __init__(
        self, coordinator: IZoneCoordinator, description: IZoneSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.system)


class IZoneControlZoneSensor(IZoneEntity, SensorEntity):
    """The zone whose sensor the unit is running from, or "unit".

    The controller moves it between zones as each nears its setpoint.
    """

    _attr_translation_key = "control_zone"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: IZoneCoordinator) -> None:
        super().__init__(coordinator, "control_zone")

    @property
    def native_value(self) -> str | None:
        index = self.system.get("CtrlZone")
        if index == CTRL_ZONE_UNIT:
            return "unit"
        zone = self.coordinator.data.zones.get(index)
        return zone_name(zone, index) if zone is not None else None


class IZoneFirmwareSensor(IZoneEntity, SensorEntity):
    """Firmware version of one module (hub, wall controller, board).

    Updates can only be installed from the iZone app; this shows what's
    installed. Read every few hours.
    """

    _attr_translation_key = "firmware"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Rarely needed: the hub's version is also on the AC's device page.
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: IZoneCoordinator, module: str) -> None:
        label, key = firmware_label(module)
        super().__init__(coordinator, f"firmware_{key}")
        self._module = module
        self._attr_translation_placeholders = {"module": label}

    @property
    def native_value(self) -> str | None:
        return self.coordinator.firmware.get(self._module)


class IZoneZoneSensor(IZoneZoneEntity, SensorEntity):
    entity_description: IZoneSensorDescription

    def __init__(
        self,
        coordinator: IZoneCoordinator,
        index: int,
        description: IZoneSensorDescription,
    ) -> None:
        super().__init__(coordinator, index, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        if self.entity_description.needs_sensor and sensor_faulted(self.zone):
            return None  # a dropped-out sensor's last value is stale
        return self.entity_description.value_fn(self.zone)
