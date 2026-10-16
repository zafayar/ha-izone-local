"""Base entity classes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import IZoneError
from .const import DOMAIN, PENDING_ICON, brand_name, system_name, unit_model
from .coordinator import IZoneCoordinator, PendingKey

# One command to send, as a zero-argument callable so it runs in order.
type Step = Callable[[], Awaitable[None]]


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
        """The system datagram, with changes still waiting on the bridge."""
        return self.coordinator.system_view()

    @property
    def pending(self) -> bool:
        """Whether this entity shows a change the bridge hasn't confirmed."""
        return False

    @property
    def icon(self) -> str | None:
        return PENDING_ICON if self.pending else super().icon

    async def _apply(self, changes: dict[PendingKey, Any], *steps: Step) -> None:
        """Show `changes` at once, send `steps` in order, then re-read the bridge.

        The bridge needs commands well apart, so a few steps take seconds;
        showing the result up front keeps the UI responsive meanwhile. One
        direct refresh at the end (not a debounced one per command), plus a
        few quick follow-ups if needed, confirms the change soon after.
        """
        if not changes and not steps:
            return
        coordinator = self.coordinator
        coordinator.expect(changes)
        try:
            for step in steps:
                await step()
        except IZoneError as err:
            coordinator.forget(changes)
            raise HomeAssistantError(f"iZone command failed: {err}") from err
        finally:
            await coordinator.async_refresh()
            # The bridge can take a second or two to report what it did.
            coordinator.confirm_soon()


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
        """This zone's datagram, with changes still waiting on the bridge."""
        return self.coordinator.zone_view(self.index)

    @property
    def available(self) -> bool:
        return super().available and self.index in self.coordinator.data.zones
