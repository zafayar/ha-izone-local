"""Offline test: favourites as scenes, with follow-through and fault fallback."""

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

from izone_local import api, const as c, scene  # noqa: E402
from izone_local.api import IZoneClient  # noqa: E402
from izone_local.coordinator import IZoneCoordinator  # noqa: E402
from ha_stubs import ConfigEntry  # noqa: E402

api.COMMAND_GAP = 0.05
c.CONFIRM_INTERVAL = 0.03
c.RESEND_AFTER = 0.4
c.FAVOURITE_SETTLE = 0.6

SYS = {"SysOn": 0, "SysMode": 2, "SysFan": 4, "Setpoint": 2200, "Temp": 2200,
       "CtrlZone": 15, "NoOfZones": 4, "OemMake": 8, "AcUnitBrand": 26, "Tag1": "Ghar"}
Z = [
    {"Index": 0, "Name": "Living", "ZoneType": 3, "SensType": 0, "Mode": 3, "Setpoint": 2100},
    {"Index": 1, "Name": "Master", "ZoneType": 3, "SensType": 0, "Mode": 2, "Setpoint": 2300},
    {"Index": 2, "Name": "Upstairs", "ZoneType": 3, "SensType": 0, "Mode": 2, "Setpoint": 2200},
    {"Index": 3, "Name": "Bypass", "ZoneType": 2, "SensType": 255, "Mode": 2, "Setpoint": 2300,
     "Bypass": 1, "SensorFault": 1},
]


def fav(name, mode, fan, zones):
    zones = zones + [(1, 2300)] * (14 - len(zones))
    return {"Name": name + "\x00\xff", "Mode": mode, "Fan": fan,
            "Zones": [{"Mode": m, "Setpoint": s} for m, s in zones]}


FAVS = [fav("Favourite 1", 2, 2, [(1, 2300)] * 4),
        fav("Get Ready", 0, 0, [(2, 2300), (3, 2200), (3, 2400), (2, 2300)])]
FAVS += [{"Name": "", "Zones": []}] * 7
cmds, fails = [], []
T = {"fav": None}
B = {"fault": set(), "lazy": {0}}


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        fails.append(name)


async def req(r):
    b = (await r.json())["iZoneV2Request"]
    if b["Type"] == 1:
        return web.Response(text=json.dumps({"AirStreamDeviceUId": "1", "SystemV2": SYS}))
    if b["Type"] == 3:
        return web.Response(text=json.dumps({"SchedulesV2": FAVS[b["No"]]}))
    zone = Z[b["No"]]
    if b["No"] in B["fault"]:
        zone = {**zone, "Mode": 2, "SensorFault": 1}
    return web.Response(text=json.dumps({"AirStreamDeviceUId": "1", "ZonesV2": zone}))


async def cmd(r):
    b = await r.json()
    now = asyncio.get_running_loop().time()
    cmds.append(b)
    T.setdefault("times", []).append(now)
    if "FavouriteSet" in b:
        T["fav"] = now
        f = FAVS[b["FavouriteSet"] - 1]
        if any(f["Zones"][i]["Mode"] == 3 for i in B["fault"]):
            return web.Response(text="{OK}")  # refuses it as a whole, silently
        SYS["SysOn"] = 1
        for i, z in enumerate(Z):
            t = f["Zones"][i]
            if z.get("Bypass") or i in B["lazy"]:
                continue  # like the real one: some zones lag behind
            z["Mode"] = t["Mode"]
            if t["Mode"] == 3:
                z["Setpoint"] = t["Setpoint"]
    if "SysOn" in b:
        SYS["SysOn"] = b["SysOn"]
    if "ZoneMode" in b:
        Z[b["ZoneMode"]["Index"]]["Mode"] = b["ZoneMode"]["Mode"]
    if "ZoneSetpoint" in b:
        z = Z[b["ZoneSetpoint"]["Index"]]
        if z["Mode"] == 3:
            z["Setpoint"] = b["ZoneSetpoint"]["Setpoint"]
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
        scenes = []
        await scene.async_setup_entry(None, entry, scenes.extend)
        names = {e._attr_name: e for e in scenes}
        check("one scene per named favourite; padding stripped",
              list(names) == ["Favourite 1", "Get Ready"], list(names))
        check("default-named slot disabled, custom one enabled",
              not names["Favourite 1"]._attr_entity_registry_enabled_default
              and names["Get Ready"]._attr_entity_registry_enabled_default)
        attrs = names["Get Ready"].extra_state_attributes
        check("attributes describe the favourite", attrs == {
            "favourite_number": 2, "mode": "unchanged", "fan": "unchanged",
            "zones": {"Living": "close", "Master": "auto 22.0", "Upstairs": "auto 24.0"}},
            attrs)

        async def settled(limit=5):
            for _ in range(int(limit / 0.02)):
                if not any(not w.waiting_for_sensor for w in co._wants.values()):
                    return True
                await asyncio.sleep(0.02)
            return False

        # Activate: one FavouriteSet; the lagging Living zone is followed up.
        cmds.clear()
        T["times"] = []
        t0 = asyncio.get_running_loop().time()
        await names["Get Ready"].async_activate()
        check("activation returns once the favourite is accepted",
              asyncio.get_running_loop().time() - t0 < 0.3 and cmds == [{"FavouriteSet": 2}],
              cmds)
        check("follow-through finishes in the background", await settled())
        check("FavouriteSet sent, then only the zone it missed",
              cmds == [{"FavouriteSet": 2}, {"ZoneMode": {"Index": 0, "Mode": 2}}], cmds)
        check("the follow-up waited for the favourite to settle",
              len(T["times"]) == 2 and T["times"][1] - T["fav"] >= 0.6, T["times"])
        check("result: AC on, Living closed, Master 22, Upstairs 24",
              SYS["SysOn"] == 1 and [(z["Mode"], z["Setpoint"]) for z in Z[:3]]
              == [(2, 2100), (3, 2200), (3, 2400)], (SYS["SysOn"], Z[:3]))
        check("nothing left pending", not co._wants, co._wants)

        # A favourite needing a zone whose sensor is out: applied zone by zone.
        for z, m in zip(Z[:3], (3, 2, 2), strict=True):
            z["Mode"] = m
        SYS["SysOn"] = 0
        B["fault"], B["lazy"] = {1}, set()
        await co.async_refresh()
        cmds.clear()
        await names["Get Ready"].async_activate()
        await settled()
        check("faulted Auto zone: no FavouriteSet, other zones sent directly",
              {"FavouriteSet": 2} not in cmds
              and {"ZoneMode": {"Index": 0, "Mode": 2}} in cmds
              and {"ZoneMode": {"Index": 2, "Mode": 3}} in cmds
              and not any(x.get("ZoneMode", {}).get("Index") == 1 for x in cmds), cmds)
        check("faulted zone waits for its sensor",
              co._wants.get((1, "Mode")) and co._wants[(1, "Mode")].waiting_for_sensor,
              co._wants)
    await runner.cleanup()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
