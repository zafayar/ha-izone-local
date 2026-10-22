"""Minimal stand-ins for the Home Assistant APIs izone_local imports.

Only base classes, enums and constants. Enough to import the platforms and
run async_setup_entry; not a substitute for real HA behaviour.
"""

import sys
import types
from dataclasses import dataclass
from enum import IntFlag, StrEnum
from typing import Any


def mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class HVACMode(StrEnum):
    OFF = "off"
    HEAT = "heat"
    COOL = "cool"
    HEAT_COOL = "heat_cool"
    AUTO = "auto"
    DRY = "dry"
    FAN_ONLY = "fan_only"


class ClimateEntityFeature(IntFlag):
    TARGET_TEMPERATURE = 1
    FAN_MODE = 8
    TURN_OFF = 128
    TURN_ON = 256


class Entity:
    entity_id = "climate.test"
    _attr_has_entity_name = False
    _attr_name = "UNSET"
    _attr_unique_id = None
    _attr_device_info = None
    _attr_translation_key = None
    entity_description = None

    _attr_icon = None
    state_writes = 0

    def async_write_ha_state(self):
        self.state_writes += 1

    @property
    def unique_id(self):
        return self._attr_unique_id

    @property
    def icon(self):
        return self._attr_icon

    @property
    def device_info(self):
        return self._attr_device_info

    @property
    def name(self):
        if self._attr_name != "UNSET":
            return self._attr_name
        d = self.entity_description
        if d is not None:
            return d.translation_key or (d.device_class and str(d.device_class)) or d.key
        return self._attr_translation_key


class ClimateEntity(Entity):
    _attr_supported_features = ClimateEntityFeature(0)
    _attr_hvac_modes: list = []

    @property
    def supported_features(self):
        return self._attr_supported_features

    @property
    def hvac_modes(self):
        return self._attr_hvac_modes


class SensorEntity(Entity):
    pass


class BinarySensorEntity(Entity):
    pass


class Scene(Entity):
    _attr_entity_registry_enabled_default = True


class SwitchEntity(Entity):
    pass


@dataclass(frozen=True, kw_only=True)
class EntityDescription:
    key: str
    translation_key: str | None = None
    device_class: Any = None
    state_class: Any = None
    native_unit_of_measurement: Any = None
    entity_category: Any = None
    options: Any = None


class SensorDeviceClass(StrEnum):
    TEMPERATURE = "temperature"
    DURATION = "duration"
    ENUM = "enum"


class SensorStateClass(StrEnum):
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"


class BinarySensorDeviceClass(StrEnum):
    PROBLEM = "problem"


class EntityCategory(StrEnum):
    DIAGNOSTIC = "diagnostic"
    CONFIG = "config"


class UnitOfTime(StrEnum):
    HOURS = "h"
    MINUTES = "min"


class NumberMode(StrEnum):
    AUTO = "auto"
    SLIDER = "slider"


class NumberEntity(Entity):
    _attr_entity_registry_enabled_default = True


class UnitOfTemperature(StrEnum):
    CELSIUS = "°C"


class Platform(StrEnum):
    BINARY_SENSOR = "binary_sensor"
    CLIMATE = "climate"
    NUMBER = "number"
    SCENE = "scene"
    SENSOR = "sensor"
    SWITCH = "switch"


class DataUpdateCoordinator[T]:
    def __init__(self, hass, logger, *, config_entry=None, name=None, update_interval=None):
        self.hass = hass
        self.config_entry = config_entry
        self.data = None
        self.last_update_success = True

    async def async_refresh(self):
        self.data = await self._async_update_data()

    async def async_request_refresh(self):
        await self.async_refresh()

    listener_updates = 0

    def async_update_listeners(self):
        self.listener_updates += 1

    def async_set_updated_data(self, data):
        self.data = data
        self.last_update_success = True
        self.async_update_listeners()


class CoordinatorEntity[T](Entity):
    def __init__(self, coordinator):
        self.coordinator = coordinator

    @property
    def available(self):
        return self.coordinator.last_update_success


def _messages():
    import json
    import pathlib
    strings = pathlib.Path(__file__).parents[1] / "custom_components/izone_local/strings.json"
    return json.loads(strings.read_text(encoding="utf-8"))["exceptions"]


class HomeAssistantError(Exception):
    """Renders a translated error from strings.json, as Home Assistant does."""

    def __init__(self, *args, translation_domain=None, translation_key=None,
                 translation_placeholders=None):
        if translation_key is not None:
            message = _messages()[translation_key]["message"]
            args = (message.format(**(translation_placeholders or {})),)
        super().__init__(*args)
        self.translation_domain = translation_domain
        self.translation_key = translation_key


class UpdateFailed(HomeAssistantError):
    pass


class ServiceValidationError(HomeAssistantError):
    pass


class ConfigEntry[T]:
    def __init__(self, data):
        self.data = data
        self.options = {}
        self.runtime_data = None
        self.update_listeners = []

    def add_update_listener(self, listener):
        self.update_listeners.append(listener)
        return lambda: self.update_listeners.remove(listener)

    def async_create_background_task(self, hass, coro, name):
        import asyncio
        return asyncio.get_running_loop().create_task(coro, name=name)


mod("homeassistant")
# Notifications shown, by notification_id: (title, message).
notifications: dict[str, tuple[str, str]] = {}


def _create_notification(hass, message, title=None, notification_id=None):
    notifications[notification_id] = (title, message)


def _dismiss_notification(hass, notification_id):
    notifications.pop(notification_id, None)


persistent_notification = mod(
    "homeassistant.components.persistent_notification",
    async_create=_create_notification, async_dismiss=_dismiss_notification)
mod("homeassistant.components", persistent_notification=persistent_notification)
mod("homeassistant.components.climate", HVACMode=HVACMode, ClimateEntity=ClimateEntity,
    ClimateEntityFeature=ClimateEntityFeature, FAN_AUTO="auto", FAN_LOW="low",
    FAN_MEDIUM="medium", FAN_HIGH="high", ATTR_HVAC_MODE="hvac_mode")
mod("homeassistant.components.sensor", SensorEntity=SensorEntity,
    SensorEntityDescription=EntityDescription, SensorDeviceClass=SensorDeviceClass,
    SensorStateClass=SensorStateClass)
mod("homeassistant.components.binary_sensor", BinarySensorEntity=BinarySensorEntity,
    BinarySensorEntityDescription=EntityDescription,
    BinarySensorDeviceClass=BinarySensorDeviceClass)
mod("homeassistant.components.switch", SwitchEntity=SwitchEntity)
mod("homeassistant.components.scene", Scene=Scene)
mod("homeassistant.components.number", NumberEntity=NumberEntity, NumberMode=NumberMode)
mod("homeassistant.const", ATTR_TEMPERATURE="temperature", CONF_HOST="host",
    PERCENTAGE="%", EntityCategory=EntityCategory, UnitOfTemperature=UnitOfTemperature,
    UnitOfTime=UnitOfTime,
    Platform=Platform)
mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
mod("homeassistant.exceptions", HomeAssistantError=HomeAssistantError,
    ServiceValidationError=ServiceValidationError)
mod("homeassistant.config_entries", ConfigEntry=ConfigEntry, ConfigFlow=object, OptionsFlow=object,
    ConfigFlowResult=dict)
mod("homeassistant.helpers")
mod("homeassistant.helpers.config_validation", config_entry_only_config_schema=lambda domain: None)
mod("homeassistant.helpers.typing", ConfigType=dict)
# Without async_register_platform_entity_service, like Home Assistant before
# 2025.10; a test can add it to check the newer path.
mod("homeassistant.helpers.service")
mod("homeassistant.helpers.device_registry", DeviceInfo=dict)
class EntityPlatform:
    """Records the entity services a platform registers."""

    def __init__(self):
        self.services = {}

    def async_register_entity_service(self, name, schema, method):
        self.services[name] = (schema, method)


platform = EntityPlatform()
mod("homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object,
    async_get_current_platform=lambda: platform)
mod("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=DataUpdateCoordinator,
    CoordinatorEntity=CoordinatorEntity, UpdateFailed=UpdateFailed)
mod("homeassistant.helpers.aiohttp_client", async_get_clientsession=None)
