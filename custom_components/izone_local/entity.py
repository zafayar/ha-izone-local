"""Base entity classes."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import IZoneError
from .const import DOMAIN, brand_name, system_name, unit_model
from .coordinator import IZoneCoordinator


class IZoneEntity(CoordinatorEntity[IZoneCoordinator]):
    """An entity attached to the bridge's AC system device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: IZoneCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.uid}_{key}"
        system = coordinator.data.system
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.uid)},
            manufacturer=brand_name(system),
            model=unit_model(system),
            name=system_name(system),
        )

    @property
    def system(self) -> dict[str, Any]:
        return self.coordinator.data.system

    async def _run(self, command: Awaitable[None]) -> None:
        """Send a command, then poll so the UI reflects the new state."""
        try:
            await command
        except IZoneError as err:
            raise HomeAssistantError(f"iZone command failed: {err}") from err
        await self.coordinator.async_request_refresh()


class IZoneZoneEntity(IZoneEntity):
    """An entity attached to one zone's own device."""

    def __init__(self, coordinator: IZoneCoordinator, index: int, key: str) -> None:
        super().__init__(coordinator, f"zone{index}_{key}")
        self.index = index
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.uid}_zone{index}")},
            manufacturer=brand_name(coordinator.data.system),
            model=f"{brand_name(coordinator.data.system)} zone",
            name=self.zone.get("Name") or f"Zone {index + 1}",
            via_device=(DOMAIN, coordinator.uid),
        )

    @property
    def zone(self) -> dict[str, Any]:
        return self.coordinator.data.zones.get(self.index, {})

    @property
    def available(self) -> bool:
        return super().available and self.index in self.coordinator.data.zones
