"""Sleep timer: the AC turns itself off after the chosen time."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SLEEP_TIMER_OPTIONS, sleep_timer_option
from .coordinator import SLEEP_TIMER_KEY, IZoneConfigEntry, IZoneCoordinator, PendingKey
from .entity import IZoneEntity

# Each platform's updates are read by the coordinator, and commands are
# queued by the client, so entities needn't be limited here.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([IZoneSleepTimer(entry.runtime_data)])


class IZoneSleepTimer(IZoneEntity, SelectEntity):
    """The controller's own sleep timer, with the wall panel's steps.

    It runs on the controller (and shows on the wall panel), so it still
    turns the AC off if Home Assistant is down. While it runs, the option
    shown is the step the time left rounds up to; the "Sleep timer ends"
    sensor has the exact time. Choosing a time while the AC is off turns the
    AC on for that long, as the panel's button does.
    """

    _attr_translation_key = "sleep_timer"
    _attr_options = list(SLEEP_TIMER_OPTIONS)

    def __init__(self, coordinator: IZoneCoordinator) -> None:
        super().__init__(coordinator, "sleep_timer")

    @property
    def current_option(self) -> str:
        if not self.system.get("SysOn"):
            return "off"  # turning the AC off ends the timer
        return sleep_timer_option(self.system.get("SleepTimerM"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "minutes_left": self.system.get("SleepTimerM") or 0,
            "status": self.status,
            "pending": self.pending,
        }

    def _matches(self, key: PendingKey) -> bool:
        return key == SLEEP_TIMER_KEY

    async def async_select_option(self, option: str) -> None:
        minutes = SLEEP_TIMER_OPTIONS[option]
        on = bool(self.system.get("SysOn"))
        if not on and not minutes:
            return  # no timer runs while the AC is off
        changes: dict[PendingKey, Any] = {SLEEP_TIMER_KEY: minutes}
        if not on:
            changes[(None, "SysOn")] = 1  # the controller turns the AC on
        await self._apply(changes)
