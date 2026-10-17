"""Live: reconcile loop timing and statuses on the real bridge. Restores state."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import sys
import time

import ha_stubs  # noqa: F401
import aiohttp

from izone_local import climate  # noqa: E402
from izone_local.api import IZoneClient  # noqa: E402
from izone_local.coordinator import IZoneCoordinator  # noqa: E402
from ha_stubs import ConfigEntry  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{detail}]"))
    if not cond:
        fails.append(name)


async def timed(label, coro, ac, zone, watch):
    """Run an action, logging when each status first appeared."""
    t0 = time.perf_counter()
    task = asyncio.create_task(coro)
    await asyncio.sleep(0)
    first = {"AC": str(ac.hvac_mode), **{n: str(e.hvac_mode) for n, e in zone.items()}}
    log = [(0.0, watch.status, watch.icon)]
    while not task.done():
        if watch.status != log[-1][1]:
            log.append((time.perf_counter() - t0, watch.status, watch.icon))
        await asyncio.sleep(0.01)
    await task
    total = time.perf_counter() - t0
    print(f"\n{label}: shown at once as {first}")
    for t, status, icon in log:
        print(f"   {t:5.2f} s  {status:<18} {icon}")
    print(f"   {total:5.2f} s  done ({watch.status}, icon {watch.icon})")
    return first, log, total


async def main():
    host = sys.argv[1]
    async with aiohttp.ClientSession() as s:
        client = IZoneClient(s, host)
        uid, _ = await client.get_system()
        entry = ConfigEntry({"host": host, "uid": uid})
        co = IZoneCoordinator(None, entry, client, uid)
        await co.async_refresh()
        entry.runtime_data = co
        ents = []
        await climate.async_setup_entry(None, entry, ents.extend)
        ac = ents[0]
        zone = {co.data.zones[e.index]["Name"]: e for e in ents[1:]}
        orig_sys = {k: co.data.system[k] for k in ("SysOn", "SysMode", "SysFan")}
        orig_z = {i: (z["Mode"], z["Setpoint"]) for i, z in co.data.zones.items()}
        print("original:", orig_sys, {co.data.zones[i]["Name"]: v for i, v in orig_z.items()})
        try:
            if co.data.system["SysOn"]:
                await ac.async_turn_off()

            first, log, total = await timed(
                "Master on (AC off)", zone["Master"].async_turn_on(), ac, zone, zone["Master"])
            check("Master on: shown at once as auto with the AC on",
                  first["Master"] == "auto" and first["AC"] != "off", first)
            check("Master on: the bridge confirms, status ready",
                  co.data.system["SysOn"] == 1 and zone["Master"].status == "ready"
                  and not co._wants, (co._wants, zone["Master"].status))

            first, log, total = await timed(
                "Upstairs on (AC on)", zone["Upstairs"].async_turn_on(), ac, zone, zone["Upstairs"])
            check("Upstairs on: confirmed", str(zone["Upstairs"].hvac_mode) == "auto"
                  and zone["Upstairs"].status == "ready", zone["Upstairs"].status)

            first, log, total = await timed(
                "Master off (Upstairs still on)", zone["Master"].async_turn_off(), ac, zone,
                zone["Master"])
            check("Master off: the AC stays on", co.data.system["SysOn"] == 1, co.data.system["SysOn"])

            first, log, total = await timed(
                "Upstairs off (last zone)", zone["Upstairs"].async_turn_off(), ac, zone,
                zone["Upstairs"])
            check("Last zone off: shown at once with the AC off", first["AC"] == "off", first)
            check("Last zone off: the bridge confirms the AC off",
                  co.data.system["SysOn"] == 0 and ac.status == "ready", ac.status)
        finally:
            print("\nrestoring...")
            await asyncio.sleep(1)
            await co.async_refresh()
            for i, (mode, sp) in orig_z.items():
                z = co.data.zones[i]
                if z.get("Bypass"):
                    continue
                if mode in (3, 4) and z["Setpoint"] != sp:
                    if z["Mode"] not in (3, 4):
                        await client.set_zone_mode(i, 3)
                    await client.set_zone_setpoint(i, sp / 100)
            await asyncio.sleep(2)
            await co.async_refresh()
            for i, (mode, _) in orig_z.items():
                z = co.data.zones[i]
                if not z.get("Bypass") and z["Mode"] != mode and mode in (1, 2, 3):
                    await client.set_zone_mode(i, mode)
            if co.data.system["SysOn"] != orig_sys["SysOn"]:
                await client.set_power(bool(orig_sys["SysOn"]))
            await asyncio.sleep(3)
            await co.async_refresh()
            now_sys = {k: co.data.system[k] for k in orig_sys}
            now_z = {i: (z["Mode"], z["Setpoint"]) for i, z in co.data.zones.items()}
            check("restored", now_sys == orig_sys and now_z == orig_z, f"{now_sys} {now_z}")
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")


asyncio.run(main())
