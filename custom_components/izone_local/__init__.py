"""The iZone Local integration (iZone LAN API v2)."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import IZoneClient, async_listen_for_changes
from .const import CONF_UID, DOMAIN, brand_name, system_name, unit_model
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .discovery import async_start_discovery
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SCENE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    async_start_discovery(hass)
    return True


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

    registry = dr.async_get(hass)
    system = coordinator.data.system
    ac = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, coordinator.uid)},
        manufacturer=brand_name(system),
        model=unit_model(system),
        name=system_name(system),
        sw_version=coordinator.hub_firmware,
    )
    coordinator.ac_device_id = ac.id  # to show a firmware update
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _link_zone_devices(registry, coordinator, ac.id)
    _remove_stale_devices(registry, entry, coordinator)
    options = dict(entry.options)

    async def options_updated(hass: HomeAssistant, entry: IZoneConfigEntry) -> None:
        """Apply changed options. Reloading starts the notifications afresh,
        so a filter warning that's still on is shown again once they're back
        on. (Other updates, like a new address, reload the entry themselves.)"""
        if entry.options == options:
            return
        if not entry.runtime_data.notifications_enabled:
            entry.runtime_data.dismiss_notifications()
        await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(options_updated))
    return True


def _current_identifiers(coordinator: IZoneCoordinator) -> set[tuple[str, str]]:
    """The devices the bridge reports now: the AC and each zone."""
    return {(DOMAIN, coordinator.uid)} | {
        (DOMAIN, f"{coordinator.uid}_zone{index}") for index in coordinator.data.zones
    }


def _remove_stale_devices(
    registry: dr.DeviceRegistry, entry: IZoneConfigEntry, coordinator: IZoneCoordinator
) -> None:
    """Remove devices the bridge no longer reports, e.g. a zone taken out."""
    current = _current_identifiers(coordinator)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not device.identifiers & current:
            _LOGGER.info("Removing %s: the bridge no longer reports it", device.name)
            registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: IZoneConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Let the user delete a device, but only one the bridge no longer reports."""
    return not device.identifiers & _current_identifiers(entry.runtime_data)


def _link_zone_devices(
    registry: dr.DeviceRegistry, coordinator: IZoneCoordinator, ac_id: str
) -> None:
    """Show each zone's device as connected through the AC's.

    Done by registry device ID once the platforms have created the zone
    devices: a `via_device` identifier in DeviceInfo is deprecated, and
    `via_device_id` there isn't accepted by older Home Assistant versions.
    """
    for index in coordinator.data.zones:
        zone = registry.async_get_device(
            identifiers={(DOMAIN, f"{coordinator.uid}_zone{index}")}
        )
        if zone is not None and zone.via_device_id != ac_id:
            registry.async_update_device(zone.id, via_device_id=ac_id)


async def async_unload_entry(hass: HomeAssistant, entry: IZoneConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
