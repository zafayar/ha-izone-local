"""Offline test: bridge-problem sensor and the diagnostics download."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import json
import sys
import types

import ha_stubs  # noqa: F401


def _redact(data, keys):
    if isinstance(data, dict):
        return {k: ("**REDACTED**" if k in keys else _redact(v, keys)) for k, v in data.items()}
    if isinstance(data, list):
        return [_redact(v, keys) for v in data]
    return data


sys.modules["homeassistant.components.diagnostics"] = types.SimpleNamespace(
    async_redact_data=_redact)

import contextlib

import aiohttp
from aiohttp import web
from ha_stubs import ConfigEntry, HomeAssistantError
from izone_local import api, binary_sensor, climate, diagnostics
from izone_local import const as c
from izone_local.api import IZoneClient
from izone_local.coordinator import IZoneCoordinator

api.COMMAND_GAP = 0.01
api.CONNECT_RETRY_DELAYS = (0.01, 0.01)
c.CONFIRM_INTERVAL = 0.02
c.FAILED_SHOW = 2  # longer than the gap between the two failing requests

SYS = {"SysOn": 1, "SysMode": 2, "SysFan": 4, "Setpoint": 2200, "NoOfZones": 1,
       "Pass": "secret", "LockCode": "��", "CtrlZone": 15}
Z = [{"Index": 0, "Name": "Living", "ZoneType": 3, "SensType": 0, "Mode": 3, "Setpoint": 2100}]
B = {"down": False}
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        fails.append(name)


async def req(r):
    if B["down"]:
        return web.Response(status=500)
    b = (await r.json())["iZoneV2Request"]
    if b["Type"] == 1:
        return web.Response(text=json.dumps({"AirStreamDeviceUId": "000012345", "SystemV2": SYS}))
    if b["Type"] == 3:
        return web.Response(text=json.dumps({"SchedulesV2": {"Name": "Get Ready", "Zones": []}}))
    return web.Response(text=json.dumps({"ZonesV2": Z[b["No"]]}))


async def cmd(r):
    return web.Response(text="")  # rejects everything


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
        entry = ConfigEntry({"host": host, "uid": "000012345"})
        co = IZoneCoordinator(None, entry, IZoneClient(s, host), "000012345")
        await co.async_refresh()
        entry.runtime_data = co
        ents = []
        await binary_sensor.async_setup_entry(None, entry, ents.extend)
        problem = next(e for e in ents if isinstance(e, binary_sensor.IZoneBridgeProblem))
        clim = []
        await climate.async_setup_entry(None, entry, clim.extend)
        ac = clim[0]
        check("healthy: off", problem.is_on is False and problem.available)

        for fan in ("low", "medium"):
            with contextlib.suppress(HomeAssistantError):
                await ac.async_set_fan_mode(fan)
        check("two failures: still off", not problem.is_on
              and problem.extra_state_attributes["recent_failures"] == 2,
              problem.extra_state_attributes)
        B["down"] = True
        try:
            await co.async_refresh()
        except Exception:
            co.last_update_success = False
        check("a failed read counts: on at 3", problem.is_on, co.recent_failures)
        check("stays available while the bridge is unreachable", problem.available)
        c.FAILURE_WINDOW = 0
        check("failures age out", co.recent_failures == 0 and not problem.is_on)
        c.FAILURE_WINDOW = 300
        B["down"] = False
        await co.async_refresh()

        diag = await diagnostics.async_get_config_entry_diagnostics(None, entry)
        text = json.dumps(diag)
        check("diagnostics: password, lock code, host and uid redacted",
              "secret" not in text and "000012345" not in text and host not in text
              and diag["system"]["Pass"] == "**REDACTED**", diag["entry"])
        check("diagnostics: zones, favourites and health included",
              diag["zones"] and diag["favourites"][0]["Name"] == "Get Ready"
              and "recent_failures" in diag and "pending_changes" in diag, list(diag))
    await runner.cleanup()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
