"""Offline test: the config flow, discovery, reconfigure and options."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import ha_stubs
from aiohttp import web
from ha_stubs import AbortFlow, ConfigEntry, ConfigFlow
from izone_local import config_flow, discovery
from izone_local.api import DiscoveredBridge, IZoneClient
from izone_local.coordinator import IZoneCoordinator

SYS = {"SysOn": 0, "SysMode": 1, "SysFan": 1, "NoOfZones": 1, "OemMake": 8,
       "Tag1": "Ghar\x00\xff", "CtrlZone": 15}
ZONE = {"Index": 0, "Name": "Living", "Mode": 2}
B = {"uid": "000012345", "down": False}
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        fails.append(name)


async def req(r):
    if B["down"]:
        return web.Response(status=500)
    b = (await r.json())["iZoneV2Request"]
    if b["Type"] == 1:
        return web.Response(text=json.dumps({"AirStreamDeviceUId": B["uid"], "SystemV2": SYS}))
    if b["Type"] == 2:
        return web.Response(text=json.dumps({"ZonesV2": ZONE}))
    return web.Response(text="{}")


async def step(coro):
    """Run a flow step the way Home Assistant does: AbortFlow -> abort."""
    try:
        return await coro
    except AbortFlow as err:
        return {"type": "abort", "reason": err.reason}


def new_flow(source="user"):
    flow = config_flow.IZoneLocalConfigFlow()
    flow.context = {"source": source}
    return flow


async def main():
    app = web.Application()
    app.router.add_post("/iZoneRequestV2", req)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host = f"127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    found = []

    async def fake_discover(*args, **kwargs):
        if B.get("no_network"):
            raise OSError("no network")
        return list(found)

    config_flow.async_discover = fake_discover
    discovery.async_discover = fake_discover
    async with aiohttp.ClientSession() as s:
        config_flow.async_get_clientsession = lambda hass: s
        ConfigFlow.entries = []

        # Nothing found: straight to the address form; errors, then success.
        found[:] = [DiscoveredBridge("999", "10.0.0.9", ["iZoneV1"])]  # v1 only: ignored
        result = await step(new_flow().async_step_user())
        check("nothing v2 found: asks for the address", result["step_id"] == "manual", result)
        B["down"] = True
        result = await step(new_flow().async_step_manual({"host": host}))
        check("unreachable address: cannot_connect, form shown again",
              result["type"] == "form" and result["errors"] == {"base": "cannot_connect"}, result)
        B["down"] = False
        flow = new_flow()
        result = await step(flow.async_step_manual({"host": host}))
        check("created: brand plus the controller's name, host and uid stored",
              result["type"] == "create_entry" and result["title"] == "Rinnai Ghar"
              and result["data"] == {"host": host, "uid": "000012345"}
              and flow.unique_id == "000012345", result)

        ConfigFlow.entries = [ConfigEntry({"host": "10.0.0.1", "uid": "000012345"},
                                          unique_id="000012345")]
        result = await step(new_flow().async_step_manual({"host": host}))
        check("same bridge again: aborts, and its address is updated",
              result == {"type": "abort", "reason": "already_configured"}
              and ConfigFlow.entries[0].data["host"] == host, result)

        # Found by the broadcast: pick it, or choose to type an address.
        ConfigFlow.entries = []
        found[:] = [DiscoveredBridge("000012345", host, ["iZoneV2"])]
        flow = new_flow()
        result = await step(flow.async_step_user())
        check("v2 bridge found: offered in a list", result["step_id"] == "pick", result)
        result = await step(flow.async_step_pick({"bridge": "manual"}))
        check("pick: 'Enter an IP address' goes to the form", result["step_id"] == "manual")
        flow = new_flow()
        await step(flow.async_step_user())
        result = await step(flow.async_step_pick({"bridge": "000012345"}))
        check("pick: the found bridge is created", result["type"] == "create_entry", result)
        B["down"] = True
        flow = new_flow()
        await step(flow.async_step_user())
        result = await step(flow.async_step_pick({"bridge": "000012345"}))
        check("pick: unreachable bridge aborts with cannot_connect",
              result == {"type": "abort", "reason": "cannot_connect"}, result)
        B["down"] = False
        B["no_network"] = True
        result = await step(new_flow().async_step_user())
        check("broadcast fails: asks for the address", result["step_id"] == "manual", result)
        B["no_network"] = False

        # Integration discovery (discovery.py), new bridge: confirm, then create.
        flow = new_flow("integration_discovery")
        result = await step(flow.async_step_integration_discovery(
            {"host": host, "uid": "000012345"}))
        check("discovered: asks to confirm, showing its address",
              result["step_id"] == "discovery_confirm"
              and result["description_placeholders"] == {"host": host}
              and flow.context.get("confirm_only")
              and flow.context["title_placeholders"] == {"host": host}, result)
        result = await step(flow.async_step_discovery_confirm({}))
        check("confirmed: created", result["type"] == "create_entry", result)

        # Known bridge at a new address: the entry gets the new address.
        ConfigFlow.entries = [ConfigEntry({"host": "10.0.0.1", "uid": "000012345"},
                                          unique_id="000012345")]
        result = await step(new_flow("integration_discovery").async_step_integration_discovery(
            {"host": "10.0.0.2", "uid": "000012345"}))
        check("discovered at a new address: entry updated, flow aborted",
              result["reason"] == "already_configured"
              and ConfigFlow.entries[0].data["host"] == "10.0.0.2", result)

        # Reconfigure: same bridge at a new address; another bridge; unreachable.
        entry = ConfigEntry({"host": "10.0.0.1", "uid": "000012345"}, unique_id="000012345")
        flow = new_flow("reconfigure")
        flow.reconfigure_entry = entry
        result = await step(flow.async_step_reconfigure())
        check("reconfigure: form suggests the current address",
              result["step_id"] == "reconfigure"
              and flow.suggested_values == {"host": "10.0.0.1"}, result)
        result = await step(flow.async_step_reconfigure({"host": host}))
        check("reconfigure: same bridge, address updated",
              result["reason"] == "reconfigure_successful" and entry.data["host"] == host, result)
        B["uid"] = "000099999"
        result = await step(flow.async_step_reconfigure({"host": host}))
        check("reconfigure: a different bridge is refused", result["reason"] == "wrong_bridge",
              result)
        B["uid"], B["down"] = "000012345", True
        result = await step(flow.async_step_reconfigure({"host": host}))
        check("reconfigure: unreachable, form again with the typed address",
              result["errors"] == {"base": "cannot_connect"}
              and flow.suggested_values == {"host": host}, result)
        B["down"] = False

        # Options.
        options = config_flow.IZoneLocalConfigFlow.async_get_options_flow(entry)
        options.config_entry = entry
        result = await step(options.async_step_init())
        schema = result["data_schema"]
        check("options: notifications, on by default", schema({}) == {"notifications": True},
              schema({}))
        result = await step(options.async_step_init({"notifications": False}))
        check("options: saved", result == {"type": "create_entry", "title": None,
                                            "data": {"notifications": False}}, result)

        # discovery.py: a scan at start-up and on a timer; flows for v2 bridges.
        found[:] = [DiscoveredBridge("000012345", "10.0.0.2", ["iZoneV2"]),
                    DiscoveredBridge("777", "10.0.0.7", ["iZoneV1"])]
        discovery.async_start_discovery(None)
        check("discovery scheduled: at start-up and every 30 minutes",
              len(ha_stubs.at_started) == 1 and len(ha_stubs.timers) == 1
              and ha_stubs.timers[0][1].total_seconds() == 1800)
        await ha_stubs.at_started[0](None)
        await ha_stubs.timers[0][0](None)
        check("each scan starts a flow for the v2 bridge only",
              ha_stubs.created_flows == [("izone_local", {"source": "integration_discovery"},
                                          {"host": "10.0.0.2", "uid": "000012345"})] * 2,
              ha_stubs.created_flows)
        B["no_network"] = True
        await discovery.async_scan(None)
        check("a failed broadcast is skipped quietly", len(ha_stubs.created_flows) == 2)

        # A zone added after setup: the entry reloads to create its entities.
        reloads = []
        hass = SimpleNamespace(config_entries=SimpleNamespace(
            async_schedule_reload=reloads.append))
        co = IZoneCoordinator(hass, entry, IZoneClient(s, host), "000012345")
        await co.async_refresh()
        SYS["NoOfZones"] = 2
        await co.async_refresh()
        check("zones changed after setup: the entry is reloaded", reloads == ["entry1"], reloads)
    await runner.cleanup()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
