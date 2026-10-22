"""Offline test: editing a favourite with izone_local.update_favourite."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import copy
import importlib
import json

import aiohttp
import ha_stubs
import voluptuous as vol
from aiohttp import web
from ha_stubs import ConfigEntry, HomeAssistantError, ServiceValidationError
from izone_local import api, scene
from izone_local import const as c
from izone_local.api import IZoneClient
from izone_local.coordinator import IZoneCoordinator

api.COMMAND_GAP = 0.01
c.FAVOURITE_EDIT_GAP = 0.05

SYS = {"SysOn": 0, "SysMode": 2, "SysFan": 4, "NoOfZones": 4, "CtrlZone": 15,
       "FanAutoEn": 1, "FanAutoType": 1}  # 3-speed with auto: no top
Z = [
    {"Index": 0, "Name": "Living", "ZoneType": 3, "SensType": 0, "Mode": 2, "Setpoint": 2100},
    {"Index": 1, "Name": "Master", "ZoneType": 3, "SensType": 0, "Mode": 2, "Setpoint": 2300},
    {"Index": 2, "Name": "Hall", "ZoneType": 1, "SensType": 255, "Mode": 2, "Setpoint": 0},
    {"Index": 3, "Name": "Bypass", "ZoneType": 2, "SensType": 255, "Mode": 5, "Setpoint": 0,
     "Bypass": 1},
]
ZONES = [{"Mode": 2, "Setpoint": 2300}, {"Mode": 3, "Setpoint": 2200},
         {"Mode": 1, "Setpoint": 2300}] + [{"Mode": 1, "Setpoint": 2300}] * 11
FAV = {"Name": "Evening", "Mode": 0, "Fan": 0, "Zones": copy.deepcopy(ZONES)}
# "error": reply {ERROR} but keep it; "drop": ignore this many edits; "never".
B = {"error": False, "drop": 0, "never": False}
cmds, times, fails = [], [], []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        fails.append(name)


async def req(r):
    b = (await r.json())["iZoneV2Request"]
    if b["Type"] == 1:
        return web.Response(text=json.dumps({"AirStreamDeviceUId": "1", "SystemV2": SYS}))
    if b["Type"] == 3:
        named = b["No"] == 0
        return web.Response(text=json.dumps(
            {"SchedulesV2": FAV if named else {"Name": "", "Zones": []}}))
    if b["Type"] == 6:
        return web.Response(text="{}")
    return web.Response(text=json.dumps({"ZonesV2": Z[b["No"]]}))


async def cmd(r):
    b = await r.json()
    cmds.append(b)
    times.append(asyncio.get_running_loop().time())
    name, value = next(iter(b.items()))
    if B["never"]:
        return web.Response(text="{OK}")
    if B["drop"]:
        B["drop"] -= 1
        return web.Response(text="{OK}")
    if name == "SchedZones":
        FAV["Zones"] = value["Zones"]
    elif name == "SchedAcFan":
        FAV["Fan"] = value["Fan"]
    elif name == "SchedAcMode":
        FAV["Mode"] = value["Mode"]
    return web.Response(text="{ERROR}" if B["error"] else "{OK}")


async def refused(coro, words):
    try:
        await coro
    except ServiceValidationError as err:
        return words in str(err) or str(err)
    return "not refused"


async def main():
    app = web.Application()
    app.router.add_post("/iZoneRequestV2", req)
    app.router.add_post("/iZoneCommandV2", cmd)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host = f"127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    async with aiohttp.ClientSession() as s:
        entry = ConfigEntry({"host": host, "uid": "1"})
        co = IZoneCoordinator(None, entry, IZoneClient(s, host), "1")
        await co.async_refresh()
        entry.runtime_data = co
        scenes = []
        await scene.async_setup_entry(None, entry, scenes.extend)
        evening = scenes[0]

        schema, method = ha_stubs.platform.services.get("update_favourite", (None, None))
        check("action registered on the scenes", method == "async_update_favourite")
        validate = vol.Schema(schema)
        check("schema: a zone number is taken as text",
              validate({"zone": 2, "temperature": "21.5"}) == {"zone": "2", "temperature": 21.5})
        for bad in ({"temperature": 31}, {"fan": "turbo"}, {"zone_mode": "override"}):
            try:
                validate(bad)
                check(f"schema refuses {bad}", False)
            except vol.Invalid:
                check(f"schema refuses {bad}", True)

        # A zone by name (any case), a temperature only: that zone becomes auto.
        cmds.clear()
        await evening.async_update_favourite(zone="master", temperature=21.5)
        sent = cmds[0].get("SchedZones", {})
        check("one SchedZones for this favourite with all 14 zones",
              len(cmds) == 1 and sent.get("Index") == 0 and len(sent.get("Zones", [])) == 14,
              cmds)
        check("Master stored as auto 21.5, the others as they were",
              sent["Zones"][1] == {"Mode": 3, "Setpoint": 2150}
              and sent["Zones"][:1] + sent["Zones"][2:] == ZONES[:1] + ZONES[2:], sent["Zones"][:3])
        check("the scene shows the new favourite",
              evening.extra_state_attributes["zones"]["Master"] == "auto 21.5"
              and evening.state_writes == 1, evening.extra_state_attributes)

        # By number; fan and mode in the same call; the fan is already unchanged.
        cmds.clear()
        await evening.async_update_favourite(zone="1", zone_mode="open", fan="unchanged",
                                             mode="cool")
        check("only what differs is sent: the zone and the mode",
              [next(iter(x)) for x in cmds] == ["SchedZones", "SchedAcMode"], cmds)
        check("sent 2 s apart (FAVOURITE_EDIT_GAP)",
              times[-1] - times[-2] >= c.FAVOURITE_EDIT_GAP, times[-2:])
        check("stored: Living open, mode cool, fan unchanged",
              FAV["Zones"][0]["Mode"] == 1 and FAV["Mode"] == 1 and FAV["Fan"] == 0)
        check("attributes: mode cool, fan unchanged, Living open",
              evening.extra_state_attributes["mode"] == "cool"
              and evening.extra_state_attributes["fan"] == "unchanged"
              and evening.extra_state_attributes["zones"]["Living"] == "open")

        # The controller answers {ERROR} to an edit it keeps: judged by read-back.
        B["error"] = True
        await evening.async_update_favourite(fan="high")
        check("{ERROR} for an edit that was kept: success", FAV["Fan"] == 3)
        B["error"] = False

        # It drops the first send: sent again.
        cmds.clear()
        B["drop"] = 1
        await evening.async_update_favourite(fan="low")
        check("an edit that didn't stick is sent again", FAV["Fan"] == 1
              and cmds == [{"SchedAcFan": {"Index": 0, "Fan": 1}}] * 2, cmds)

        # It never keeps it.
        cmds.clear()
        B["never"] = True
        try:
            await evening.async_update_favourite(mode="heat")
            check("never kept: a clear error after 3 rounds", False)
        except HomeAssistantError as err:
            check("never kept: a clear error after 3 rounds",
                  "didn't keep the change to mode" in str(err) and len(cmds) == 3,
                  (err, len(cmds)))
        B["never"] = False

        for what, coro, words in (
            ("fan the unit lacks", evening.async_update_favourite(fan="top"),
             "low, medium, high, auto"),
            ("auto on an open/close zone",
             evening.async_update_favourite(zone="Hall", zone_mode="auto"), "only be open or close"),
            ("a temperature on an open/close zone",
             evening.async_update_favourite(zone="Hall", temperature=22), "only be open or close"),
            ("a bypass zone", evening.async_update_favourite(zone="bypass", zone_mode="open"),
             "run by the system"),
            ("an unknown zone", evening.async_update_favourite(zone="Garage", zone_mode="open"),
             "the zones are Living, Master, Hall, Bypass"),
            ("zone settings without a zone", evening.async_update_favourite(zone_mode="open"),
             "need a zone"),
            ("a zone with nothing to set", evening.async_update_favourite(zone="Living"),
             "zone_mode and/or temperature"),
            ("nothing at all", evening.async_update_favourite(), "Nothing to change"),
        ):
            cmds.clear()
            result = await refused(coro, words)
            check(f"refused: {what}", result is True and not cmds, (result, cmds))

    # Home Assistant 2025.10+: registered once when the integration loads.
    registered = []
    service_helper = sys.modules["homeassistant.helpers.service"]
    service_helper.async_register_platform_entity_service = (
        lambda hass, domain, name, **kw: registered.append((domain, name, kw)))
    services = importlib.reload(importlib.import_module("izone_local.services"))
    services.async_setup_services(None)
    check("2025.10+: registered on load for the scenes",
          services.REGISTER_ON_LOAD and len(registered) == 1
          and registered[0][:2] == ("izone_local", "update_favourite")
          and registered[0][2]["entity_domain"] == "scene"
          and registered[0][2]["func"] == "async_update_favourite", registered)
    del service_helper.async_register_platform_entity_service
    await runner.cleanup()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
