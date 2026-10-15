"""Climate entities: the AC unit, plus one per zone."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
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
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import const as c
from .api import from_centi
from .coordinator import IZoneConfigEntry, IZoneCoordinator
from .entity import IZoneEntity, IZoneZoneEntity

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
HA_TO_SYS_FAN = {v: k for k, v in SYS_FAN_TO_HA.items()}

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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IZoneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[ClimateEntity] = [IZoneSystemClimate(coordinator)]
    entities.extend(
        IZoneZoneClimate(coordinator, index) for index in coordinator.data.zones
    )
    async_add_entities(entities)


class IZoneSystemClimate(IZoneEntity, ClimateEntity):
    """The ducted AC unit itself."""

    _attr_name = None  # use the device name
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = c.TEMP_STEP
    _attr_min_temp = c.MIN_TEMP
    _attr_max_temp = c.MAX_TEMP
    _attr_hvac_modes = [HVACMode.OFF, *SYS_MODE_TO_HVAC.values()]
    _attr_fan_modes = list(SYS_FAN_TO_HA.values())
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
    def current_temperature(self) -> float | None:
        zone = self._control_zone
        if zone is not None:
            return from_centi(self.coordinator.data.zones[zone].get("Temp"))
        return from_centi(self.system.get("Temp"))

    @property
    def target_temperature(self) -> float | None:
        zone = self._control_zone
        if zone is not None:
            return from_centi(self.coordinator.data.zones[zone].get("Setpoint"))
        return from_centi(self.system.get("Setpoint"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "supply_temperature": from_centi(self.system.get("Supply")),
            "return_temperature": from_centi(self.system.get("Temp")),
            "control_zone": self._control_zone,
            "sleep_timer": self.system.get("SleepTimer"),
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        client = self.coordinator.client
        if hvac_mode == HVACMode.OFF:
            await self._run(client.set_power(False))
            return
        await self._run(client.set_mode(HVAC_TO_SYS_MODE[hvac_mode]))
        if not self.system.get("SysOn"):
            await self._run(client.set_power(True))

    async def async_turn_on(self) -> None:
        await self._run(self.coordinator.client.set_power(True))

    async def async_turn_off(self) -> None:
        await self._run(self.coordinator.client.set_power(False))

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._run(self.coordinator.client.set_fan(HA_TO_SYS_FAN[fan_mode]))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        zone = self._control_zone
        if zone is not None:
            # SysSetpoint is ignored while a zone is in control.
            mode = self.coordinator.data.zones[zone].get("Mode")
            await self._run(
                self.coordinator.client.ensure_zone_setpoint(zone, temp, mode)
            )
        else:
            await self._run(self.coordinator.client.set_setpoint(temp))


class IZoneZoneClimate(IZoneZoneEntity, ClimateEntity):
    """One zone: closed (off), open (fan only) or temperature controlled (auto)."""

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = c.TEMP_STEP
    _attr_min_temp = c.MIN_TEMP
    _attr_max_temp = c.MAX_TEMP

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
        return ZONE_MODE_TO_HVAC.get(self.zone.get("Mode"))

    @property
    def current_temperature(self) -> float | None:
        temp = self.zone.get("Temp")
        # Zones without a sensor report 0.
        return from_centi(temp) if temp else None

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
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._run(
            self.coordinator.client.set_zone_mode(
                self.index, HVAC_TO_ZONE_MODE[hvac_mode]
            )
        )

    async def async_turn_on(self) -> None:
        mode = HVACMode.AUTO if self._is_auto_zone else HVACMode.FAN_ONLY
        await self.async_set_hvac_mode(mode)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        await self._run(
            self.coordinator.client.ensure_zone_setpoint(
                self.index, temp, self.zone.get("Mode")
            )
        )
