"""The integration's actions: izone_local.update_favourite."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import service

from . import const as c

SERVICE_UPDATE_FAVOURITE = "update_favourite"

MODE_NAMES = {1: "cool", 2: "heat", 3: "vent", 4: "dry", 5: "auto"}
FAN_NAMES = {1: "low", 2: "medium", 3: "high", 4: "auto", 5: "top"}
ZONE_MODE_NAMES = {1: "open", 2: "close", 3: "auto"}

# "unchanged" (0) is how a favourite leaves the AC's mode or fan alone, as
# favourites made in the app usually do.
UNCHANGED = "unchanged"
ZONE_MODE_CHOICES = {name: mode for mode, name in ZONE_MODE_NAMES.items()}
FAN_CHOICES = {UNCHANGED: 0} | {name: fan for fan, name in FAN_NAMES.items()}
MODE_CHOICES = {UNCHANGED: 0} | {name: mode for mode, name in MODE_NAMES.items()}

UPDATE_FAVOURITE_SCHEMA = {
    vol.Optional("zone"): vol.Coerce(str),
    vol.Optional("zone_mode"): vol.In(list(ZONE_MODE_CHOICES)),
    vol.Optional("temperature"): vol.All(
        vol.Coerce(float), vol.Range(min=c.MIN_TEMP, max=c.MAX_TEMP)
    ),
    vol.Optional("fan"): vol.In(list(FAN_CHOICES)),
    vol.Optional("mode"): vol.In(list(MODE_CHOICES)),
}

# Registering an entity action when the integration loads (rather than with
# each config entry) needs Home Assistant 2025.10. Before that, the scene
# platform registers it as it sets up.
REGISTER_ON_LOAD = hasattr(service, "async_register_platform_entity_service")


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions, once, when the integration loads."""
    if REGISTER_ON_LOAD:
        service.async_register_platform_entity_service(
            hass,
            c.DOMAIN,
            SERVICE_UPDATE_FAVOURITE,
            entity_domain=Platform.SCENE,
            schema=UPDATE_FAVOURITE_SCHEMA,
            func="async_update_favourite",
        )
