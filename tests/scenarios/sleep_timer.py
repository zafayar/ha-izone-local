"""Offline test: the sleep timer select, against a bridge that behaves like the
real one (measured): the minutes left show a moment after the command, a
minute less; setting a timer turns the AC on; 0 cancels; other values get
{ERROR}."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import json
from datetime import UTC, datetime, timedelta

import aiohttp
import ha_stubs  # noqa: F401
from aiohttp import web
from ha_stubs import ConfigEntry, HomeAssistantError
from izone_local import api, select, sensor
from izone_local import const as c
from izone_local.api import IZoneClient
from izone_local.coordinator import IZoneCoordinator

api.COMMAND_GAP = 0.02
c.CONFIRM_INTERVAL = 0.02
c.SLEEP_TIMER_SETTLE = 0.3
SHOW_AFTER = 0.15  # longer than RESEND_AFTER would allow other changes

SYS = {"SysOn": 1, "SysMode": 1, "SysFan": 1, "NoOfZones": 0, "CtrlZone": 15,
       "SleepTimer": 0, "SleepTimerM": 0}
B = {"ignore": 0, "error": False, "tick": False}
cmds, fails = [], []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        fails.append(name)


async def req(r):
    if B["tick"] and 1 < SYS["SleepTimerM"] < 60:
        SYS["SleepTimerM"] -= 1  # the old timer running, a minute per read
    return web.Response(text=json.dumps({"AirStreamDeviceUId": "1", "SystemV2": SYS}))


async def show(minutes):
    await asyncio.sleep(SHOW_AFTER)
    SYS["SleepTimerM"] = max(minutes - 1, 0)
    SYS["SleepTimer"] = minutes or SYS["SleepTimer"]  # stale: not cleared by 0


async def cmd(r):
    b = await r.json()
    cmds.append(b)
    if "SysSleepTimer" in b:
        minutes = b["SysSleepTimer"]
        if B["error"] or minutes % 30 or not 0 <= minutes <= 720:
            return web.Response(text="{ERROR}")
        if B["ignore"]:
            B["ignore"] -= 1
            return web.Response(text="{OK}")
        if minutes:
            SYS["SysOn"] = 1
        asyncio.get_running_loop().create_task(show(minutes))
    if "SysOn" in b:
        SYS["SysOn"] = b["SysOn"]
        if not b["SysOn"]:
            SYS["SleepTimerM"] = 0
    return web.Response(text="{OK}")


async def main():
    check("panel steps, rounded up",
          [c.sleep_timer_option(m) for m in (0, None, 1, 29, 30, 31, 59, 241, 359, 719, 900)]
          == ["off", "off", "30m", "30m", "30m", "1h", "1h", "6h", "6h", "12h", "12h"])

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
        await co.async_refresh()
        ents = []
        await select.async_setup_entry(None, entry, ents.extend)
        timer = ents[0]
        sens = []
        await sensor.async_setup_entry(None, entry, sens.extend)
        end = next(e for e in sens if isinstance(e, sensor.IZoneSleepTimerEndSensor))
        now = [datetime(2026, 10, 23, 21, 0, tzinfo=UTC)]
        sensor.dt_util.utcnow = lambda: now[0]
        check("end sensor: a timestamp, unknown with no timer",
              end.unique_id == "1_sleep_timer_end" and end._attr_device_class == "timestamp"
              and end.native_value is None)
        check("one select, the panel's options",
              timer.unique_id == "1_sleep_timer"
              and timer.options == ["off", "30m", "1h", "2h", "4h", "6h", "8h", "12h"]
              and timer.current_option == "off")

        await timer.async_select_option("1h")
        check("1 hour: sent once, though it shows late",
              cmds == [{"SysSleepTimer": 60}], cmds)
        check("shown from the minutes left",
              timer.current_option == "1h" and timer.extra_state_attributes["minutes_left"] == 59
              and timer.status == c.STATUS_READY, timer.extra_state_attributes)
        first = end.native_value
        check("end time from the minutes left", first == now[0] + timedelta(minutes=59), first)
        now[0] += timedelta(seconds=50)
        check("steady between countdown steps", end.native_value == first)
        SYS["SleepTimerM"] = 58
        now[0] += timedelta(seconds=20)
        check("steady across a step", end.native_value == first)

        # While the 1 h timer counts down, a change the bridge first drops
        # is re-sent: the countdown isn't mistaken for a change elsewhere.
        cmds.clear()
        B["tick"], B["ignore"] = True, 1
        await timer.async_select_option("4h")
        B["tick"] = False
        check("a new timer moves the end time",
              end.native_value == now[0] + timedelta(minutes=239), end.native_value)
        check("re-sent after the countdown moved",
              cmds == [{"SysSleepTimer": 240}] * 2 and timer.current_option == "4h", cmds)

        cmds.clear()
        await timer.async_select_option("off")
        check("off cancels it", cmds == [{"SysSleepTimer": 0}]
              and timer.current_option == "off" and SYS["SleepTimer"] == 240, cmds)

        # A change on the wall panel while ours is still being applied wins.
        cmds.clear()
        B["ignore"] = 3
        task = asyncio.create_task(timer.async_select_option("2h"))
        await asyncio.sleep(0.1)
        SYS["SleepTimerM"] = 480
        await task
        B["ignore"] = 0
        check("a change on the panel meanwhile wins, without an error",
              timer.current_option == "8h" and len(cmds) < 3, cmds)

        cmds.clear()
        B["error"] = True
        try:
            await timer.async_select_option("30m")
            raised = None
        except HomeAssistantError as err:
            raised = err
        B["error"] = False
        check("{ERROR} raises", raised is not None and "SysSleepTimer" in str(raised), raised)

        # With the AC off: turning it off ended the timer, whatever the bridge
        # still reports.
        SYS["SysOn"], SYS["SleepTimerM"] = 0, 12
        await co.async_refresh()
        check("AC off: shown as off", timer.current_option == "off"
              and end.native_value is None)
        cmds.clear()
        await timer.async_select_option("off")
        check("AC off, choose off: nothing sent", cmds == [], cmds)

        SYS["SleepTimerM"] = 0
        await co.async_refresh()
        await timer.async_select_option("30m")
        check("AC off, choose 30 min: one command turns it on for 30 min",
              cmds == [{"SysSleepTimer": 30}] and SYS["SysOn"] == 1
              and co.system_view()["SysOn"] == 1 and timer.current_option == "30m", cmds)
    await runner.cleanup()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
