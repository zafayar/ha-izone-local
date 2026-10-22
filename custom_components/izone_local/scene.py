"""Scenes for the favourites saved on the controller (from the iZone app)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import const as c
from .api import (
    FAVOURITE_COUNT,
    FAVOURITE_ZONE_SLOTS,
    IZoneError,
    clean_name,
    from_centi,
    to_centi,
)
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity
from .services import (
    FAN_CHOICES,
    FAN_NAMES,
    MODE_CHOICES,
    MODE_NAMES,
    REGISTER_ON_LOAD,
    SERVICE_UPDATE_FAVOURITE,
    UNCHANGED,
    UPDATE_FAVOURITE_SCHEMA,
    ZONE_MODE_CHOICES,
    ZONE_MODE_NAMES,
)

# Each platform's updates are read by the coordinator, and commands are
# queued by the client, so entities needn't be limited here.
PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

# The controller's names for slots nobody has set up.
DEFAULT_NAME = re.compile(r"Favourite \d")

# One edit: what it changes (for messages), how to send it, and whether a
# favourite as read back shows it.
type _Edit = tuple[str, Callable[[], Awaitable[None]], Callable[[dict], bool]]


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

    if not REGISTER_ON_LOAD:  # Home Assistant before 2025.10
        entity_platform.async_get_current_platform().async_register_entity_service(
            SERVICE_UPDATE_FAVOURITE, UPDATE_FAVOURITE_SCHEMA, "async_update_favourite"
        )


def _stored_zone(favourite: dict[str, Any], index: int) -> dict[str, Any]:
    zones = favourite.get("Zones") or []
    zone = zones[index] if index < len(zones) else {}
    return {"Mode": zone.get("Mode"), "Setpoint": zone.get("Setpoint")}


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
                zones[name] = ZONE_MODE_NAMES.get(mode, UNCHANGED)
        return {
            "favourite_number": self.index + 1,
            "mode": MODE_NAMES.get(favourite.get("Mode"), UNCHANGED),
            "fan": FAN_NAMES.get(favourite.get("Fan"), UNCHANGED),
            "zones": zones,
        }

    async def async_activate(self, **kwargs: Any) -> None:
        client = self.coordinator.client
        try:
            # Read it fresh, in case it was changed in the iZone app.
            self._favourite = await client.get_favourite(self.index)
        except IZoneError as err:
            raise HomeAssistantError(
                translation_domain=c.DOMAIN,
                translation_key="favourite_read_failed",
                translation_placeholders={"error": str(err)},
            ) from err
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

    # Editing (izone_local.update_favourite) -------------------------------

    async def async_update_favourite(
        self,
        zone: str | None = None,
        zone_mode: str | None = None,
        temperature: float | None = None,
        fan: str | None = None,
        mode: str | None = None,
    ) -> None:
        """Change what this favourite stores: one zone's mode and setpoint,
        the fan and/or the AC mode.

        It's saved on the controller, so the app and wall controller see it
        too; nothing is applied until the favourite is next run. The replies
        to these commands aren't reliable, so each edit is judged by reading
        the favourite back, and what didn't stick is sent again.
        """
        if zone is None and (zone_mode is not None or temperature is not None):
            raise ServiceValidationError(translation_domain=c.DOMAIN, translation_key="zone_settings_need_zone")
        if zone is not None and zone_mode is None and temperature is None:
            raise ServiceValidationError(
                translation_domain=c.DOMAIN,
                translation_key="zone_needs_setting",
                translation_placeholders={"zone": zone},
            )
        if zone is None and fan is None and mode is None:
            raise ServiceValidationError(translation_domain=c.DOMAIN, translation_key="nothing_to_change")

        client = self.coordinator.client
        try:
            current = await client.get_favourite(self.index)
        except IZoneError as err:
            raise HomeAssistantError(
                translation_domain=c.DOMAIN,
                translation_key="favourite_read_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        edits: list[_Edit] = []
        if zone is not None:
            index, zones = self._zone_edit(current, zone, zone_mode, temperature)
            edits.append(
                (
                    clean_name(self.coordinator.data.zones[index].get("Name")) or zone,
                    lambda: client.set_favourite_zones(self.index, zones),
                    lambda f: _stored_zone(f, index) == zones[index],
                )
            )
        if fan is not None:
            fan_value = FAN_CHOICES[fan]
            speeds = c.fan_speeds(self.coordinator.data.system)
            if fan_value and fan_value not in speeds:
                raise ServiceValidationError(
                    translation_domain=c.DOMAIN,
                    translation_key="fan_speeds",
                    translation_placeholders={
                        "speeds": ", ".join(FAN_NAMES[speed] for speed in speeds)
                    },
                )
            edits.append(
                (
                    "fan",
                    lambda: client.set_favourite_fan(self.index, fan_value),
                    lambda f: f.get("Fan") == fan_value,
                )
            )
        if mode is not None:
            mode_value = MODE_CHOICES[mode]
            edits.append(
                (
                    "mode",
                    lambda: client.set_favourite_mode(self.index, mode_value),
                    lambda f: f.get("Mode") == mode_value,
                )
            )
        await self._write(current, edits)

    def _zone_edit(
        self,
        favourite: dict[str, Any],
        zone: str,
        zone_mode: str | None,
        temperature: float | None,
    ) -> tuple[int, list[dict[str, int]]]:
        """The zone's index, and all 14 zones with that one changed: the
        command replaces every zone, so the others are sent as stored."""
        index = self._zone_index(zone)
        data = self.coordinator.data.zones[index]
        name = clean_name(data.get("Name")) or zone
        if data.get("Bypass") or data.get("ZoneType") == c.ZONE_TYPE_CONSTANT:
            raise ServiceValidationError(
                translation_domain=c.DOMAIN,
                translation_key="zone_run_by_system",
                translation_placeholders={"zone": name},
            )
        zones = []
        for slot in range(FAVOURITE_ZONE_SLOTS):
            stored = _stored_zone(favourite, slot)
            zones.append(
                {
                    "Mode": stored["Mode"] or c.ZONE_MODE_CLOSE,
                    "Setpoint": stored["Setpoint"] or to_centi(23),
                }
            )
        if zone_mode is not None:
            new_mode = ZONE_MODE_CHOICES[zone_mode]
        elif temperature is not None:
            new_mode = c.ZONE_MODE_AUTO  # as on a zone: a temperature means auto
        else:
            new_mode = zones[index]["Mode"]
        if new_mode == c.ZONE_MODE_AUTO and data.get("ZoneType") != c.ZONE_TYPE_AUTO:
            raise ServiceValidationError(
                translation_domain=c.DOMAIN,
                translation_key="zone_open_close_only",
                translation_placeholders={"zone": name},
            )
        zones[index] = {
            "Mode": new_mode,
            "Setpoint": (
                zones[index]["Setpoint"] if temperature is None else to_centi(temperature)
            ),
        }
        return index, zones

    def _zone_index(self, zone: str) -> int:
        """A zone's index, from its name (any case) or its number (1 = first)."""
        zones = self.coordinator.data.zones
        wanted = zone.strip()
        for index, data in zones.items():
            if clean_name(data.get("Name")).casefold() == wanted.casefold():
                return index
        if wanted.isdigit() and int(wanted) - 1 in zones:
            return int(wanted) - 1
        names = ", ".join(c.zone_name(data, i) for i, data in zones.items())
        raise ServiceValidationError(
            translation_domain=c.DOMAIN,
            translation_key="unknown_zone",
            translation_placeholders={"zone": zone, "zones": names},
        )

    async def _write(self, current: dict[str, Any], edits: list[_Edit]) -> None:
        client = self.coordinator.client
        pending = [edit for edit in edits if not edit[2](current)]
        last_error: IZoneError | None = None
        for _ in range(c.FAVOURITE_EDIT_ATTEMPTS):
            if not pending:
                break
            for _what, send, _kept in pending:
                try:
                    await send()
                except IZoneError as err:
                    _LOGGER.debug("%s: %s (checking the read-back)", self._attr_name, err)
                    last_error = err
                await asyncio.sleep(c.FAVOURITE_EDIT_GAP)
            try:
                current = await client.get_favourite(self.index)
            except IZoneError as err:
                last_error = err
                continue
            self._favourite = current
            pending = [edit for edit in pending if not edit[2](current)]
        self.async_write_ha_state()
        if pending:
            raise HomeAssistantError(
                translation_domain=c.DOMAIN,
                translation_key="favourite_not_kept",
                translation_placeholders={
                    "favourite": self._attr_name or "",
                    "changes": ", ".join(what for what, _send, _kept in pending),
                    "error": str(last_error) if last_error else "none",
                },
            )
        _LOGGER.info("Favourite %s updated: %s", self._attr_name, [e[0] for e in edits])
