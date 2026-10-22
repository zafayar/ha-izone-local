"""Offline test: the bridge sometimes answers with every live field zeroed.

The last good state is kept (for ZEROED_GRACE), and a zeroed read is never
compared with a change being applied.
"""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import json
import logging

import aiohttp
import ha_stubs  # noqa: F401
from aiohttp import web
from ha_stubs import ConfigEntry, UpdateFailed
from izone_local import api
from izone_local import const as c
from izone_local.api import IZoneClient
from izone_local.coordinator import IZoneCoordinator

api.COMMAND_GAP = 0.05
c.CONFIRM_INTERVAL = 0.03
c.RESEND_AFTER = 1.0

SYS = {"SysOn": 1, "SysMode": 2, "SysFan": 4, "Setpoint": 2200, "Temp": 2100,
       "NoOfZones": 2, "CtrlZone": 15}
Z = [
    {"Index": 0, "Name": "Living", "ZoneType": 3, "Mode": 1, "Setpoint": 2100, "Bypass": 0},
    {"Index": 1, "Name": "Master", "ZoneType": 3, "Mode": 3, "Setpoint": 2100, "Bypass": 0},
]
ZEROED_SYS = {**SYS, "SysOn": 0, "SysMode": 0, "SysFan": 0, "Setpoint": 0, "Temp": 0}
B = {"zeroed": False, "lag": 0.0}
cmds, fails = [], []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        fails.append(name)


class Messages(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


async def req(r):
    b = (await r.json())["iZoneV2Request"]
    if b["Type"] == 1:
        system = ZEROED_SYS if B["zeroed"] else SYS
        return web.Response(text=json.dumps({"AirStreamDeviceUId": "1", "SystemV2": system}))
    zone = Z[b["No"]]
    if B["zeroed"]:
        zone = {**zone, "Mode": 0, "Setpoint": 0}
    return web.Response(text=json.dumps({"ZonesV2": zone}))


async def cmd(r):
    b = await r.json()
    cmds.append(b)
    if "ZoneMode" in b:
        def apply():
            Z[b["ZoneMode"]["Index"]]["Mode"] = b["ZoneMode"]["Mode"]
        asyncio.get_running_loop().call_later(B["lag"], apply)
    return web.Response(text="{OK}")


async def main():
    log = Messages()
    logging.getLogger("izone_local").addHandler(log)
    logging.getLogger("izone_local").setLevel(logging.DEBUG)
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
        entry.runtime_data = co

        check("zeroed_reply: zeroed system", c.zeroed_reply(ZEROED_SYS))
        check("zeroed_reply: a zeroed zone", c.zeroed_reply(SYS, [{"Mode": 0}]))
        check("zeroed_reply: real data, or fields missing",
              not c.zeroed_reply(SYS, Z) and not c.zeroed_reply({}, [{}]))
        check("zeroed_reply: off with a real mode isn't zeroed",
              not c.zeroed_reply({**SYS, "SysOn": 0}))

        await co.async_refresh()
        good = co.data
        B["zeroed"] = True
        await co.async_refresh()
        check("a zeroed poll keeps the last state", co.data is good
              and co.data.system["SysOn"] == 1 and co.data.zones[0]["Mode"] == 1)
        check("a zeroed poll isn't counted as a failure", co.recent_failures == 0,
              co.recent_failures)

        c.ZEROED_GRACE = 0.1
        await asyncio.sleep(0.15)
        try:
            await co.async_refresh()
            check("still zeroed after the grace period: the update fails", False)
        except UpdateFailed as err:
            check("still zeroed after the grace period: the update fails",
                  "zeroed" in str(err), err)
        check("and that counts as a failure", co.recent_failures == 1, co.recent_failures)
        c.ZEROED_GRACE = 300

        B["zeroed"] = False
        await co.async_refresh()
        check("real data again: used", co.data is not good and co.data.zones[1]["Mode"] == 3)

        # A change being applied while the bridge answers zeroed. Taken at face
        # value, Mode 0 would become last_seen, and the real Mode 1 on the next
        # read would look like the zone was changed elsewhere: the change would
        # be dropped. The bridge applies the command late so that read happens.
        B["zeroed"], B["lag"] = True, 0.4
        asyncio.get_running_loop().call_later(0.2, lambda: B.update(zeroed=False))
        await asyncio.wait_for(co.async_request({(0, "Mode"): 2}), 5)
        check("change applied through zeroed reads", Z[0]["Mode"] == 2
              and co.data.zones[0]["Mode"] == 2, Z[0])
        check("not mistaken for a change made elsewhere",
              not any("changed elsewhere" in m for m in log.lines),
              [m for m in log.lines if "elsewhere" in m])
        check("zeroed reads during a change aren't failures", co.recent_failures == 1,
              co.recent_failures)
    await runner.cleanup()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
