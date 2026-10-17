"""Live: Theatre/Gst airflow through the number entities; nothing else moves."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import sys

import ha_stubs  # noqa: F401
import aiohttp

from izone_local import number  # noqa: E402
from izone_local.api import IZoneClient  # noqa: E402
from izone_local.coordinator import IZoneCoordinator  # noqa: E402
from ha_stubs import ConfigEntry  # noqa: E402
from live.bridge_state import snap  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + f"  [{detail}]")
    if not cond:
        fails.append(name)


async def main():
    host = sys.argv[1]
    async with aiohttp.ClientSession() as s:
        client = IZoneClient(s, host)
        before = await snap(client)
        uid, _ = await client.get_system()
        entry = ConfigEntry({"host": host, "uid": uid})
        co = IZoneCoordinator(None, entry, client, uid)
        await co.async_refresh()
        entry.runtime_data = co
        nums = []
        await number.async_setup_entry(None, entry, nums.extend)
        zmin = next(e for e in nums if e.index == 1 and e._field == "MinAir")
        zmax = next(e for e in nums if e.index == 1 and e._field == "MaxAir")
        orig = (zmin.native_value, zmax.native_value)
        print("Theatre/Gst min/max:", orig)
        try:
            await zmax.async_set_native_value(90)
            await zmin.async_set_native_value(10)
            check("set to 10/90", (zmin.native_value, zmax.native_value) == (10, 90),
                  (zmin.native_value, zmax.native_value))
            check("AC and zone modes unchanged", (await snap(client)) == before,
                  await snap(client))
        finally:
            await zmin.async_set_native_value(orig[0])
            await zmax.async_set_native_value(orig[1])
            await asyncio.sleep(10)
            await co.async_refresh()
            check("restored", (zmin.native_value, zmax.native_value) == orig
                  and (await snap(client)) == before, (zmin.native_value, zmax.native_value))
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")


asyncio.run(main())
