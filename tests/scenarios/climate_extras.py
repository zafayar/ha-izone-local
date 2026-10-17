"""Offline test: eco limits, a mode bundled with a temperature, clear errors."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import json
import sys

import ha_stubs  # noqa: F401
import aiohttp
from aiohttp import web

from izone_local import api, climate, const as c  # noqa: E402
from izone_local.api import IZoneClient  # noqa: E402
from izone_local.coordinator import IZoneCoordinator  # noqa: E402
from ha_stubs import ConfigEntry, HVACMode, ServiceValidationError  # noqa: E402

api.COMMAND_GAP = 0.02
c.CONFIRM_INTERVAL = 0.02

SYS = {"SysOn": 0, "SysMode": 2, "SysFan": 6, "Setpoint": 2200, "Temp": 2200,
       "CtrlZone": 15, "NoOfZones": 2, "EcoLock": 1, "EcoMin": 1800, "EcoMax": 2600}
Z = [{"Index": 0, "Name": "Living", "ZoneType": 3, "SensType": 0, "Mode": 2, "Setpoint": 2100},
     {"Index": 1, "Name": "Hall", "ZoneType": 1, "SensType": 255, "Mode": 2, "Setpoint": 0}]
cmds, fails = [], []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        fails.append(name)


async def req(r):
    b = (await r.json())["iZoneV2Request"]
    if b["Type"] == 1:
        return web.Response(text=json.dumps({"SystemV2": SYS}))
    return web.Response(text=json.dumps({"ZonesV2": Z[b["No"]]}))


async def cmd(r):
    b = await r.json()
    cmds.append(b)
    for k in ("SysOn", "SysMode", "SysFan"):
        if k in b:
            SYS[k] = b[k]
    if "SysSetpoint" in b:
        SYS["Setpoint"] = b["SysSetpoint"]
    if "ZoneMode" in b:
        Z[b["ZoneMode"]["Index"]]["Mode"] = b["ZoneMode"]["Mode"]
    if "ZoneSetpoint" in b and Z[b["ZoneSetpoint"]["Index"]]["Mode"] == 3:
        Z[b["ZoneSetpoint"]["Index"]]["Setpoint"] = b["ZoneSetpoint"]["Setpoint"]
    return web.Response(text="{OK}")


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
        ents = []
        await climate.async_setup_entry(None, entry, ents.extend)
        ac, living, hall = ents

        check("eco lock: range 18-26", (ac.min_temp, ac.max_temp) == (18.0, 26.0)
              and (living.min_temp, living.max_temp) == (18.0, 26.0),
              (ac.min_temp, ac.max_temp))
        check("reported Quiet fan is named, not offered",
              ac.fan_mode == "quiet" and "quiet" not in ac._attr_fan_modes, ac.fan_mode)

        cmds.clear()
        await ac.async_set_temperature(temperature=30, hvac_mode=HVACMode.COOL)
        check("AC: mode + temperature in one request, temperature clamped to 26",
              cmds == [{"SysMode": 1}, {"SysSetpoint": 2600}, {"SysOn": 1}]
              or sorted(map(str, cmds)) == sorted(map(str, [{"SysMode": 1}, {"SysOn": 1},
                                                             {"SysSetpoint": 2600}])), cmds)
        check("AC ends cool at 26", str(ac.hvac_mode) == "cool" and ac.target_temperature == 26.0,
              (ac.hvac_mode, ac.target_temperature))

        cmds.clear()
        await living.async_set_temperature(temperature=15, hvac_mode=HVACMode.AUTO)
        check("zone: auto + temperature clamped to 18",
              {"ZoneSetpoint": {"Index": 0, "Setpoint": 1800}} in cmds
              and living.target_temperature == 18.0, cmds)

        cmds.clear()
        await living.async_set_temperature(temperature=21, hvac_mode=HVACMode.OFF)
        check("zone: off with a temperature closes it and skips the temperature",
              not any("ZoneSetpoint" in x for x in cmds)
              and {"ZoneMode": {"Index": 0, "Mode": 2}} in cmds, cmds)

        for what, coro in (("set_hvac_mode heat", living.async_set_hvac_mode(HVACMode.HEAT)),
                           ("temperature with heat",
                            living.async_set_temperature(temperature=21, hvac_mode=HVACMode.HEAT)),
                           ("open/close zone set to auto",
                            hall.async_set_hvac_mode(HVACMode.AUTO))):
            try:
                await coro
                check(f"{what} raises a clear error", False)
            except ServiceValidationError as err:
                check(f"{what} raises a clear error", "set on the AC" in str(err), str(err))

        SYS["EcoLock"] = 0
        await co.async_refresh()
        check("eco lock off: full 15-30 range", (ac.min_temp, ac.max_temp) == (15.0, 30.0))
    await runner.cleanup()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
