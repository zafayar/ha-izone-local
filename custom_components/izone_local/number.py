"""Settings: each zone's minimum and maximum airflow.

There's no sleep-timer setting: on a real controller, setting it turns the AC
on, and setting it back to 0 doesn't cancel it (only turning the AC off does).
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import IZoneConfigEntry, IZoneCoordinator, PendingKey
from .entity import IZoneZoneEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = []
    for index, zone in coordinator.data.zones.items():
        if not zone.get("Bypass"):
            entities += [
                IZoneAirflow(coordinator, index, "MinAir"),
                IZoneAirflow(coordinator, index, "MaxAir"),
            ]
    async_add_entities(entities)


class IZoneAirflow(IZoneZoneEntity, NumberEntity):
    """A zone's minimum or maximum damper opening (a setting; off by default)."""

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: IZoneCoordinator, index: int, field: str) -> None:
        key = "min_airflow" if field == "MinAir" else "max_airflow"
        super().__init__(coordinator, index, key)
        self._field = field
        self._attr_translation_key = key

    @property
    def native_value(self) -> int | None:
        return self.zone.get(self._field)

    def _matches(self, key: PendingKey) -> bool:
        return key == (self.index, self._field)

    async def async_set_native_value(self, value: float) -> None:
        value = int(value)
        low = value if self._field == "MinAir" else self.zone.get("MinAir", 0)
        high = value if self._field == "MaxAir" else self.zone.get("MaxAir", 100)
        if low > high:
            raise ServiceValidationError(
                f"Minimum airflow ({low}%) can't be above maximum ({high}%)"
            )
        if value != self.native_value:
            await self._apply({(self.index, self._field): value})
