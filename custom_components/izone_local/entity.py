"""Base entity classes."""

from __future__ import annotations

from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import IZoneError
from .const import (
    DOMAIN,
    STATUS_FAILED,
    STATUS_ICONS,
    STATUS_READY,
    brand_name,
    system_name,
    unit_model,
    zone_name,
)
from .coordinator import IZoneCoordinator, PendingKey


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

    def _matches(self, key: PendingKey) -> bool:
        """Whether a change to this field shows on this entity."""
        return False

    @property
    def status(self) -> str:
        """Progress of changes to this entity: ready, queued, sending (1 of 2),
        confirming or failed."""
        return self.coordinator.status(self._matches)

    @property
    def pending(self) -> bool:
        """Whether this entity shows a change the bridge hasn't confirmed."""
        return self.status not in (STATUS_READY, STATUS_FAILED)

    @property
    def icon(self) -> str | None:
        status = self.status.split(" ")[0]  # "sending (1 of 2)" -> "sending"
        return STATUS_ICONS.get(status) or super().icon

    async def _apply(self, changes: dict[PendingKey, Any], **options: Any) -> None:
        """Ask for these values. They show at once; this returns once the
        bridge shows them too, or raises if it couldn't apply them."""
        try:
            await self.coordinator.async_request(changes, **options)
        except IZoneError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err


class IZoneZoneEntity(IZoneEntity):
    """An entity attached to one zone's own device."""

    def __init__(self, coordinator: IZoneCoordinator, index: int, key: str) -> None:
        super().__init__(coordinator, f"zone{index}_{key}")
        self.index = index
        name = zone_name(self.zone, index)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.uid}_zone{index}")},
            manufacturer=brand_name(coordinator.data.system),
            model=f"{brand_name(coordinator.data.system)} zone",
            name=name,
            suggested_area=name,  # a zone is usually a room
            # Linked to the AC device after setup (see __init__.py).
        )

    @property
    def zone(self) -> dict[str, Any]:
        """This zone's datagram, with changes still waiting on the bridge."""
        return self.coordinator.zone_view(self.index)

    @property
    def available(self) -> bool:
        return super().available and self.index in self.coordinator.data.zones
