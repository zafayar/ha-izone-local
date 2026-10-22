"""Offline test: notifications for the filter warning and firmware changes."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import json

import aiohttp
import ha_stubs
from aiohttp import web
from ha_stubs import ConfigEntry, notifications
from izone_local.api import IZoneClient, describe_firmware_changes
from izone_local.coordinator import IZoneCoordinator

SYS = {"SysOn": 1, "SysMode": 1, "SysFan": 1, "NoOfZones": 0, "CtrlZone": 15,
       "Warnings": "none", "FilterWarn": 3}
B = {"fmw": "COCB-2F5 4.36 (000000042),CR 3.23"}
FILTER, FIRMWARE = "izone_local_1_filter", "izone_local_1_firmware"
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        fails.append(name)


async def req(r):
    b = (await r.json())["iZoneV2Request"]
    if b["Type"] == 6:
        return web.Response(text=json.dumps({"Fmw": B["fmw"]}))
    return web.Response(text=json.dumps({"AirStreamDeviceUId": "1", "SystemV2": SYS}))


async def main():
    check("firmware changes described",
          describe_firmware_changes({"CR": "3.23", "C225": "3.80", "X": "1"},
                                    {"CR": "3.25", "C225": "3.80", "Y": "2"})
          == ["CR: 3.23 -> 3.25", "X: no longer reported (was 1)", "Y: new, 2"])

    app = web.Application()
    app.router.add_post("/iZoneRequestV2", req)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host = f"127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    async with aiohttp.ClientSession() as s:
        entry = ConfigEntry({"host": host, "uid": "1"})
        co = IZoneCoordinator(None, entry, IZoneClient(s, host), "1")
        entry.runtime_data = co
        await co.async_refresh()
        check("nothing to say: no notifications", notifications == {}, notifications)

        SYS["Warnings"] = "filter"
        await co.async_refresh()
        title, message = notifications.get(FILTER, ("", ""))
        check("filter warning: notified", "filter" in title and "every 3 months" in message,
              notifications)

        notifications.pop(FILTER)  # the user dismisses it
        await co.async_refresh()
        check("a dismissed notification isn't raised again every poll", FILTER not in notifications)

        SYS["Warnings"] = "none"
        await co.async_refresh()
        SYS["Warnings"] = "filter"
        await co.async_refresh()
        check("a new warning is notified again", FILTER in notifications)
        SYS["Warnings"] = "none"
        await co.async_refresh()
        check("the notification clears when the warning goes", FILTER not in notifications)

        B["fmw"] = "COCB-2F5 4.39 (000000042),CR 3.23"
        co._firmware_due = 0
        await co.async_refresh()
        title, message = notifications.get(FIRMWARE, ("", ""))
        check("firmware change: notified with what changed",
              "firmware" in title and message == "- COCB-2F5: 4.36 -> 4.39", notifications)
        check("the serial number isn't in it", "000000042" not in message)

        # Turned off in the options.
        entry.options = {"notifications": False}
        co.dismiss_notifications()
        check("turning them off clears them", notifications == {}, notifications)
        SYS["Warnings"] = "filter"
        B["fmw"] = "COCB-2F5 4.40"
        co._firmware_due = 0
        await co.async_refresh()
        check("while off: none raised", notifications == {}, notifications)
        check("but the firmware is still read", co.hub_firmware == "4.40")

        # Turning them back on reloads the entry: a fresh coordinator.
        entry.options = {"notifications": True}
        co = IZoneCoordinator(None, entry, IZoneClient(s, host), "1")
        await co.async_refresh()
        check("back on: a warning that's still on is shown", FILTER in notifications)
        check("but the first read of the firmware isn't a change", FIRMWARE not in notifications)
    await runner.cleanup()
    ha_stubs.notifications.clear()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
