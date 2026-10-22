"""iSave switch (only created when the system has iSave enabled)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import IZoneConfigEntry, IZoneCoordinator, PendingKey
from .entity import IZoneEntity

# Each platform's updates are read by the coordinator, and commands are
# queued by the client, so entities needn't be limited here.
PARALLEL_UPDATES = 0


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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"status": self.status, "pending": self.pending}

    def _matches(self, key: PendingKey) -> bool:
        return key == (None, "iSaveOn")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, on: bool) -> None:
        if self.is_on != on:
            await self._apply({(None, "iSaveOn"): int(on)})
