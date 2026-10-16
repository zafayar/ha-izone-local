"""Polling coordinator for one iZone bridge."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import IZoneClient, IZoneError
from .const import DOMAIN, PENDING_TIMEOUT, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

type IZoneConfigEntry = ConfigEntry[IZoneCoordinator]

# A field waiting for the bridge to confirm a change: (zone index, field name),
# with None as the index for the system datagram. E.g. (2, "Mode"), (None, "SysOn").
type PendingKey = tuple[int | None, str]

# The bridge reports a change 0-2 s after accepting it (measured: zone open
# 0.5 s, power on 1 s, zone close 1.9 s). While a change is unconfirmed,
# re-read after these delays instead of waiting for the next regular poll.
CONFIRM_DELAYS = (1, 1, 1, 2, 3)


@dataclass
class IZoneData:
    system: dict[str, Any]
    zones: dict[int, dict[str, Any]] = field(default_factory=dict)


@dataclass
class _Pending:
    value: Any
    deadline: float


class IZoneCoordinator(DataUpdateCoordinator[IZoneData]):
    """Fetches the system datagram, then each zone, on every poll.

    It also holds the changes the user asked for that the bridge hasn't
    confirmed yet. Entities show those values straight away (and an hourglass
    icon) until a poll reads them back from the bridge, a command fails, or
    PENDING_TIMEOUT passes, after which the bridge's value wins.
    """

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
        self._pending: dict[PendingKey, _Pending] = {}
        self._confirm_task: asyncio.Task | None = None

    async def _async_update_data(self) -> IZoneData:
        try:
            _, system = await self.client.get_system()
            zones: dict[int, dict[str, Any]] = {}
            for index in range(int(system.get("NoOfZones", 0))):
                zones[index] = await self.client.get_zone(index)
        except IZoneError as err:
            raise UpdateFailed(str(err)) from err
        data = IZoneData(system=system, zones=zones)
        self._settle_pending(data)
        return data

    # Pending changes -----------------------------------------------------

    def expect(self, changes: dict[PendingKey, Any]) -> None:
        """Show these values now, until the bridge confirms them."""
        deadline = time.monotonic() + PENDING_TIMEOUT
        for key, value in changes.items():
            self._pending[key] = _Pending(value, deadline)
        self.async_update_listeners()

    def confirm_soon(self) -> None:
        """Re-read the bridge shortly, in the background, until it confirms."""
        running = self._confirm_task is not None and not self._confirm_task.done()
        if self._pending and not running:
            self._confirm_task = self.config_entry.async_create_background_task(
                self.hass, self._confirm(), f"{DOMAIN} confirm {self.uid}"
            )

    async def _confirm(self) -> None:
        for delay in CONFIRM_DELAYS:
            await asyncio.sleep(delay)
            if not self._pending:
                return
            await self.async_refresh()

    def forget(self, keys: Any) -> None:
        """Drop pending values (e.g. after a command failed)."""
        for key in list(keys):
            self._pending.pop(key, None)
        self.async_update_listeners()

    def is_pending(self, index: int | None, fields: tuple[str, ...] = ()) -> bool:
        """Whether a system (None) or zone value is waiting for confirmation."""
        return any(
            i == index and (not fields or f in fields) for i, f in self._pending
        )

    def system_view(self) -> dict[str, Any]:
        """The system datagram with pending values applied."""
        return self._view(None, self.data.system)

    def zone_view(self, index: int) -> dict[str, Any]:
        """One zone's datagram with pending values applied."""
        return self._view(index, self.data.zones.get(index, {}))

    def _view(self, index: int | None, raw: dict[str, Any]) -> dict[str, Any]:
        overlay = {f: p.value for (i, f), p in self._pending.items() if i == index}
        return {**raw, **overlay} if overlay else raw

    def _settle_pending(self, data: IZoneData) -> None:
        now = time.monotonic()
        for (index, name), pending in list(self._pending.items()):
            raw = data.system if index is None else data.zones.get(index, {})
            if raw.get(name) == pending.value:
                del self._pending[(index, name)]
            elif now > pending.deadline:
                _LOGGER.debug(
                    "Bridge never confirmed %s=%r on %s; showing its value %r",
                    name, pending.value, "system" if index is None else f"zone {index}",
                    raw.get(name),
                )
                del self._pending[(index, name)]
