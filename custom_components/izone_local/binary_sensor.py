"""Problem sensors: filter, AC fault, zone sensor and damper faults."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SENS_TYPE_NONE
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity, IZoneZoneEntity


@dataclass(frozen=True, kw_only=True)
class IZoneBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], bool | None]
    needs_sensor: bool = False  # skip on zones with no temperature sensor


SYSTEM_BINARY = (
    IZoneBinaryDescription(
        key="filter_warning",
        translation_key="filter_warning",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: str(d.get("Warnings", "")).lower() == "filter",
    ),
    IZoneBinaryDescription(
        key="ac_error",
        translation_key="ac_error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        # " OK" (note the leading space) means no error.
        value_fn=lambda d: (d.get("ACError") or "OK").strip().upper() != "OK",
    ),
)

ZONE_BINARY = (
    IZoneBinaryDescription(
        key="sensor_fault",
        translation_key="sensor_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: bool(d.get("SensorFault")),
        needs_sensor=True,
    ),
    IZoneBinaryDescription(
        key="damper_fault",
        translation_key="damper_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: bool(d.get("DmpFlt")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        IZoneSystemBinary(coordinator, desc) for desc in SYSTEM_BINARY
    ]
    for index, zone in coordinator.data.zones.items():
        has_sensor = zone.get("SensType") != SENS_TYPE_NONE
        entities.extend(
            IZoneZoneBinary(coordinator, index, desc)
            for desc in ZONE_BINARY
            if has_sensor or not desc.needs_sensor
        )
    async_add_entities(entities)


class IZoneSystemBinary(IZoneEntity, BinarySensorEntity):
    entity_description: IZoneBinaryDescription

    def __init__(
        self, coordinator: IZoneCoordinator, description: IZoneBinaryDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.system)


class IZoneZoneBinary(IZoneZoneEntity, BinarySensorEntity):
    entity_description: IZoneBinaryDescription

    def __init__(
        self,
        coordinator: IZoneCoordinator,
        index: int,
        description: IZoneBinaryDescription,
    ) -> None:
        super().__init__(coordinator, index, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.zone)
