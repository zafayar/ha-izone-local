"""Scenes for the favourites saved on the controller (from the iZone app)."""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import const as c
from .api import FAVOURITE_COUNT, IZoneError, clean_name, from_centi
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity

_LOGGER = logging.getLogger(__name__)

# The controller's names for slots nobody has set up.
DEFAULT_NAME = re.compile(r"Favourite \d")

MODE_NAMES = {1: "cool", 2: "heat", 3: "vent", 4: "dry", 5: "auto"}
FAN_NAMES = {1: "low", 2: "medium", 3: "high", 4: "auto", 5: "top"}
ZONE_MODE_NAMES = {1: "open", 2: "close", 3: "auto"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One scene per named favourite. Favourites are read once here; reload
    the integration after renaming or adding one in the iZone app."""
    coordinator = entry.runtime_data
    scenes = []
    for index in range(FAVOURITE_COUNT):
        try:
            favourite = await coordinator.client.get_favourite(index)
        except IZoneError as err:
            _LOGGER.debug("Can't read favourite %d: %s", index + 1, err)
            continue
        if clean_name(favourite.get("Name")):
            scenes.append(IZoneFavouriteScene(coordinator, index, favourite))
    async_add_entities(scenes)


class IZoneFavouriteScene(IZoneEntity, Scene):
    """A saved favourite: AC mode, fan and per-zone settings in one go.

    Activating it sends one FavouriteSet command, then follows through: any
    part the controller hasn't applied after a few seconds is sent zone by
    zone, as for any other change.
    """

    def __init__(
        self, coordinator: IZoneCoordinator, index: int, favourite: dict[str, Any]
    ) -> None:
        super().__init__(coordinator, f"favourite{index}")
        self.index = index
        self._favourite = favourite
        self._attr_name = clean_name(favourite.get("Name"))
        # Unused slots keep a default name; add them, but switched off.
        self._attr_entity_registry_enabled_default = not DEFAULT_NAME.fullmatch(
            self._attr_name
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        favourite = self._favourite
        targets = favourite.get("Zones") or []
        zones = {}
        for index, zone in self.coordinator.data.zones.items():
            if index >= len(targets) or zone.get("Bypass"):
                continue
            mode = targets[index].get("Mode")
            name = clean_name(zone.get("Name")) or f"Zone {index + 1}"
            if mode == c.ZONE_MODE_AUTO:
                zones[name] = f"auto {from_centi(targets[index].get('Setpoint'))}"
            else:
                zones[name] = ZONE_MODE_NAMES.get(mode, "unchanged")
        return {
            "favourite_number": self.index + 1,
            "mode": MODE_NAMES.get(favourite.get("Mode"), "unchanged"),
            "fan": FAN_NAMES.get(favourite.get("Fan"), "unchanged"),
            "zones": zones,
        }

    async def async_activate(self, **kwargs: Any) -> None:
        client = self.coordinator.client
        try:
            # Read it fresh, in case it was changed in the iZone app.
            self._favourite = await client.get_favourite(self.index)
        except IZoneError as err:
            raise HomeAssistantError(f"Can't read the favourite: {err}") from err
        changes, whole = self.coordinator.favourite_changes(self._favourite)
        if not whole:
            _LOGGER.info(
                "%s needs a zone whose sensor is out, which the controller "
                "refuses; applying it zone by zone",
                self._attr_name,
            )
        prelude = (lambda: client.run_favourite(self.index)) if whole else None
        # The controller can take a minute to finish a favourite, so don't
        # hold the caller: return once it's accepted, and follow through in
        # the background (the entities show the progress).
        await self._apply(changes, prelude=prelude, wait=False)
