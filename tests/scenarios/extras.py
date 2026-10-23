"""Offline test: run hours, control zone, wireless sensors, numbers, areas."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import json
import sys

import aiohttp
import ha_stubs  # noqa: F401
from aiohttp import web
from ha_stubs import ConfigEntry, ServiceValidationError
from izone_local import api, climate, number, sensor
from izone_local import const as c
from izone_local.api import IZoneClient
from izone_local.coordinator import IZoneCoordinator

api.COMMAND_GAP = 0.02
c.CONFIRM_INTERVAL = 0.02

SYS = {"SysOn": 1, "SysMode": 2, "SysFan": 4, "Setpoint": 2200, "Temp": 2200, "Supply": 1900,
       "CtrlZone": 1, "NoOfZones": 3, "AcRunH": 77, "SleepTimer": 0, "SleepTimerM": 0,
       "Tag1": "Ghar\x00��"}
Z = [
    {"Index": 0, "Name": "Living\x00�", "ZoneType": 3, "SensType": 3, "Mode": 3,
     "Setpoint": 2100, "Temp": 2050, "RfSignal": 2, "BattVolt": 1, "MinAir": 0, "MaxAir": 100,
     "DmpPos": 40},
    {"Index": 1, "Name": "Master", "ZoneType": 3, "SensType": 0, "Mode": 3, "Setpoint": 2300,
     "Temp": 2200, "SensorFault": 1, "MinAir": 0, "MaxAir": 100, "DmpPos": 0},
    {"Index": 2, "Name": "Bypass", "ZoneType": 2, "SensType": 255, "Mode": 2, "Setpoint": 0,
     "Temp": 0, "Bypass": 1, "MinAir": 0, "MaxAir": 100, "DmpPos": 0},
]
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
    if "SysSleepTimer" in b:
        SYS["SleepTimer"] = b["SysSleepTimer"]
    for name, field in (("ZoneMinAir", "MinAir"), ("ZoneMaxAir", "MaxAir")):
        if name in b:
            Z[b[name]["Index"]][field] = b[name][field]
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
        sens, nums, clim = [], [], []
        await sensor.async_setup_entry(None, entry, sens.extend)
        await number.async_setup_entry(None, entry, nums.extend)
        await climate.async_setup_entry(None, entry, clim.extend)
        by_key = {(getattr(e, "index", None), e.unique_id.split("_", 1)[1]): e for e in sens}

        check("run hours", by_key[(None, "run_hours")].native_value == 77)
        check("control zone named", by_key[(None, "control_zone")].native_value == "Master")
        SYS["CtrlZone"] = 15
        await co.async_refresh()
        check("control zone: unit", by_key[(None, "control_zone")].native_value == "unit")
        check("wireless zone: battery half, signal quarter",
              by_key[(0, "zone0_sensor_battery")].native_value == "half"
              and by_key[(0, "zone0_sensor_signal")].native_value == "quarter")
        check("wired zone has no battery/signal sensors",
              (1, "zone1_sensor_battery") not in by_key and (1, "zone1_sensor_signal") not in by_key)
        check("faulted sensor: zone temperature unknown",
              by_key[(1, "zone1_temperature")].native_value is None)
        check("damper position is diagnostic",
              by_key[(0, "zone0_damper_position")].entity_description.entity_category == "diagnostic")

        living = clim[1]
        info = living._attr_device_info
        check("zone device: clean name, suggested area",
              info["name"] == "Living" and info["suggested_area"] == "Living", info)
        SYS["SleepTimer"], SYS["SleepTimerM"] = 720, 95
        await co.async_refresh()
        check("climate sleep_timer attribute: the minutes left, not the last value set",
              clim[0].extra_state_attributes["sleep_timer"] == 95,
              clim[0].extra_state_attributes)
        check("AC device named from Tag1 without padding",
              clim[0]._attr_device_info["name"] == "Ghar", clim[0]._attr_device_info["name"])

        check("no sleep-timer setting",
              not any("sleep" in e.unique_id for e in nums), [e.unique_id for e in nums])
        airflow = [e for e in nums if isinstance(e, number.IZoneAirflow)]
        check("airflow numbers: 2 per non-bypass zone, settings, off by default",
              len(airflow) == 4 and all(e._attr_entity_category == "config"
                                        and not e._attr_entity_registry_enabled_default
                                        for e in airflow), len(airflow))
        living_min = next(e for e in airflow if e.index == 0 and e._field == "MinAir")
        living_max = next(e for e in airflow if e.index == 0 and e._field == "MaxAir")
        cmds.clear()
        await living_max.async_set_native_value(80)
        await living_min.async_set_native_value(20)
        check("airflow set", cmds == [{"ZoneMaxAir": {"Index": 0, "MaxAir": 80}},
                                      {"ZoneMinAir": {"Index": 0, "MinAir": 20}}], cmds)
        try:
            await living_min.async_set_native_value(90)
            check("min above max refused", False)
        except ServiceValidationError:
            check("min above max refused", True)
    await runner.cleanup()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
