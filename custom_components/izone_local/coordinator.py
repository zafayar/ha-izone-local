"""Polling coordinator for one iZone bridge."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import IZoneClient, IZoneError
from .const import DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

type IZoneConfigEntry = ConfigEntry[IZoneCoordinator]


@dataclass
class IZoneData:
    system: dict[str, Any]
    zones: dict[int, dict[str, Any]] = field(default_factory=dict)


class IZoneCoordinator(DataUpdateCoordinator[IZoneData]):
    """Fetches the system datagram, then each zone, on every poll."""

    config_entry: IZoneConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: IZoneConfigEntry,
        client: IZoneClient,
        uid: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {uid}",
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self.uid = uid

    async def _async_update_data(self) -> IZoneData:
        try:
            _, system = await self.client.get_system()
            zones: dict[int, dict[str, Any]] = {}
            for index in range(int(system.get("NoOfZones", 0))):
                zones[index] = await self.client.get_zone(index)
        except IZoneError as err:
            raise UpdateFailed(str(err)) from err
        return IZoneData(system=system, zones=zones)
