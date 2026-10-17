"""Polling coordinator for one iZone bridge, and the loop that applies changes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import const as c
from .api import IZoneClient, IZoneCommandError, IZoneError, from_centi

_LOGGER = logging.getLogger(__name__)

type IZoneConfigEntry = ConfigEntry[IZoneCoordinator]

# One field of the desired state: (zone index, field name), with None as the
# index for the system datagram. E.g. (2, "Mode"), (None, "SysOn").
type PendingKey = tuple[int | None, str]


@dataclass
class IZoneData:
    system: dict[str, Any]
    zones: dict[int, dict[str, Any]] = field(default_factory=dict)


@dataclass
class _Want:
    """A value the user asked for that the bridge doesn't show yet."""

    value: Any
    deadline: float
    sent_at: float | None = None
    attempts: int = 0
    # Turning the AC off because the last zone was closed: only goes ahead if,
    # on fresh data, no zone is on.
    unless_zones_on: bool = False


@dataclass
class _Request:
    """One user action, waiting until all of its fields are settled."""

    keys: frozenset[PendingKey]
    done: asyncio.Future[None]
    errors: list[str] = field(default_factory=list)


class IZoneCoordinator(DataUpdateCoordinator[IZoneData]):
    """Polls the bridge, and reconciles it with the state the user asked for.

    An action records its desired values here. Entities show them at once,
    with a status ("sending (1 of 2)", "confirming"...), while one background
    loop repeats: sync the affected datagrams, drop what the bridge now shows,
    and send the next missing command in a safe order. A command the bridge
    accepts but doesn't apply is re-sent; after MAX_ATTEMPTS, or
    PENDING_TIMEOUT, the change fails and the bridge's value is shown again.
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
            name=f"{c.DOMAIN} {uid}",
            update_interval=c.SCAN_INTERVAL,
        )
        self.client = client
        self.uid = uid
        self._wants: dict[PendingKey, _Want] = {}
        self._requests: list[_Request] = []
        self._failed: dict[PendingKey, float] = {}  # key -> show "failed" until
        self._loop_task: asyncio.Task | None = None

    async def _async_update_data(self) -> IZoneData:
        try:
            _, system = await self.client.get_system()
            zones: dict[int, dict[str, Any]] = {}
            for index in range(int(system.get("NoOfZones", 0))):
                zones[index] = await self.client.get_zone(index)
        except IZoneError as err:
            raise UpdateFailed(str(err)) from err
        data = IZoneData(system=system, zones=zones)
        self._settle(data)
        return data

    # Desired state -------------------------------------------------------

    async def async_request(
        self, changes: dict[PendingKey, Any], *, ac_off_unless_zones_on: bool = False
    ) -> None:
        """Show `changes` now, then wait until the bridge shows them too.

        With `ac_off_unless_zones_on`, a request to turn the AC off is dropped
        (without error) if a zone turns out to be on when the bridge is read.
        Raises IZoneCommandError if any change could not be applied.
        """
        if not changes:
            return
        deadline = time.monotonic() + c.PENDING_TIMEOUT
        for key, value in changes.items():
            self._wants[key] = _Want(
                value, deadline, unless_zones_on=ac_off_unless_zones_on
            )
            self._failed.pop(key, None)
        request = _Request(
            frozenset(changes), asyncio.get_running_loop().create_future()
        )
        self._requests.append(request)
        self.async_update_listeners()
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = self.config_entry.async_create_background_task(
                self.hass, self._reconcile(), f"{c.DOMAIN} reconcile {self.uid}"
            )
        await request.done

    def system_view(self) -> dict[str, Any]:
        """The system datagram with the desired values applied."""
        return self._view(None, self.data.system)

    def zone_view(self, index: int) -> dict[str, Any]:
        """One zone's datagram with the desired values applied."""
        return self._view(index, self.data.zones.get(index, {}))

    def _view(self, index: int | None, raw: dict[str, Any]) -> dict[str, Any]:
        overlay = {f: w.value for (i, f), w in self._wants.items() if i == index}
        return {**raw, **overlay} if overlay else raw

    def status(self, match: Callable[[PendingKey], bool]) -> str:
        """Progress of the open requests touching the fields `match` selects."""
        keys = {
            key for r in self._requests if not r.done.done() for key in r.keys
            if match(key)
        }
        if not keys:
            now = time.monotonic()
            failed = any(match(k) and now < t for k, t in self._failed.items())
            return c.STATUS_FAILED if failed else c.STATUS_READY
        open_keys = [k for k in keys if k in self._wants]
        sent = sum(
            1 for k in keys if k not in self._wants or self._wants[k].sent_at
        )
        if all(self._wants[k].sent_at for k in open_keys):
            return c.STATUS_CONFIRMING
        if not sent:
            return c.STATUS_QUEUED
        return f"{c.STATUS_SENDING} ({sent} of {len(keys)})"

    # The reconcile loop --------------------------------------------------

    async def _reconcile(self) -> None:
        try:
            while self._wants:
                try:
                    await self._sync()
                except IZoneError as err:
                    _LOGGER.debug("Sync failed, will retry: %s", err)
                    self._settle(None)  # still let deadlines expire
                if not self._wants:
                    break
                step = self._next_step()
                wait = self.client.command_ready_in()
                if step is not None and wait <= 0:
                    await self._send(*step)
                    continue
                await asyncio.sleep(
                    min(wait, c.CONFIRM_INTERVAL) if step else c.CONFIRM_INTERVAL
                )
        finally:
            self._settle(None)
            self.async_update_listeners()
        # Clear the "failed" status once it has been shown.
        if self._failed:
            await asyncio.sleep(c.FAILED_SHOW)
            now = time.monotonic()
            self._failed = {k: t for k, t in self._failed.items() if t > now}
            self.async_update_listeners()

    async def _sync(self) -> None:
        """Read the system and the zones that have pending changes."""
        zones = {i for i, _ in self._wants if i is not None}
        off = self._wants.get((None, "SysOn"))
        if off and off.unless_zones_on and not off.sent_at:
            # Whether the AC goes off depends on every zone: read them all.
            zones = set(self.data.zones)
        _, system = await self.client.get_system()
        data = IZoneData(system=system, zones=dict(self.data.zones))
        for index in sorted(zones):
            data.zones[index] = await self.client.get_zone(index)
        self._settle(data)
        self.async_set_updated_data(data)

    def _settle(self, data: IZoneData | None) -> None:
        """Drop the wants the bridge now shows; fail the hopeless ones."""
        data = data or self.data
        now = time.monotonic()
        for key, want in list(self._wants.items()):
            actual = self._raw(data, key)
            if actual == want.value:
                del self._wants[key]
            elif now > want.deadline:
                self._fail(key, "no response from the bridge")
            elif (
                want.attempts >= c.MAX_ATTEMPTS
                and want.sent_at
                and now - want.sent_at > c.RESEND_AFTER
            ):
                self._fail(key, "the bridge accepted it but didn't apply it")
        # The AC only goes off if no zone is on, judged on fresh data (and
        # changes still on their way): a zone opened from the wall controller
        # in the meantime keeps it on.
        off = self._wants.get((None, "SysOn"))
        if off and off.value == 0 and off.unless_zones_on and not off.sent_at:
            if any(c.zone_is_on(self._view(i, z)) for i, z in data.zones.items()):
                del self._wants[(None, "SysOn")]
        for request in self._requests:
            if not request.done.done() and not request.keys & self._wants.keys():
                if request.errors:
                    request.done.set_exception(
                        IZoneCommandError("; ".join(request.errors))
                    )
                else:
                    request.done.set_result(None)
        self._requests = [r for r in self._requests if not r.done.done()]

    def _fail(self, key: PendingKey, reason: str) -> None:
        want = self._wants.pop(key)
        index, name = key
        where = "AC" if index is None else self.data.zones.get(index, {}).get(
            "Name", f"zone {index + 1}"
        )
        message = f"{where} {name}={want.value}: {reason}"
        _LOGGER.warning("Change not applied: %s", message)
        self._failed[key] = time.monotonic() + c.FAILED_SHOW
        for request in self._requests:
            if key in request.keys:
                request.errors.append(message)

    def _next_step(self) -> tuple[PendingKey, _Want] | None:
        """The next command to send, in a safe order, or None to wait."""
        now = time.monotonic()
        ready = []
        for key, want in list(self._wants.items()):
            due = want.sent_at is None or (
                now - want.sent_at > c.RESEND_AFTER and want.attempts < c.MAX_ATTEMPTS
            )
            if due and not self._blocked(key, want):
                ready.append((self._order(key, want.value), key, want))
        if not ready:
            return None
        ready.sort(key=lambda r: r[0])  # stable: same order keeps request order
        _, key, want = ready[0]
        return key, want

    @staticmethod
    def _order(key: PendingKey, value: Any) -> int:
        index, name = key
        if name == "Mode" and value == c.ZONE_MODE_CLOSE or name == "SysMode":
            return 0  # close zones and set the mode before powering on
        if name == "Mode":
            return 1
        if name == "SysOn":
            return 3 if value else 4
        return 2

    def _blocked(self, key: PendingKey, want: _Want) -> bool:
        index, name = key
        if name == "Setpoint" and index is not None:
            # The bridge drops a setpoint unless the zone is already in Auto.
            mode = self.data.zones.get(index, {}).get("Mode")
            if mode in (c.ZONE_MODE_AUTO, c.ZONE_MODE_OVERRIDE):
                return False
            if (index, "Mode") not in self._wants:
                self._wants[(index, "Mode")] = _Want(c.ZONE_MODE_AUTO, want.deadline)
            return True
        if name == "SysOn":
            # Power changes go after zone closes (and, for on, the mode).
            return any(
                w.sent_at is None
                and (
                    (n == "Mode" and w.value == c.ZONE_MODE_CLOSE)
                    or (n == "SysMode" and want.value)
                )
                for (_, n), w in self._wants.items()
            )
        return False

    async def _send(self, key: PendingKey, want: _Want) -> None:
        want.sent_at = time.monotonic()
        want.attempts += 1
        self.async_update_listeners()
        try:
            await self._command(key, want.value)()
        except IZoneError as err:
            if key in self._wants:
                self._fail(key, str(err))
                self._settle(None)

    def _command(self, key: PendingKey, value: Any) -> Callable[[], Awaitable[None]]:
        client = self.client
        index, name = key
        if index is not None:
            if name == "Mode":
                return lambda: client.set_zone_mode(index, value)
            return lambda: client.set_zone_setpoint(index, from_centi(value))
        return {
            "SysOn": lambda: client.set_power(bool(value)),
            "SysMode": lambda: client.set_mode(value),
            "SysFan": lambda: client.set_fan(value),
            "Setpoint": lambda: client.set_setpoint(from_centi(value)),
            "iSaveOn": lambda: client.set_isave(bool(value)),
        }[name]

    @staticmethod
    def _raw(data: IZoneData, key: PendingKey) -> Any:
        index, name = key
        return (data.system if index is None else data.zones.get(index, {})).get(name)
