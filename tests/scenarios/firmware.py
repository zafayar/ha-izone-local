"""Offline test: firmware versions from the hub's request Type 6."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import json
import time
import types

import aiohttp
import ha_stubs  # noqa: F401
from aiohttp import web
from ha_stubs import ConfigEntry
from izone_local import api, sensor
from izone_local import const as c
from izone_local import coordinator as coordinator_module
from izone_local.api import IZoneClient, parse_firmware
from izone_local.coordinator import IZoneCoordinator

api.CONNECT_RETRY_DELAYS = (0.01, 0.01)

SYS = {"SysOn": 1, "SysMode": 1, "SysFan": 1, "NoOfZones": 0, "CtrlZone": 15}
# A made-up hub serial number: it must never reach entities or diagnostics.
FMW = "COCB-2F5 4.36 (000000042),C225-1O9 3.80,CCTSL-4E9 2.38"
B = {"fmw": FMW, "type6": 0}
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        fails.append(name)


async def req(r):
    b = (await r.json())["iZoneV2Request"]
    if b["Type"] == 6:
        B["type6"] += 1
        if B["fmw"] is None:
            return web.Response(status=500)
        return web.Response(text=json.dumps({"Fmw": B["fmw"]}))
    return web.Response(text=json.dumps({"AirStreamDeviceUId": "1", "SystemV2": SYS}))


class Registry:
    def __init__(self):
        self.updates = []

    def async_update_device(self, device_id, **changes):
        self.updates.append((device_id, changes))


async def main():
    check("parse: modules and versions, serial dropped",
          parse_firmware(FMW) == {"COCB-2F5": "4.36", "C225-1O9": "3.80", "CCTSL-4E9": "2.38"},
          parse_firmware(FMW))
    check("parse: padding and junk skipped",
          parse_firmware("CR 3.23,,odd\x00\xff") == {"CR": "3.23"}
          and parse_firmware(None) == {})
    check("labels: hub, wall controller, boards by code",
          c.firmware_label("COCBH-4F5") == ("hub", "hub")
          and c.firmware_label("CR") == ("wall controller", "controller")
          and c.firmware_label("C225-1O9") == ("C225-1O9", "c225-1o9"))

    app = web.Application()
    app.router.add_post("/iZoneRequestV2", req)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host = f"127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    registry = Registry()
    coordinator_module.dr.async_get = lambda hass: registry
    async with aiohttp.ClientSession() as s:
        entry = ConfigEntry({"host": host, "uid": "1"})
        co = IZoneCoordinator(None, entry, IZoneClient(s, host), "1")
        entry.runtime_data = co
        await co.async_refresh()
        check("read at the first poll", co.firmware == parse_firmware(FMW)
              and co.hub_firmware == "4.36", co.firmware)

        ents = []
        await sensor.async_setup_entry(None, entry, ents.extend)
        fw = {e.unique_id: e for e in ents if isinstance(e, sensor.IZoneFirmwareSensor)}
        check("one sensor per module", sorted(fw) == ["1_firmware_c225-1o9",
              "1_firmware_cctsl-4e9", "1_firmware_hub"], sorted(fw))
        hub = fw["1_firmware_hub"]
        check("firmware sensors are off by default",
              not any(e._attr_entity_registry_enabled_default for e in fw.values()))
        check("hub sensor: version and name", hub.native_value == "4.36"
              and hub._attr_translation_placeholders == {"module": "hub"})
        check("the serial number isn't kept", "000000042" not in json.dumps(co.firmware))

        await co.async_refresh()
        check("not re-read until it's due", B["type6"] == 1, B["type6"])

        co.ac_device_id = "ac"
        B["fmw"] = FMW.replace("4.36", "4.39")
        co._firmware_due = 0
        await co.async_refresh()
        check("a change is picked up and shown on the AC device",
              hub.native_value == "4.39" and registry.updates == [("ac", {"sw_version": "4.39"})],
              registry.updates)

        B["fmw"] = None
        co._firmware_due = 0
        await co.async_refresh()
        check("a failed read keeps the versions and doesn't fail the poll",
              co.last_update_success and hub.native_value == "4.39")
        wait = co._firmware_due - time.monotonic()
        check("and is retried after FIRMWARE_RETRY", c.FIRMWARE_RETRY - 5 < wait <= c.FIRMWARE_RETRY,
              wait)

        # A bridge that doesn't know Type 6 answers with something else.
        co2 = IZoneCoordinator(None, ConfigEntry({}), IZoneClient(s, host), "2")
        B["fmw"] = ""
        await co2.async_refresh()
        ents2 = []
        await sensor.async_setup_entry(None, types.SimpleNamespace(runtime_data=co2),
                                       ents2.extend)
        check("no firmware list: no firmware sensors, poll fine",
              co2.firmware == {} and co2.data is not None
              and not any(isinstance(e, sensor.IZoneFirmwareSensor) for e in ents2))
    await runner.cleanup()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
