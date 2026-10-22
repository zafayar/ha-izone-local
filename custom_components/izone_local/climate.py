"""Climate entities: the AC unit, plus one per zone."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import const as c
from .api import from_centi, to_centi
from .coordinator import IZoneConfigEntry, IZoneCoordinator, PendingKey
from .entity import IZoneEntity, IZoneZoneEntity

# Each platform's updates are read by the coordinator, and commands are
# queued by the client, so entities needn't be limited here.
PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

FAN_TOP = "top"

SYS_MODE_TO_HVAC = {
    c.MODE_COOL: HVACMode.COOL,
    c.MODE_HEAT: HVACMode.HEAT,
    c.MODE_VENT: HVACMode.FAN_ONLY,
    c.MODE_DRY: HVACMode.DRY,
    c.MODE_AUTO: HVACMode.HEAT_COOL,
}
HVAC_TO_SYS_MODE = {v: k for k, v in SYS_MODE_TO_HVAC.items()}

SYS_FAN_TO_HA = {
    c.FAN_LOW: FAN_LOW,
    c.FAN_MED: FAN_MEDIUM,
    c.FAN_HIGH: FAN_HIGH,
    c.FAN_AUTO: FAN_AUTO,
    c.FAN_TOP: FAN_TOP,
}
# Speeds some units report but that aren't offered.
SYS_FAN_TO_HA |= {c.FAN_QUIET: "quiet", c.FAN_TURBO: "turbo"}
HA_TO_SYS_FAN = {v: k for k, v in SYS_FAN_TO_HA.items()}

# System fields the AC entity changes; a pending one shows its hourglass.
SYS_FIELDS = ("SysOn", "SysMode", "SysFan", "Setpoint")

ZONE_MODE_TO_HVAC = {
    c.ZONE_MODE_CLOSE: HVACMode.OFF,
    c.ZONE_MODE_OPEN: HVACMode.FAN_ONLY,
    c.ZONE_MODE_CONSTANT: HVACMode.FAN_ONLY,
    c.ZONE_MODE_AUTO: HVACMode.AUTO,
    c.ZONE_MODE_OVERRIDE: HVACMode.AUTO,
}
HVAC_TO_ZONE_MODE = {
    HVACMode.OFF: c.ZONE_MODE_CLOSE,
    HVACMode.FAN_ONLY: c.ZONE_MODE_OPEN,
    HVACMode.AUTO: c.ZONE_MODE_AUTO,
}

class _TemperatureLimits:
    """The setpoint range: the eco-lock limits while the lock is on."""

    system: dict[str, Any]

    def _eco(self, field: str) -> float | None:
        if not self.system.get("EcoLock"):
            return None
        return from_centi(self.system.get(field))

    @property
    def min_temp(self) -> float:
        low = self._eco("EcoMin")
        return max(c.MIN_TEMP, low) if low else c.MIN_TEMP

    @property
    def max_temp(self) -> float:
        high = self._eco("EcoMax")
        return min(c.MAX_TEMP, high) if high else c.MAX_TEMP

    def _clamped(self, temp: float) -> float:
        return min(max(float(temp), self.min_temp), self.max_temp)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[ClimateEntity] = [IZoneSystemClimate(coordinator)]
    # A bypass zone is a pressure-relief damper the system drives itself, so it
    # gets no climate entity (its damper sensors are still created).
    entities.extend(
        IZoneZoneClimate(coordinator, index)
        for index, zone in coordinator.data.zones.items()
        if not zone.get("Bypass")
    )
    async_add_entities(entities)


class IZoneSystemClimate(_TemperatureLimits, IZoneEntity, ClimateEntity):
    """The ducted AC unit itself."""

    _attr_name = None  # use the device name
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = c.TEMP_STEP
    _attr_hvac_modes = [HVACMode.OFF, *SYS_MODE_TO_HVAC.values()]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: IZoneCoordinator) -> None:
        super().__init__(coordinator, "climate")

    @property
    def _control_zone(self) -> int | None:
        """Index of the zone whose sensor drives the unit, or None for the unit."""
        ctrl = self.system.get("CtrlZone", c.CTRL_ZONE_UNIT)
        if ctrl in self.coordinator.data.zones:
            return ctrl
        return None

    @property
    def hvac_mode(self) -> HVACMode | None:
        if not self.system.get("SysOn"):
            return HVACMode.OFF
        return SYS_MODE_TO_HVAC.get(self.system.get("SysMode"))

    @property
    def fan_mode(self) -> str | None:
        return SYS_FAN_TO_HA.get(self.system.get("SysFan"))

    @property
    def fan_modes(self) -> list[str]:
        """The speeds this unit's fan has (e.g. no "top" on a 3-speed fan)."""
        return [SYS_FAN_TO_HA[fan] for fan in c.fan_speeds(self.coordinator.data.system)]

    @property
    def current_temperature(self) -> float | None:
        zone = self._control_zone
        if zone is not None:
            return from_centi(self.coordinator.zone_view(zone).get("Temp"))
        return from_centi(self.system.get("Temp"))

    @property
    def target_temperature(self) -> float | None:
        zone = self._control_zone
        if zone is not None:
            return from_centi(self.coordinator.zone_view(zone).get("Setpoint"))
        return from_centi(self.system.get("Setpoint"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "supply_temperature": from_centi(self.system.get("Supply")),
            "return_temperature": from_centi(self.system.get("Temp")),
            "control_zone": self._control_zone,
            "sleep_timer": self.system.get("SleepTimer"),
            "status": self.status,
            "pending": self.pending,
        }

    def _matches(self, key: PendingKey) -> bool:
        index, name = key
        if index is None:
            return name in SYS_FIELDS
        return index == self._control_zone and name == "Setpoint"

    def _power(self, on: bool) -> dict[PendingKey, Any]:
        if bool(self.system.get("SysOn")) == on:
            return {}
        return {(None, "SysOn"): int(on)}

    def _mode_changes(self, hvac_mode: HVACMode) -> dict[PendingKey, Any]:
        if hvac_mode == HVACMode.OFF:
            return self._power(False)
        if hvac_mode not in HVAC_TO_SYS_MODE:
            raise ServiceValidationError(f"The AC has no '{hvac_mode}' mode")
        changes = self._power(True)
        if self.system.get("SysMode") != HVAC_TO_SYS_MODE[hvac_mode]:
            changes[(None, "SysMode")] = HVAC_TO_SYS_MODE[hvac_mode]
        return changes

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._apply(self._mode_changes(hvac_mode))

    async def async_turn_on(self) -> None:
        await self._apply(self._power(True))

    async def async_turn_off(self) -> None:
        await self._apply(self._power(False))

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if fan_mode not in self.fan_modes:
            raise ServiceValidationError(
                f"This unit's fan speeds are {', '.join(self.fan_modes)}"
            )
        fan = HA_TO_SYS_FAN[fan_mode]
        if self.system.get("SysFan") != fan:
            await self._apply({(None, "SysFan"): fan})

    async def async_set_temperature(self, **kwargs: Any) -> None:
        # A mode can come with the temperature; both go in one request.
        changes: dict[PendingKey, Any] = {}
        if (hvac_mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            changes |= self._mode_changes(hvac_mode)
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            changes |= self._temperature_changes(to_centi(self._clamped(temp)))
        await self._apply(changes)

    def _temperature_changes(self, setpoint: int) -> dict[PendingKey, Any]:
        zone = self._control_zone
        if zone is None:
            return {(None, "Setpoint"): setpoint}
        # SysSetpoint is ignored while a zone is in control.
        changes: dict[PendingKey, Any] = {(zone, "Setpoint"): setpoint}
        mode = self.coordinator.zone_view(zone).get("Mode")
        if mode not in (c.ZONE_MODE_AUTO, c.ZONE_MODE_OVERRIDE):
            changes[(zone, "Mode")] = c.ZONE_MODE_AUTO
        return changes


class IZoneZoneClimate(_TemperatureLimits, IZoneZoneEntity, ClimateEntity):
    """One zone: closed (off), open (fan only) or temperature controlled (auto).

    The AC's power follows the zones: turning a zone on turns the AC on, and
    turning the last zone off turns the AC off. While the AC is off, every
    zone reports off, whatever its damper setting.
    """

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = c.TEMP_STEP

    def __init__(self, coordinator: IZoneCoordinator, index: int) -> None:
        super().__init__(coordinator, index, "climate")

    @property
    def _is_auto_zone(self) -> bool:
        return self.zone.get("ZoneType") == c.ZONE_TYPE_AUTO

    @property
    def hvac_modes(self) -> list[HVACMode]:
        if self._is_auto_zone:
            return [HVACMode.OFF, HVACMode.FAN_ONLY, HVACMode.AUTO]
        return [HVACMode.OFF, HVACMode.FAN_ONLY]

    @property
    def supported_features(self) -> ClimateEntityFeature:
        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if self._is_auto_zone:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        return features

    @property
    def hvac_mode(self) -> HVACMode | None:
        if not self.system.get("SysOn"):
            return HVACMode.OFF
        return ZONE_MODE_TO_HVAC.get(self.zone.get("Mode"))

    @property
    def current_temperature(self) -> float | None:
        temp = self.zone.get("Temp")
        # Zones without a sensor report 0; a dropped-out sensor's value is stale.
        if not temp or c.sensor_faulted(self.zone):
            return None
        return from_centi(temp)

    @property
    def target_temperature(self) -> float | None:
        if not self._is_auto_zone:
            return None
        return from_centi(self.zone.get("Setpoint"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "zone_index": self.index,
            "damper_position": self.zone.get("DmpPos"),
            "max_airflow": self.zone.get("MaxAir"),
            "min_airflow": self.zone.get("MinAir"),
            "override": self.zone.get("Mode") == c.ZONE_MODE_OVERRIDE,
            "constant": bool(self.zone.get("ConstA")),
            "status": self.status,
            "pending": self.pending,
        }

    def _matches(self, key: PendingKey) -> bool:
        # A change to the AC's power flips every zone's state too.
        return key[0] == self.index or key == (None, "SysOn")

    def _check_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode not in self.hvac_modes:
            raise ServiceValidationError(
                f"A zone can only be {', '.join(self.hvac_modes)}; heat, cool and "
                "dry are set on the AC itself"
            )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._check_mode(hvac_mode)
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        changes: dict[PendingKey, Any] = {}
        # Override is a flavour of Auto; don't cancel it just to turn on.
        if ZONE_MODE_TO_HVAC.get(self.zone.get("Mode")) != hvac_mode:
            changes[(self.index, "Mode")] = HVAC_TO_ZONE_MODE[hvac_mode]
        await self._apply(changes | self._ac_on())

    async def async_turn_on(self) -> None:
        mode = HVACMode.AUTO if self._is_auto_zone else HVACMode.FAN_ONLY
        await self.async_set_hvac_mode(mode)

    async def async_turn_off(self) -> None:
        changes: dict[PendingKey, Any] = {}
        if self.zone.get("Mode") != c.ZONE_MODE_CLOSE:
            changes[(self.index, "Mode")] = c.ZONE_MODE_CLOSE
        if self.system.get("SysOn") and not self._other_zones_on():
            changes[(None, "SysOn")] = 0
        # The coordinator re-reads every zone before turning the AC off, and
        # keeps it on if one was opened elsewhere in the meantime.
        await self._apply(changes, ac_off_unless_zones_on=True)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        if hvac_mode is not None:
            self._check_mode(hvac_mode)
            if hvac_mode != HVACMode.AUTO:
                # The bridge would switch the zone back to Auto to take a
                # temperature, so honour the mode and skip the temperature.
                _LOGGER.warning(
                    "%s: set to %s; a temperature only applies in auto",
                    self.entity_id, hvac_mode,
                )
                await self.async_set_hvac_mode(hvac_mode)
                return
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        # The bridge only keeps a setpoint on a zone in Auto, so setting a
        # temperature turns the zone on, and therefore the AC too.
        setpoint = to_centi(self._clamped(temp))
        changes: dict[PendingKey, Any] = {(self.index, "Setpoint"): setpoint}
        if self.zone.get("Mode") not in (c.ZONE_MODE_AUTO, c.ZONE_MODE_OVERRIDE):
            changes[(self.index, "Mode")] = c.ZONE_MODE_AUTO
        await self._apply(changes | self._ac_on())

    def _other_zones_on(self) -> bool:
        coordinator = self.coordinator
        return any(
            c.zone_is_on(coordinator.zone_view(index))
            for index in coordinator.data.zones
            if index != self.index
        )

    def _ac_on(self) -> dict[PendingKey, Any]:
        """Turn the AC on for this zone alone.

        Zones left open when the AC went off show as off, so close them first;
        otherwise they would come back on with the AC. (The AC's own on
        button does bring them back, like a resume.)
        """
        if self.system.get("SysOn"):
            return {}
        changes: dict[PendingKey, Any] = {
            (index, "Mode"): c.ZONE_MODE_CLOSE
            for index in self.coordinator.data.zones
            if index != self.index and c.zone_is_on(self.coordinator.zone_view(index))
        }
        changes[(None, "SysOn")] = 1
        return changes
