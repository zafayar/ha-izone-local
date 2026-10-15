"""iSave switch (only created when the system has iSave enabled)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator.data.system.get("iSaveEnable"):
        async_add_entities([IZoneISaveSwitch(coordinator)])


class IZoneISaveSwitch(IZoneEntity, SwitchEntity):
    _attr_translation_key = "isave"

    def __init__(self, coordinator: IZoneCoordinator) -> None:
        super().__init__(coordinator, "isave")

    @property
    def is_on(self) -> bool:
        return bool(self.system.get("iSaveOn"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._run(self.coordinator.client.set_isave(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._run(self.coordinator.client.set_isave(False))
