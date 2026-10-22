"""Live: activate the 'Get Ready' scene, follow it through, check, restore."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import sys
import time

import aiohttp
import ha_stubs  # noqa: F401
from ha_stubs import ConfigEntry
from izone_local import climate, scene
from izone_local.api import IZoneClient
from izone_local.coordinator import IZoneCoordinator

from live.bridge_state import restore, snap

T0 = time.perf_counter()


def t():
    return f"{time.perf_counter() - T0:6.2f}s"


async def main():
    global T0
    host = sys.argv[1]
    async with aiohttp.ClientSession() as s:
        client = IZoneClient(s, host)
        real_post = client._post_once

        async def post(url, payload):
            text = await real_post(url, payload)
            if "Command" in url:
                print(f"{t()} CMD {payload} -> {text!r}")
            return text

        client._post_once = post
        orig = await snap(client)
        uid, _ = await client.get_system()
        entry = ConfigEntry({"host": host, "uid": uid})
        co = IZoneCoordinator(None, entry, client, uid)
        await co.async_refresh()
        entry.runtime_data = co
        scenes, climates = [], []
        await scene.async_setup_entry(None, entry, scenes.extend)
        await climate.async_setup_entry(None, entry, climates.extend)
        master = next(e for e in climates[1:] if e.zone.get("Name") == "Master")
        ready = next(e for e in scenes if e._attr_name == "Get Ready")
        T0 = time.perf_counter()
        ok = False
        try:
            await ready.async_activate()
            print(f"{t()} activate returned; Master shows {master.hvac_mode}, status {master.status!r}")
            last = None
            while co._wants and time.perf_counter() - T0 < 120:
                cur = (co.data.system["SysOn"],
                       [(z["Mode"], z["Setpoint"]) for z in co.data.zones.values()][:4],
                       master.status)
                if cur != last:
                    print(f"{t()} STATE {cur}")
                    last = cur
                await asyncio.sleep(0.2)
            now = await snap(client)
            got = [z[:2] for z in now[1][:4]]
            ok = (now[0][0] == 1 and got[0][0] == 2 and got[1][0] == 2
                  and got[2] == (3, 2200) and got[3] == (3, 2400) and not co._wants)
            print(f"{t()} done: AC {now[0]}, zones {got} ->", "PASS" if ok else "FAIL")
        finally:
            ok2, _ = await restore(client, orig)
            await asyncio.sleep(45)
            later = await snap(client)
            print("restored:", ok2, "| 45 s later:", "OK" if later == orig else f"DIFFERS {later}")


asyncio.run(main())
