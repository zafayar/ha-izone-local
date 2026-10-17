"""The iZone Local integration (iZone LAN API v2)."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import IZoneClient, async_listen_for_changes
from .const import CONF_UID
from .coordinator import IZoneConfigEntry, IZoneCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SCENE,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: IZoneConfigEntry) -> bool:
    client = IZoneClient(async_get_clientsession(hass), entry.data[CONF_HOST])
    coordinator = IZoneCoordinator(hass, entry, client, entry.data[CONF_UID])
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Changes made at the wall controller or in the app show within seconds
    # if the bridge's broadcasts reach Home Assistant; polling covers the rest.
    def changed(message: str) -> None:
        _LOGGER.debug("Bridge broadcast %s", message)
        coordinator.notify_changed()

    bridge_ip = entry.data[CONF_HOST].split(":")[0]
    if transport := await async_listen_for_changes(bridge_ip, changed):
        entry.async_on_unload(transport.close)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IZoneConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
