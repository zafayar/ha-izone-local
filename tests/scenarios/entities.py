"""Offline test: entity behaviour and error paths the other scenarios don't reach.

The AC with a control zone, zone edge cases, the iSave switch, malformed
bridge replies, and failures while reading or applying.
"""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import json
import socket
import types

import aiohttp
import ha_stubs
from aiohttp import web
from ha_stubs import ConfigEntry, HomeAssistantError, HVACMode, ServiceValidationError
from izone_local import api, binary_sensor, climate, scene, switch
from izone_local import const as c
from izone_local.api import IZoneClient, IZoneConnectionError, parse_discovery_reply
from izone_local.coordinator import IZoneCoordinator

sys.modules["homeassistant.components.diagnostics"] = types.SimpleNamespace(
    async_redact_data=lambda data, keys: data)
from izone_local import diagnostics

api.COMMAND_GAP = 0.01
api.CONNECT_RETRY_DELAYS = (0.01, 0.01)
api.BUSY_RETRY_DELAYS = (0,)
c.CONFIRM_INTERVAL = 0.02
c.RESEND_AFTER = 0.3
c.FAILED_SHOW = 0.2

SYS = {"SysOn": 1, "SysMode": 2, "SysFan": 1, "Setpoint": 2200, "Temp": 2100, "Supply": 3000,
       "CtrlZone": 0, "NoOfZones": 3, "iSaveEnable": 1, "iSaveOn": 0}
Z = [
    {"Index": 0, "Name": "Living", "ZoneType": 3, "SensType": 0, "Mode": 1, "Setpoint": 2300,
     "Temp": 1950},
    {"Index": 1, "Name": "Hall", "ZoneType": 1, "SensType": 255, "Mode": 2, "Setpoint": 0,
     "Temp": 0},
    {"Index": 2, "Name": "Study", "ZoneType": 3, "SensType": 0, "Mode": 2, "Setpoint": 2100,
     "Temp": 2000},
]
FAV = {"Name": "Evening", "Mode": 1, "Fan": 2,
       "Zones": [{"Mode": 3, "Setpoint": 2200}, {"Mode": 1, "Setpoint": 2300},
                 {"Mode": 0, "Setpoint": 2300}] + [{"Mode": 1, "Setpoint": 2300}] * 11}
# reply: override the next reply for a request type; ignore: commands not applied;
# reject: commands answered with an error; down: every request fails.
B = {"reply": {}, "ignore": set(), "reject": set(), "down": False, "fav_fail": 0,
     "fail_system_reads": 0}
cmds, fails = [], []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        fails.append(name)


async def req(r):
    if B["down"]:
        return web.Response(status=500)
    b = (await r.json())["iZoneV2Request"]
    if b["Type"] == 1 and B["fail_system_reads"]:
        B["fail_system_reads"] -= 1
        return web.Response(status=500)
    if b["Type"] in B["reply"]:
        return web.Response(text=B["reply"].pop(b["Type"]))
    if b["Type"] == 1:
        return web.Response(text=json.dumps({"AirStreamDeviceUId": "1", "SystemV2": SYS}))
    if b["Type"] == 2:
        return web.Response(text=json.dumps({"ZonesV2": Z[b["No"]]}))
    if b["Type"] == 3:
        if b["No"] == 0 and B["fav_fail"]:
            B["fav_fail"] -= 1
            return web.Response(text="{}")
        if b["No"] == 0:
            return web.Response(text=json.dumps({"SchedulesV2": FAV}))
        if b["No"] == 1:
            return web.Response(text="{}")  # unreadable: skipped
        return web.Response(text=json.dumps({"SchedulesV2": {"Name": "", "Zones": []}}))
    return web.Response(text="{}")


async def cmd(r):
    b = await r.json()
    cmds.append(b)
    name, value = next(iter(b.items()))
    if name in B["reject"]:
        return web.Response(text="{ERROR}")
    if name in B["ignore"]:
        return web.Response(text="{OK}")
    if name in ("SysOn", "SysMode", "SysFan", "iSaveOn"):
        SYS[name] = value
    elif name == "SysSetpoint":
        SYS["Setpoint"] = value
    elif name == "ZoneMode":
        Z[value["Index"]]["Mode"] = value["Mode"]
    elif name == "ZoneSetpoint" and Z[value["Index"]]["Mode"] in (3, 4):
        Z[value["Index"]]["Setpoint"] = value["Setpoint"]
    elif name == "SchedAcFan":
        FAV["Fan"] = value["Fan"]
    return web.Response(text="{OK}")


async def raises(coro, kind=HomeAssistantError):
    try:
        await coro
    except kind as err:
        return str(err)
    return None


async def main():
    # Discovery replies that aren't a bridge.
    check("discovery: a reply without an address is ignored",
          parse_discovery_reply(b"ASPort_12107,Mac_000012345") is None
          and parse_discovery_reply(b"hello") is None)
    protocol = api._DiscoveryProtocol()
    protocol.datagram_received(b"not a bridge", ("10.0.0.1", 12107))
    check("discovery: other datagrams are ignored", protocol.found == {})

    # The change-broadcast port can't be opened.
    real_bind = socket.socket.bind
    socket.socket.bind = lambda self, address: (_ for _ in ()).throw(OSError("in use"))
    try:
        transport = await api.async_listen_for_changes("10.0.0.1", print)
    finally:
        socket.socket.bind = real_bind
    check("change broadcasts: a port that can't be opened gives None", transport is None)

    app = web.Application()
    app.router.add_post("/iZoneRequestV2", req)
    app.router.add_post("/iZoneCommandV2", cmd)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host = f"127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    async with aiohttp.ClientSession() as s:
        client = IZoneClient(s, host)
        for reply, call, what in (
            ("not json", client.get_system(), "Invalid JSON"),
            ("[1, 2]", client.get_system(), "Unexpected reply"),
            ("{}", client.get_system(), "No SystemV2"),
        ):
            B["reply"][1] = reply
            check(f"malformed system reply: {what}",
                  what in (await raises(call, IZoneConnectionError) or ""))
        B["reply"][2] = "{}"
        check("malformed zone reply", "No zone datagram" in (
            await raises(client.get_zone(0), IZoneConnectionError) or ""))
        B["reply"][2] = json.dumps({"Other": {"Index": 0, "Mode": 1}})
        check("a zone under another key is still found",
              (await client.get_zone(0)) == {"Index": 0, "Mode": 1})
        check("malformed favourite reply", "No favourite" in (
            await raises(client.get_favourite(1), IZoneConnectionError) or ""))

        entry = ConfigEntry({"host": host, "uid": "1"})
        co = IZoneCoordinator(None, entry, client, "1")
        await co.async_refresh()
        entry.runtime_data = co
        ents = []
        await climate.async_setup_entry(None, entry, ents.extend)
        ac, living, hall, study = ents

        # The AC with a zone in control (CtrlZone 0).
        check("AC: temperatures come from the control zone",
              ac.current_temperature == 19.5 and ac.target_temperature == 23.0)
        attrs = ac.extra_state_attributes
        check("AC: attributes", attrs["control_zone"] == 0 and attrs["supply_temperature"] == 30.0
              and attrs["status"] == "ready" and attrs["pending"] is False, attrs)
        check("AC: heat while on", ac.hvac_mode == HVACMode.HEAT)
        cmds.clear()
        await ac.async_set_temperature(temperature=21)
        check("AC: a temperature goes to the control zone, switched to auto first",
              {"ZoneMode": {"Index": 0, "Mode": 3}} in cmds
              and {"ZoneSetpoint": {"Index": 0, "Setpoint": 2100}} in cmds
              and Z[0]["Mode"] == 3 and Z[0]["Setpoint"] == 2100, cmds)
        await ac.async_set_hvac_mode(HVACMode.COOL)
        check("AC: cool", SYS["SysMode"] == 1 and ac.hvac_mode == HVACMode.COOL)
        await ac.async_set_hvac_mode(HVACMode.OFF)
        check("AC: off", SYS["SysOn"] == 0 and ac.hvac_mode == HVACMode.OFF)
        check("AC: zones show off while the AC is off", living.hvac_mode == HVACMode.OFF)
        await ac.async_turn_on()
        check("AC: turn on", SYS["SysOn"] == 1)
        cmds.clear()
        await ac.async_turn_on()
        await ac.async_set_fan_mode("low")
        check("AC: nothing sent when it's already so", cmds == [], cmds)
        check("AC: a zone-only mode is refused", "no auto mode" in (
            await raises(ac.async_set_hvac_mode(HVACMode.AUTO), ServiceValidationError) or ""))
        SYS["CtrlZone"] = 15
        await co.async_refresh()
        check("AC: the unit's own temperatures without a control zone",
              ac.current_temperature == 21.0 and ac.target_temperature == 22.0)
        await ac.async_set_temperature(temperature=24)
        check("AC: unit setpoint", SYS["Setpoint"] == 2400)
        await ac.async_turn_off()
        check("AC: turn off", SYS["SysOn"] == 0)

        # Zones.
        check("zones: temperature control only on auto zones",
              living.supported_features & 1 and not hall.supported_features & 1)
        check("zones: an open/close zone has no target or temperature",
              hall.target_temperature is None and hall.current_temperature is None)
        check("zone attributes", living.extra_state_attributes["zone_index"] == 0
              and living.extra_state_attributes["status"] == "ready")
        cmds.clear()
        await living.async_set_temperature()
        check("zone: no temperature, nothing sent", cmds == [])
        Z[2]["SensorFault"] = 1
        await co.async_refresh()
        check("zone: a dropped-out sensor shows no temperature", study.current_temperature is None)
        Z[2]["SensorFault"] = 0
        await living.async_turn_on()
        check("zone on: AC on", SYS["SysOn"] == 1)
        await living.async_turn_off()
        check("last zone off: AC off", SYS["SysOn"] == 0 and Z[0]["Mode"] == 2)

        # A setpoint asked for directly on a closed zone: the mode is added.
        await co.async_request({(2, "Setpoint"): 2250})
        check("setpoint on a closed zone: switched to auto, then set",
              Z[2]["Mode"] == 3 and Z[2]["Setpoint"] == 2250, Z[2])

        # iSave.
        sw = []
        await switch.async_setup_entry(None, entry, sw.extend)
        isave = sw[0]
        await isave.async_turn_on()
        check("iSave on", SYS["iSaveOn"] == 1 and isave.is_on
              and isave.extra_state_attributes == {"status": "ready", "pending": False})
        cmds.clear()
        await isave.async_turn_on()
        await isave.async_turn_off()
        check("iSave off (and nothing sent when already on)",
              cmds == [{"iSaveOn": 0}] and not isave.is_on, cmds)

        # Base entity behaviour.
        bins = []
        await binary_sensor.async_setup_entry(None, entry, bins.extend)
        check("an entity with no settable fields is never pending",
              bins[0].pending is False and bins[0].status == "ready")
        zone2 = co.data.zones.pop(2)
        check("a zone the bridge stops reporting is unavailable", not study.available)
        co.data.zones[2] = zone2

        # Failures while applying.
        B["ignore"] = {"SysFan"}
        c.PENDING_TIMEOUT, c.RESEND_AFTER = 0.3, 5
        msg = await raises(ac.async_set_fan_mode("high"))
        check("a change the bridge never shows fails after PENDING_TIMEOUT",
              msg and "no response" in msg, msg)
        c.PENDING_TIMEOUT, c.RESEND_AFTER = 30, 0.3
        B["ignore"] = set()
        B["reject"] = {"SysFan"}
        msg = await raises(ac.async_set_fan_mode("high"))
        check("a rejected command fails with the bridge's reply", msg and "rejected" in msg, msg)
        B["reject"] = set()
        before = co.recent_failures
        B["fail_system_reads"] = 3  # one sync, with its two retries
        await ac.async_set_fan_mode("medium")
        check("a failed read while applying counts, and the change still goes through",
              co.recent_failures > before and SYS["SysFan"] == 2, co.recent_failures)

        # Firmware details.
        co.firmware = {"CR": "3.23"}
        check("no hub module: no hub version", co.hub_firmware is None)
        co.firmware, co._firmware_due = {}, 0
        B["reply"][6] = json.dumps({"Fmw": "COCB-2F5 4.36"})
        await co.async_refresh()
        co._firmware_due = 0
        B["reply"][6] = json.dumps({"Fmw": "COCB-2F5 4.36"})
        await co.async_refresh()
        check("the same firmware read again changes nothing", co.firmware == {"COCB-2F5": "4.36"})

        # Favourites: unreadable slots, failed reads, and a rejected FavouriteSet.
        scenes = []
        await scene.async_setup_entry(None, entry, scenes.extend)
        check("an unreadable favourite slot is skipped", [x.index for x in scenes] == [0])
        evening = scenes[0]
        B["fav_fail"] = 1
        check("activating: a failed read is reported",
              "Can't read the favourite" in (await raises(evening.async_activate()) or ""))
        B["fav_fail"] = 1
        check("editing: a failed read is reported", "Can't read the favourite" in (
            await raises(evening.async_update_favourite(fan="high")) or ""))
        c.FAVOURITE_EDIT_GAP = 0.01
        B["fav_fail"] = 0
        orig_get = client.get_favourite
        reads = {"n": 0}

        async def flaky_get(index):
            reads["n"] += 1
            if reads["n"] == 2:  # the first read-back fails
                raise IZoneConnectionError("dropped")
            return await orig_get(index)

        client.get_favourite = flaky_get
        await evening.async_update_favourite(fan="high")
        client.get_favourite = orig_get
        check("editing: a failed read-back is retried", FAV["Fan"] == 3 and reads["n"] == 3,
              reads)

        changes, whole = co.favourite_changes(FAV)
        check("favourite: mode and fan, auto zone with setpoint, unset zone left alone",
              changes[(None, "SysMode")] == 1 and changes[(None, "SysFan")] == 3
              and changes[(0, "Setpoint")] == 2200 and (2, "Mode") not in changes
              and changes[(None, "SysOn")] == 1 and whole, changes)
        B["reject"] = {"FavouriteSet"}
        c.FAVOURITE_SETTLE = 0.2
        await evening.async_activate()
        for _ in range(100):
            if not co._wants:
                break
            await asyncio.sleep(0.03)
        check("a rejected FavouriteSet is applied zone by zone instead",
              Z[0]["Mode"] == 3 and Z[0]["Setpoint"] == 2200 and SYS["SysMode"] == 1, Z[0])
        B["reject"] = set()

        diag = await diagnostics.async_get_config_entry_diagnostics(None, entry)
        check("diagnostics: an unreadable favourite is listed with its error",
              diag["favourites"][1] == {"favourite": 2, "error": diag["favourites"][1]["error"]}
              and "No favourite" in diag["favourites"][1]["error"])
    await runner.cleanup()
    ha_stubs.notifications.clear()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
