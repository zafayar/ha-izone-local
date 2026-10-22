"""Polling coordinator for one iZone bridge, and the loop that applies changes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Iterable
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

_UNSEEN = object()  # a want's last_seen before the first sync after the request


class _ZeroedReply(IZoneError):
    """The bridge answered with placeholder data (see const.zeroed_reply)."""


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
    # The bridge's value at the previous sync. If it moves somewhere other
    # than what we asked for, someone else (the app, the wall controller)
    # changed it, and their newer change wins.
    last_seen: Any = _UNSEEN
    # The zone's sensor has dropped out; nothing is sent until it's back.
    waiting_for_sensor: bool = False
    # How long to give a send before re-sending (None: RESEND_AFTER).
    resend_after: float | None = None

    def due_again(self, now: float) -> bool:
        """Sent, but long enough ago that it should have shown by now."""
        wait = c.RESEND_AFTER if self.resend_after is None else self.resend_after
        return self.sent_at is not None and now - self.sent_at > wait


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
        self._failures: deque[float] = deque(maxlen=64)  # monotonic times
        self._zeroed_since: float | None = None  # first of a run of zeroed replies

    async def _async_update_data(self) -> IZoneData:
        try:
            _, system = await self.client.get_system()
            zones: dict[int, dict[str, Any]] = {}
            for index in range(int(system.get("NoOfZones", 0))):
                zones[index] = await self.client.get_zone(index)
            self._check_reply(system, zones.values())
        except _ZeroedReply as err:
            return self._keep_last_data(err)
        except IZoneError as err:
            self._failures.append(time.monotonic())
            raise UpdateFailed(str(err)) from err
        data = IZoneData(system=system, zones=zones)
        self._settle(data)
        self._start_loop()  # e.g. a zone's sensor came back
        return data

    def _check_reply(self, system: dict[str, Any], zones: Iterable[dict]) -> None:
        """Raise _ZeroedReply for placeholder data, so it's never compared
        with the desired state (a zeroed zone would look like a change made
        elsewhere, and the AC like it was turned off)."""
        if c.zeroed_reply(system, zones):
            raise _ZeroedReply("Bridge answered with zeroed data")
        self._zeroed_since = None

    def _keep_last_data(self, err: _ZeroedReply) -> IZoneData:
        """Ride out zeroed replies with the last good state, for ZEROED_GRACE."""
        now = time.monotonic()
        if self._zeroed_since is None:
            self._zeroed_since = now
            _LOGGER.debug("%s; keeping the last state", err)
        if self.data is None or now - self._zeroed_since > c.ZEROED_GRACE:
            self._failures.append(now)
            raise UpdateFailed(
                f"{err} for {round(now - self._zeroed_since)} s; the controller "
                "may not be talking to the bridge"
            ) from err
        return self.data

    # Desired state -------------------------------------------------------

    async def async_request(
        self,
        changes: dict[PendingKey, Any],
        *,
        ac_off_unless_zones_on: bool = False,
        prelude: Callable[[], Coroutine[Any, Any, None]] | None = None,
        wait: bool = True,
    ) -> None:
        """Show `changes` now, then wait until the bridge shows them too.

        With `ac_off_unless_zones_on`, a request to turn the AC off is dropped
        (without error) if a zone turns out to be on when the bridge is read.
        `prelude` is one command that should produce all the changes (e.g.
        running a favourite); they then count as sent once, and only what's
        still missing after FAVOURITE_SETTLE is sent one by one.
        With `wait` False this returns once the prelude is accepted, and the
        rest carries on in the background (problems are logged).
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
        if prelude is not None:
            await self._send_prelude(prelude, changes)
        self._start_loop()
        if wait:
            await request.done
        else:
            # Errors were logged by _fail; mark the result as seen.
            request.done.add_done_callback(lambda f: f.cancelled() or f.exception())

    async def _send_prelude(
        self,
        prelude: Callable[[], Coroutine[Any, Any, None]],
        changes: dict[PendingKey, Any],
    ) -> None:
        wants = [self._wants[k] for k in changes if k in self._wants]
        now = time.monotonic()
        for want in wants:
            want.sent_at, want.attempts = now, 1
            want.resend_after = c.FAVOURITE_SETTLE
            settle = now + c.FAVOURITE_SETTLE + c.PENDING_TIMEOUT
            want.deadline = max(want.deadline, settle)
        self.async_update_listeners()
        try:
            await prelude()
        except IZoneError as err:
            _LOGGER.info("%s; applying the changes one by one instead", err)
            for want in wants:
                want.sent_at, want.attempts, want.resend_after = None, 0, None

    def favourite_changes(
        self, favourite: dict[str, Any]
    ) -> tuple[dict[PendingKey, Any], bool]:
        """The state a favourite produces, and whether the controller can
        apply it as a whole: it refuses a favourite that needs a zone whose
        sensor is out, so that one has to be applied zone by zone."""
        changes: dict[PendingKey, Any] = {}
        if favourite.get("Mode") in range(c.MODE_COOL, c.MODE_AUTO + 1):
            changes[(None, "SysMode")] = favourite["Mode"]
        if favourite.get("Fan") in range(c.FAN_LOW, c.FAN_TOP + 1):
            changes[(None, "SysFan")] = favourite["Fan"]
        targets = favourite.get("Zones") or []
        whole, any_on = True, False
        for index, zone in self.data.zones.items():
            if index >= len(targets) or zone.get("Bypass"):
                continue
            if zone.get("ZoneType") == c.ZONE_TYPE_CONSTANT:
                continue  # the controller runs constant zones itself
            mode = targets[index].get("Mode")
            if mode not in (c.ZONE_MODE_OPEN, c.ZONE_MODE_CLOSE, c.ZONE_MODE_AUTO):
                continue  # left alone by this favourite
            changes[(index, "Mode")] = mode
            any_on = any_on or mode != c.ZONE_MODE_CLOSE
            if mode == c.ZONE_MODE_AUTO:
                changes[(index, "Setpoint")] = targets[index].get("Setpoint")
                whole = whole and not c.sensor_faulted(zone)
        if any_on:
            changes[(None, "SysOn")] = 1  # the controller turns the AC on
        return changes, whole

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
        """Progress of the changes touching the fields `match` selects: those
        of open requests, plus any still being applied (e.g. after a sensor
        came back)."""
        keys = {
            key for r in self._requests if not r.done.done() for key in r.keys
            if match(key)
        } | {k for k, w in self._wants.items() if match(k) and not w.waiting_for_sensor}
        if not keys:
            now = time.monotonic()
            if any(match(k) and w.waiting_for_sensor for k, w in self._wants.items()):
                return c.STATUS_WAITING_SENSOR
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

    # Health and diagnostics ---------------------------------------------

    @property
    def recent_failures(self) -> int:
        """Failed changes and reads within FAILURE_WINDOW."""
        since = time.monotonic() - c.FAILURE_WINDOW
        while self._failures and self._failures[0] < since:
            self._failures.popleft()
        return len(self._failures)

    @property
    def bridge_problem(self) -> bool:
        """Failing often enough that something needs attention (e.g. a
        power cycle of the bridge from an automation)."""
        return self.recent_failures >= c.FAILURE_THRESHOLD

    def pending_for_diagnostics(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        return [
            {
                "where": self._describe(key),
                "field": key[1],
                "value": want.value,
                "attempts": want.attempts,
                "sent_seconds_ago": (
                    round(now - want.sent_at, 1) if want.sent_at else None
                ),
                "waiting_for_sensor": want.waiting_for_sensor,
            }
            for key, want in self._wants.items()
        ]

    def notify_changed(self) -> None:
        """The bridge broadcast that something changed: read it soon.

        Ignored while the reconcile loop runs, since it's reading anyway. The
        refresh is debounced, so a burst of broadcasts costs one read.
        """
        if self._loop_task is not None and not self._loop_task.done():
            return
        self.config_entry.async_create_background_task(
            self.hass, self.async_request_refresh(), f"{c.DOMAIN} notified {self.uid}"
        )

    # The reconcile loop --------------------------------------------------

    def _has_work(self) -> bool:
        """Changes to send or confirm. One waiting for a sensor isn't work:
        the regular poll watches for the sensor and restarts the loop."""
        return any(not w.waiting_for_sensor for w in self._wants.values())

    def _start_loop(self) -> None:
        if self._has_work() and (self._loop_task is None or self._loop_task.done()):
            self._loop_task = self.config_entry.async_create_background_task(
                self.hass, self._reconcile(), f"{c.DOMAIN} reconcile {self.uid}"
            )

    async def _reconcile(self) -> None:
        try:
            while self._has_work():
                try:
                    await self._sync()
                except IZoneError as err:
                    _LOGGER.debug("Sync failed, will retry: %s", err)
                    if not isinstance(err, _ZeroedReply):
                        self._failures.append(time.monotonic())
                    self._settle(None)  # still let deadlines expire
                if not self._has_work():
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
        self._check_reply(system, (data.zones[i] for i in zones))
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
                continue
            if self._sensor_wait(data, key, want, now):
                continue
            seen, want.last_seen = want.last_seen, actual
            if seen is not _UNSEEN and actual != seen:
                del self._wants[key]
                _LOGGER.info(
                    "%s was changed elsewhere to %r while %r was being applied; "
                    "keeping the newer change",
                    self._describe(key), actual, want.value,
                )
                continue
            if now > want.deadline:
                self._fail(key, "no response from the bridge")
            elif want.attempts >= c.MAX_ATTEMPTS and want.due_again(now):
                self._fail(key, "the bridge accepted it but didn't apply it")
        # The AC only goes off if no zone is on, judged on fresh data (and
        # changes still on their way): a zone opened from the wall controller
        # in the meantime keeps it on.
        off = self._wants.get((None, "SysOn"))
        if (
            off
            and off.value == 0
            and off.unless_zones_on
            and not off.sent_at
            and any(c.zone_is_on(self._view(i, z)) for i, z in data.zones.items())
        ):
            del self._wants[(None, "SysOn")]
        active = {k for k, w in self._wants.items() if not w.waiting_for_sensor}
        for request in self._requests:
            if not request.done.done() and not request.keys & active:
                if request.errors:
                    request.done.set_exception(
                        IZoneCommandError("; ".join(request.errors))
                    )
                else:
                    request.done.set_result(None)
        self._requests = [r for r in self._requests if not r.done.done()]

    def _sensor_wait(
        self, data: IZoneData, key: PendingKey, want: _Want, now: float
    ) -> bool:
        """Hold a change that needs a zone's sensor while the sensor is out.

        Returns True while it's held: nothing is sent, and it isn't judged,
        because the controller reports the zone as closed until it recovers.
        """
        index, name = key
        zone = data.zones.get(index) if index is not None else None
        needs_sensor = name == "Setpoint" or (
            name == "Mode" and want.value in (c.ZONE_MODE_AUTO, c.ZONE_MODE_OVERRIDE)
        )
        if zone is not None and needs_sensor and c.sensor_faulted(zone):
            if not want.waiting_for_sensor:
                _LOGGER.warning(
                    "%s: the zone's sensor isn't responding; %s=%r will be applied "
                    "when it's back",
                    self._describe(key), name, want.value,
                )
                want.waiting_for_sensor = True
                want.deadline = max(want.deadline, now + c.SENSOR_WAIT)
            want.last_seen, want.sent_at, want.attempts = _UNSEEN, None, 0
            return True
        if want.waiting_for_sensor:
            _LOGGER.info("%s: the zone's sensor is back", self._describe(key))
            want.waiting_for_sensor = False
            want.deadline = now + c.PENDING_TIMEOUT
        return False

    def _clear_failed(self) -> None:
        now = time.monotonic()
        self._failed = {k: t for k, t in self._failed.items() if t > now}
        self.async_update_listeners()

    def _describe(self, key: PendingKey) -> str:
        index, _ = key
        if index is None:
            return "AC"
        return self.data.zones.get(index, {}).get("Name") or f"Zone {index + 1}"

    def _fail(self, key: PendingKey, reason: str) -> None:
        want = self._wants.pop(key)
        message = f"{self._describe(key)} {key[1]}={want.value}: {reason}"
        _LOGGER.warning("Change not applied: %s", message)
        self._failures.append(time.monotonic())
        self._failed[key] = time.monotonic() + c.FAILED_SHOW
        # A timer, not the loop, clears it: a lingering loop would look busy
        # to the next request and never pick it up.
        asyncio.get_running_loop().call_later(c.FAILED_SHOW, self._clear_failed)
        for request in self._requests:
            if key in request.keys:
                request.errors.append(message)

    def _next_step(self) -> tuple[PendingKey, _Want] | None:
        """The next command to send, in a safe order, or None to wait."""
        now = time.monotonic()
        ready = []
        for key, want in list(self._wants.items()):
            if want.waiting_for_sensor:
                continue
            due = want.sent_at is None or (
                want.due_again(now) and want.attempts < c.MAX_ATTEMPTS
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
        _, name = key
        if (name == "Mode" and value == c.ZONE_MODE_CLOSE) or name == "SysMode":
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
        want.resend_after = None  # sent on its own now: normal timing
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
            return {
                "Mode": lambda: client.set_zone_mode(index, value),
                "Setpoint": lambda: client.set_zone_setpoint(index, from_centi(value)),
                "MinAir": lambda: client.set_zone_min_air(index, value),
                "MaxAir": lambda: client.set_zone_max_air(index, value),
            }[name]
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
