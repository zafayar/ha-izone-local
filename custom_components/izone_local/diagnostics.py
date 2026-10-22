"""Diagnostics download (Settings > Devices > the AC > Download diagnostics)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .api import FAVOURITE_COUNT, IZoneError
from .const import CONF_UID
from .coordinator import IZoneConfigEntry

# The bridge's password and lock code, and what identifies the installation.
TO_REDACT = {"Pass", "LockCode", "AirStreamDeviceUId", CONF_HOST, CONF_UID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IZoneConfigEntry
) -> dict[str, Any]:
    """The raw bridge state, favourites and pending changes, for issue reports."""
    coordinator = entry.runtime_data
    favourites: list[dict[str, Any]] = []
    for index in range(FAVOURITE_COUNT):
        try:
            favourites.append(await coordinator.client.get_favourite(index))
        except IZoneError as err:
            favourites.append({"favourite": index + 1, "error": str(err)})
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "system": async_redact_data(coordinator.data.system, TO_REDACT),
        "zones": coordinator.data.zones,
        "favourites": favourites,
        "firmware": coordinator.firmware,  # without the hub's serial number
        "last_update_success": coordinator.last_update_success,
        "recent_failures": coordinator.recent_failures,
        "pending_changes": coordinator.pending_for_diagnostics(),
    }
