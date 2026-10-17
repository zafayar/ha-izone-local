"""Exercise izone_local/api.py against a fake bridge (HTTP + UDP)."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components/izone_local"))


import asyncio
import json
import socket
import sys

import aiohttp
from aiohttp import web

import api  # noqa: E402

SYSTEM = {"SysOn": 1, "SysMode": 1, "SysFan": 2, "Setpoint": 2350, "Temp": 2410,
          "Supply": 1600, "CtrlZone": 15, "NoOfZones": 2, "ACError": " OK",
          "Warnings": "none", "iSaveEnable": 1, "iSaveOn": 0}
ZONES = [{"Index": 0, "Name": "Living", "ZoneType": 3, "Mode": 3, "Setpoint": 2200,
          "Temp": 2300, "DmpPos": 40},
         {"Index": 1, "Name": "Bed 1", "ZoneType": 1, "Mode": 2, "Setpoint": 0,
          "Temp": 0, "DmpPos": 0}]

state = {"active": 0, "max_active": 0, "commands": [], "zone_key": "ZonesV2",
         "mode": "normal"}
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not cond:
        fails.append(name)


async def handle_request(req):
    state["requests"] = state.get("requests", 0) + 1
    if state.get("fail_next", 0):
        state["fail_next"] -= 1
        return web.Response(status=500, text="hiccup")
    if state.get("drop_next", 0):
        state["drop_next"] -= 1
        req.transport.close()  # the bridge drops the connection
        return web.Response(text="")
    state["active"] += 1
    state["max_active"] = max(state["max_active"], state["active"])
    try:
        await asyncio.sleep(0.05)
        if state["mode"] == "500":
            return web.Response(status=500, text="boom")
        if state["mode"] == "badjson":
            return web.Response(text="not json")
        if state["mode"] == "slow":
            await asyncio.sleep(2)
        body = await req.json()
        r = body["iZoneV2Request"]
        if r["Type"] == 1:
            out = {"AirStreamDeviceUId": "000012345", "DeviceType": "ASH",
                   "SystemV2": {**SYSTEM, "LockCode": "LOCK"}}
            # Real bridges send raw 0xFF bytes here and no Content-Type.
            raw = json.dumps(out).encode().replace(b'"LOCK"', b'"\xff\xff\xff"')
            return web.Response(body=raw)
        else:
            out = {"AirStreamDeviceUId": "000012345", state["zone_key"]: ZONES[r["No"]]}
        return web.Response(text=json.dumps(out))
    finally:
        state["active"] -= 1


async def handle_command(req):
    body = await req.json()
    state["commands"].append(body)
    if "Bogus" in body:
        return web.Response(text="")
    if state.get("busy", 0) > 0:
        state["busy"] -= 1
        return web.Response(text="{BUSY}")
    if "ZoneSetpoint" in body and not state.get("drop_setpoints"):
        zs = body["ZoneSetpoint"]
        ZONES[zs["Index"]]["Setpoint"] = zs["Setpoint"]
    return web.Response(text="{OK}")


class Responder(asyncio.DatagramProtocol):
    def connection_made(self, t):
        self.t = t

    def datagram_received(self, data, addr):
        if data == b"IASD":
            self.t.sendto(b"ASPort_12107,Mac_000012345,IP_127.0.0.1,iZoneV2,iZone,X,iPower", addr)


async def main():
    app = web.Application()
    app.router.add_post("/iZoneRequestV2", handle_request)
    app.router.add_post("/iZoneCommandV2", handle_command)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    host = f"127.0.0.1:{port}"

    async with aiohttp.ClientSession() as session:
        client = api.IZoneClient(session, host)

        api.CONNECT_RETRY_DELAYS = (0.01, 0.01)
        uid, system = await client.get_system()
        check("get_system uid", uid == "000012345", uid)

        state["requests"] = 0
        state["fail_next"] = 2
        uid, _ = await client.get_system()
        check("two server errors, then retried successfully",
              uid == "000012345" and state["requests"] == 3, state["requests"])
        state["requests"] = 0
        state["drop_next"] = 1
        uid, _ = await client.get_system()
        check("dropped connection, then retried successfully",
              uid == "000012345" and state["requests"] == 2, state["requests"])
        state["requests"] = 0
        check("get_system data", system["Setpoint"] == 2350)

        z = await client.get_zone(0)
        check("get_zone ZonesV2", z["Name"] == "Living")
        state["zone_key"] = "SomethingElse"
        z = await client.get_zone(1)
        check("get_zone fallback key", z["Name"] == "Bed 1")
        state["zone_key"] = "ZonesV2"

        # Concurrency: lock should serialise
        await asyncio.gather(*(client.get_zone(i % 2) for i in range(6)))
        check("requests serialised", state["max_active"] == 1, state["max_active"])

        await client.set_power(True)
        await client.set_setpoint(23.4)
        await client.set_zone_mode(0, 2)
        await client.set_zone_setpoint(1, 21.0)
        await client.set_isave(False)
        check("command payloads", state["commands"] == [
            {"SysOn": 1}, {"SysSetpoint": 2350}, {"ZoneMode": {"Index": 0, "Mode": 2}},
            {"ZoneSetpoint": {"Index": 1, "Setpoint": 2100}}, {"iSaveOn": 0}],
            state["commands"])

        api.BUSY_RETRY_DELAYS = (0.01, 0.01, 0.01, 0)

        # Command spacing: two back-to-back commands are >= COMMAND_GAP apart
        api.COMMAND_GAP = 0.3
        t0 = asyncio.get_running_loop().time()
        await client.set_fan(1)
        await client.set_fan(2)
        gap = asyncio.get_running_loop().time() - t0
        check("commands spaced by COMMAND_GAP", gap >= 0.3, f"{gap:.2f}s")
        api.COMMAND_GAP = 0
        state["commands"].clear()
        state["busy"] = 2
        await client.set_fan(4)
        check("BUSY retried then OK", state["commands"] == [{"SysFan": 4}] * 3,
              state["commands"])
        state["busy"] = 10
        try:
            await client.set_fan(4)
            check("persistent BUSY raises", False)
        except api.IZoneCommandError:
            check("persistent BUSY raises", True)
        state["busy"] = 0

        api.COMMAND_GAP = 0.5
        await client.set_fan(1)
        r = client.command_ready_in()
        check("command_ready_in after a command", 0.3 < r <= 0.5, r)
        api.COMMAND_GAP = 0
        check("command_ready_in with no gap", client.command_ready_in() == 0)

        try:
            await client.command("Bogus", 1)
            check("rejected command raises", False)
        except api.IZoneCommandError:
            check("rejected command raises", True)

        for mode, exc, tries in (("500", api.IZoneConnectionError, 3),
                                 ("badjson", api.IZoneConnectionError, 1)):
            state["mode"] = mode
            state["requests"] = 0
            try:
                await client.get_system()
                check(f"{mode} raises", False)
            except exc:
                check(f"{mode} raises after {tries} attempt(s)",
                      state["requests"] == tries, state["requests"])
        state["mode"] = "slow"
        api.REQUEST_TIMEOUT = 0.5
        try:
            await client.get_system()
            check("timeout raises", False)
        except api.IZoneConnectionError as err:
            check("timeout raises IZoneConnectionError, naming the error",
                  "TimeoutError" in str(err), str(err))
        state["mode"] = "normal"

        dead = api.IZoneClient(session, "127.0.0.1:1")
        try:
            await dead.get_system()
            check("unreachable raises", False)
        except api.IZoneConnectionError:
            check("unreachable raises", True)

    check("to_centi rounding", [api.to_centi(x) for x in (23.4, 22.25, 40, 10)]
          == [2350, 2250, 3000, 1500], [api.to_centi(x) for x in (23.4, 22.25, 40, 10)])
    check("from_centi", api.from_centi(2350) == 23.5 and api.from_centi(None) is None)
    b = api.parse_discovery_reply(b"ASPort_12107,Mac_000012345,IP_192.168.1.50,iZoneV2,iZone,X,iPower")
    check("parse discovery", b and b.uid == "000012345" and b.host == "192.168.1.50"
          and b.supports_v2 and "X" not in b.capabilities, b)
    check("parse junk", api.parse_discovery_reply(b"hello") is None)

    # Discovery over real broadcast to a local responder
    loop = asyncio.get_running_loop()
    try:
        t, _ = await loop.create_datagram_endpoint(
            Responder, local_addr=("0.0.0.0", api.DISCOVERY_PORT),
            family=socket.AF_INET)
        found = await api.async_discover(timeout=1.5)
        t.close()
        print(f"INFO discovery found: {found}")
    except OSError as err:
        print(f"INFO discovery responder could not bind: {err}")

    await runner.cleanup()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
