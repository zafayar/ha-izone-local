"""Offline test of the reconcile loop against a stateful fake bridge."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import json
import sys

import aiohttp
import ha_stubs  # noqa
from aiohttp import web
from ha_stubs import ConfigEntry, HomeAssistantError, HVACMode
from izone_local import api, climate
from izone_local import const as c
from izone_local.api import IZoneClient
from izone_local.coordinator import IZoneCoordinator

api.COMMAND_GAP = 0.1
c.CONFIRM_INTERVAL = 0.03
c.RESEND_AFTER = 0.4
c.FAILED_SHOW = 0.3

SYS = {"SysOn": 0, "SysMode": 2, "SysFan": 4, "Setpoint": 2200, "Temp": 2200,
       "CtrlZone": 15, "NoOfZones": 4, "OemMake": 8, "AcUnitBrand": 26, "Tag1": "Ghar"}
Z = [
    {"Index": 0, "Name": "Living", "ZoneType": 3, "Mode": 1, "Setpoint": 2100, "Temp": 2000, "Bypass": 0},
    {"Index": 1, "Name": "Master", "ZoneType": 3, "Mode": 2, "Setpoint": 2100, "Temp": 2000, "Bypass": 0},
    {"Index": 2, "Name": "Hall", "ZoneType": 1, "Mode": 2, "Setpoint": 0, "Temp": 0, "Bypass": 0},
    {"Index": 3, "Name": "Bypass", "ZoneType": 2, "Mode": 5, "Setpoint": 0, "Temp": 0, "Bypass": 1},
]
cmds, reads, fails = [], [], []
B = {"lag": 0, "drop": {}, "reject": set(), "on_close": None, "fault": set(),
     "after_drop": None}


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        fails.append(name)


def apply(b):
    if "SysOn" in b: SYS["SysOn"] = b["SysOn"]
    if "SysMode" in b: SYS["SysMode"] = b["SysMode"]
    if "SysFan" in b: SYS["SysFan"] = b["SysFan"]
    if "ZoneMode" in b:
        Z[b["ZoneMode"]["Index"]]["Mode"] = b["ZoneMode"]["Mode"]
        if b["ZoneMode"]["Mode"] == 2 and B["on_close"]:
            B["on_close"]()
    if "ZoneSetpoint" in b:
        z = Z[b["ZoneSetpoint"]["Index"]]
        if z["Mode"] in (3, 4):
            z["Setpoint"] = b["ZoneSetpoint"]["Setpoint"]
        else:
            z["Mode"] = 3  # like the real bridge: switches to Auto, drops the value


async def req(r):
    b = (await r.json())["iZoneV2Request"]
    reads.append(b["Type"] if b["Type"] == 1 else f"z{b['No']}")
    if b["Type"] == 1:
        return web.Response(text=json.dumps({"AirStreamDeviceUId": "1", "SystemV2": SYS}))
    zone = Z[b["No"]]
    if b["No"] in B["fault"]:
        # A dropped-out sensor: reported closed; the real mode is remembered.
        zone = {**zone, "Mode": 2, "SensorFault": 1, "SensType": 0}
    return web.Response(text=json.dumps({"AirStreamDeviceUId": "1", "ZonesV2": zone}))


async def cmd(r):
    b = await r.json()
    cmds.append(b)
    name = next(iter(b))
    if name in B["reject"]:
        return web.Response(text="")
    if B["drop"].get(name, 0):
        B["drop"][name] -= 1
        if B["after_drop"]:
            asyncio.get_running_loop().call_later(0.1, B["after_drop"])
        return web.Response(text="{OK}")
    if B["lag"]:
        asyncio.get_running_loop().call_later(B["lag"], apply, b)
    else:
        apply(b)
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
        ac = ents[0]
        zone = {co.data.zones[e.index]["Name"]: e for e in ents[1:]}

        def modes():
            return {n: str(e.hvac_mode) for n, e in zone.items()}

        async def run(coro, watch):
            """Run an action; record the statuses `watch` showed meanwhile."""
            seen = []
            task = asyncio.create_task(coro)
            await asyncio.sleep(0)
            first = (modes(), str(ac.hvac_mode), watch.status, watch.icon)
            while not task.done():
                if not seen or seen[-1] != watch.status:
                    seen.append(watch.status)
                await asyncio.sleep(0.005)
            await task
            return first, seen

        check("AC off: every zone reports off", set(modes().values()) == {"off"}, modes())

        # A. Zone on while the AC is off.
        cmds.clear()
        first, seen = await run(zone["Master"].async_turn_on(), zone["Master"])
        check("A instant: Master auto, Living off, AC heat", first[0]["Master"] == "auto"
              and first[0]["Living"] == "off" and first[1] == "heat", first)
        check("A instant: Master queued with hourglass",
              first[2] == "queued" and first[3] == "mdi:timer-sand", first)
        check("A order: close Living, Master auto, then AC on", cmds == [
            {"ZoneMode": {"Index": 0, "Mode": 2}}, {"ZoneMode": {"Index": 1, "Mode": 3}},
            {"SysOn": 1}], cmds)
        check("A status: queued, then sending (1 of 2)",
              seen[:2] == ["queued", "sending (1 of 2)"], seen)
        check("A done: ready, default icon, nothing pending", zone["Master"].status == "ready"
              and zone["Master"].icon is None and not co._wants and not co._requests,
              (zone["Master"].status, co._wants))

        # B/C/D. More zones on and off.
        cmds.clear()
        await zone["Hall"].async_turn_on()
        check("B second zone on: one command", cmds == [{"ZoneMode": {"Index": 2, "Mode": 1}}], cmds)
        cmds.clear()
        await zone["Master"].async_turn_off()
        check("C one of two off: AC stays on",
              cmds == [{"ZoneMode": {"Index": 1, "Mode": 2}}] and SYS["SysOn"] == 1, cmds)
        reads.clear(); cmds.clear()
        await zone["Hall"].async_turn_off()
        check("D last zone off: close, then AC off",
              cmds == [{"ZoneMode": {"Index": 2, "Mode": 2}}, {"SysOn": 0}], cmds)
        check("D every zone read before the AC goes off", {"z0", "z1", "z2", "z3"} <= set(reads), reads)

        # E. Temperature on a closed zone with the AC off.
        cmds.clear()
        await zone["Master"].async_set_temperature(temperature=23)
        check("E Auto, setpoint once Auto shows, then AC on", cmds == [
            {"ZoneMode": {"Index": 1, "Mode": 3}},
            {"ZoneSetpoint": {"Index": 1, "Setpoint": 2300}}, {"SysOn": 1}], cmds)
        check("E Master auto at 23", Z[1]["Mode"] == 3 and Z[1]["Setpoint"] == 2300
              and zone["Master"].target_temperature == 23.0, Z[1])

        # M. A sync reads only what the action touches.
        reads.clear()
        await zone["Master"].async_set_temperature(temperature=24)
        check("M one-zone action reads the system and that zone only", set(reads) == {1, "z1"}, reads)

        # F. The AC's own off/on resumes the zones.
        cmds.clear()
        await ac.async_turn_off()
        check("F AC off: one command, zones report off, Master stays Auto on the bridge",
              cmds == [{"SysOn": 0}] and set(modes().values()) == {"off"} and Z[1]["Mode"] == 3,
              (cmds, modes()))
        cmds.clear()
        await ac.async_turn_on()
        check("F AC on resumes Master", cmds == [{"SysOn": 1}] and modes()["Master"] == "auto", modes())

        # G. Dropped once: re-sent, then it works.
        B["drop"] = {"SysFan": 1}; cmds.clear()
        await ac.async_set_fan_mode("low")
        check("G dropped command re-sent once",
              cmds == [{"SysFan": 1}, {"SysFan": 1}] and ac.fan_mode == "low", cmds)

        # H. Dropped every time: fails after 3 sends, shows failed, then ready.
        B["drop"] = {"SysFan": 99}; cmds.clear()
        try:
            await ac.async_set_fan_mode("high")
            check("H raises", False)
        except HomeAssistantError as err:
            check("H fails after 3 sends with a clear error",
                  len(cmds) == 3 and "didn't apply" in str(err), (cmds, err))
        check("H fan back to the bridge's low; failed status and alert icon",
              ac.fan_mode == "low" and ac.status == "failed"
              and ac.icon == "mdi:alert-circle-outline", (ac.fan_mode, ac.status, ac.icon))
        await asyncio.sleep(0.5)
        check("H failed clears to ready", ac.status == "ready" and ac.icon is None, ac.status)
        B["drop"] = {}

        # I. Rejected (empty reply): fails without re-sending.
        B["reject"] = {"SysFan"}; cmds.clear()
        try:
            await ac.async_set_fan_mode("high")
            check("I raises", False)
        except HomeAssistantError:
            check("I rejected command fails at once", len(cmds) == 1, cmds)
        B["reject"] = set()
        await asyncio.sleep(0.4)

        # J. The bridge reports late (0.3 s): no needless re-send.
        B["lag"] = 0.3; cmds.clear()
        _, seen = await run(ac.async_set_fan_mode("high"), ac)
        check("J late report: sent once, then confirmed",
              cmds == [{"SysFan": 3}] and ac.fan_mode == "high", cmds)
        check("J status: confirming while the bridge catches up, then ready",
              "confirming" in seen and ac.status == "ready", seen)
        B["lag"] = 0

        # K. Last zone off, but Living is opened at the wall meanwhile.
        B["on_close"] = lambda: Z[0].__setitem__("Mode", 1)
        cmds.clear()
        await zone["Master"].async_turn_off()
        check("K AC stays on, no power-off sent, no error",
              SYS["SysOn"] == 1 and {"SysOn": 0} not in cmds and str(ac.hvac_mode) == "heat",
              (cmds, SYS["SysOn"]))
        B["on_close"] = None
        await zone["Living"].async_turn_off()
        check("K then turning Living off turns the AC off", SYS["SysOn"] == 0, SYS)

        # L. Two zones pressed together while the AC is off.
        cmds.clear()
        await asyncio.gather(zone["Master"].async_turn_on(), zone["Hall"].async_turn_on())
        check("L both on, AC turned on once", modes()["Master"] == "auto"
              and modes()["Hall"] == "fan_only" and cmds.count({"SysOn": 1}) == 1
              and SYS["SysOn"] == 1, (cmds, modes()))

        # O. A zone whose sensor has dropped out.
        Z[1]["Mode"] = 2
        B["fault"] = {1}
        await co.async_refresh()
        cmds.clear()
        t0 = asyncio.get_running_loop().time()
        await zone["Master"].async_set_temperature(temperature=22)
        took = asyncio.get_running_loop().time() - t0
        check("O faulted zone: returns at once, nothing sent to it",
              took < 1 and not [x for x in cmds if "Zone" in next(iter(x))], (took, cmds))
        check("O status waiting for sensor, thermometer-off icon, shown as auto",
              zone["Master"].status == "waiting for sensor"
              and zone["Master"].icon == "mdi:thermometer-off"
              and str(zone["Master"].hvac_mode) == "auto", zone["Master"].status)
        check("O no fast polling while only waiting",
              co._loop_task is None or co._loop_task.done(), co._loop_task)
        check("O no temperature shown for a faulted sensor",
              zone["Master"].current_temperature is None)
        B["fault"] = set()
        cmds.clear()
        await co.async_refresh()  # the regular poll sees the sensor back
        for _ in range(100):
            if zone["Master"].status == "ready":
                break
            await asyncio.sleep(0.02)
        check("O sensor back: Auto then the setpoint are sent, then ready", cmds == [
            {"ZoneMode": {"Index": 1, "Mode": 3}},
            {"ZoneSetpoint": {"Index": 1, "Setpoint": 2200}}]
            and zone["Master"].status == "ready" and Z[1]["Setpoint"] == 2200,
            (cmds, zone["Master"].status))

        # P. Changed at the wall while our change was on its way.
        B["drop"] = {"SysFan": 1}
        B["after_drop"] = lambda: SYS.__setitem__("SysFan", 2)
        cmds.clear()
        try:
            await ac.async_set_fan_mode("low")
            raised = None
        except HomeAssistantError as err:
            raised = err
        check("P changed elsewhere: no error, not re-sent, their medium kept",
              raised is None and cmds == [{"SysFan": 1}] and ac.fan_mode == "medium"
              and not co._wants, (raised, cmds, ac.fan_mode))
        B["drop"], B["after_drop"] = {}, None

        # N. Override isn't cancelled by asking for Auto.
        Z[1]["Mode"] = 4
        await co.async_refresh()
        cmds.clear()
        await zone["Master"].async_set_hvac_mode(HVACMode.AUTO)
        check("N override zone turned to auto: no command", cmds == [], cmds)
    await runner.cleanup()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
