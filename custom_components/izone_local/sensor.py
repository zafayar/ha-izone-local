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
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import from_centi
from .const import SENS_TYPE_NONE
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity, IZoneZoneEntity


@dataclass(frozen=True, kw_only=True)
class IZoneSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    needs_sensor: bool = False  # skip on zones with no temperature sensor


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
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda d: d.get("DmpPos"),
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
    for index, zone in coordinator.data.zones.items():
        has_sensor = zone.get("SensType") != SENS_TYPE_NONE
        entities.extend(
            IZoneZoneSensor(coordinator, index, desc)
            for desc in ZONE_SENSORS
            if has_sensor or not desc.needs_sensor
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
        return self.entity_description.value_fn(self.zone)
