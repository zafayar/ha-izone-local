"""The iZone Local integration (iZone LAN API v2)."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import IZoneClient
from .const import CONF_UID
from .coordinator import IZoneConfigEntry, IZoneCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: IZoneConfigEntry) -> bool:
    client = IZoneClient(async_get_clientsession(hass), entry.data[CONF_HOST])
    coordinator = IZoneCoordinator(hass, entry, client, entry.data[CONF_UID])
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IZoneConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
