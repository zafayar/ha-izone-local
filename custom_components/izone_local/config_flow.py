"""Config flow: discover bridges on the LAN, or enter an IP address."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    DiscoveredBridge,
    IZoneClient,
    IZoneError,
    async_discover,
    clean_name,
)
from .const import CONF_UID, DOMAIN, brand_name

_LOGGER = logging.getLogger(__name__)

MANUAL = "manual"


class IZoneLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, DiscoveredBridge] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        try:
            bridges = await async_discover()
        except OSError as err:
            _LOGGER.debug("Discovery failed: %s", err)
            bridges = []
        configured = self._async_current_ids()
        self._discovered = {
            b.uid: b
            for b in bridges
            if b.uid not in configured and "iZoneV2" in b.capabilities
        }
        if not self._discovered:
            return await self.async_step_manual()
        return await self.async_step_pick()

    async def async_step_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            choice = user_input["bridge"]
            if choice == MANUAL:
                return await self.async_step_manual()
            return await self._async_create(self._discovered[choice].host)

        options = {
            uid: f"iZone {uid} ({b.host})" for uid, b in self._discovered.items()
        }
        options[MANUAL] = "Enter an IP address"
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema({vol.Required("bridge"): vol.In(options)}),
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_create(user_input[CONF_HOST], errors)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
            errors=errors,
        )

    async def _async_create(
        self, host: str, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult | None:
        client = IZoneClient(async_get_clientsession(self.hass), host)
        try:
            uid, system = await client.get_system()
        except IZoneError as err:
            _LOGGER.debug("Cannot connect to %s: %s", host, err)
            if errors is None:
                return self.async_abort(reason="cannot_connect")
            errors["base"] = "cannot_connect"
            return None
        if not uid:
            uid = host
        await self.async_set_unique_id(uid)
        # If the bridge moved to a new DHCP address, update the existing entry.
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        # e.g. "Rinnai Ghar", or "Rinnai 000012345" if the system has no Tag1.
        tag = clean_name(system.get("Tag1"))
        return self.async_create_entry(
            title=f"{brand_name(system)} {tag or uid}",
            data={CONF_HOST: host, CONF_UID: uid},
        )
