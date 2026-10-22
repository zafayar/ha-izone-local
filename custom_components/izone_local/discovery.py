"""Finds iZone bridges on the network, and offers them for setup.

Bridges answer a UDP broadcast (see api.async_discover). A scan runs when
Home Assistant has started and then every DISCOVERY_INTERVAL. Each v2 bridge
found starts a discovery flow: a new one shows up under Discovered, and a
known one whose address has changed gets the new address (the config flow
updates its entry).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import discovery_flow
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started

from .api import async_discover
from .const import CONF_UID, DOMAIN

_LOGGER = logging.getLogger(__name__)

DISCOVERY_INTERVAL = timedelta(minutes=30)


async def async_scan(hass: HomeAssistant, _now: datetime | None = None) -> None:
    """Broadcast once, and start a discovery flow for each v2 bridge."""
    try:
        bridges = await async_discover()
    except OSError as err:
        _LOGGER.debug("Discovery broadcast failed: %s", err)
        return
    for bridge in bridges:
        if bridge.supports_v2:
            discovery_flow.async_create_flow(
                hass,
                DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY},
                data={CONF_HOST: bridge.host, CONF_UID: bridge.uid},
            )


def async_start_discovery(hass: HomeAssistant) -> None:
    """Scan once Home Assistant has started, then every DISCOVERY_INTERVAL."""

    async def scan(now: datetime) -> None:
        await async_scan(hass, now)

    async def started(hass: HomeAssistant) -> None:
        await async_scan(hass)

    async_at_started(hass, started)
    async_track_time_interval(
        hass, scan, DISCOVERY_INTERVAL, name=f"{DOMAIN} discovery", cancel_on_shutdown=True
    )
