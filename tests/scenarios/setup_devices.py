"""Offline test: full setup with a fake device registry.

Zone devices must be linked to the AC device by registry device ID, never
through the deprecated `via_device` identifier in DeviceInfo.
"""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import importlib
import json
from types import SimpleNamespace

import aiohttp
import ha_stubs
from aiohttp import web

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        fails.append(name)


class Device:
    def __init__(self, n, identifiers):
        self.id = f"device{n}"
        self.identifiers = identifiers
        self.via_device_id = None
        self.name = None


class Registry:
    """Just enough of Home Assistant's device registry."""

    def __init__(self):
        self.devices = []
        self.deprecated = []  # identifiers created with via_device

    def _find(self, identifiers):
        return next((d for d in self.devices if d.identifiers & identifiers), None)

    def async_get_or_create(self, *, config_entry_id, identifiers, via_device=None,
                            via_device_id=None, name=None, **_):
        if via_device is not None:
            self.deprecated.append(identifiers)
        device = self._find(identifiers)
        if device is None:
            device = Device(len(self.devices), set(identifiers))
            self.devices.append(device)
        device.name = name or device.name
        if via_device_id is not None:
            device.via_device_id = via_device_id
        return device

    def async_get_device(self, identifiers):
        return self._find(identifiers)

    def async_update_device(self, device_id, *, via_device_id):
        next(d for d in self.devices if d.id == device_id).via_device_id = via_device_id


REGISTRY = Registry()
dr = sys.modules["homeassistant.helpers.device_registry"]
dr.async_get = lambda hass: REGISTRY
dr.DeviceRegistry = Registry
# The integration imports this helper by name, so set it before importing it.
SESSION = {}
sys.modules["homeassistant.helpers.aiohttp_client"].async_get_clientsession = (
    lambda hass: SESSION["session"]
)

SYS = {"SysOn": 0, "SysMode": 2, "SysFan": 4, "Setpoint": 2200, "Temp": 2200,
       "CtrlZone": 15, "NoOfZones": 3, "OemMake": 8, "AcUnitBrand": 26, "Tag1": "Ghar"}
Z = [
    {"Index": 0, "Name": "Living", "ZoneType": 3, "SensType": 0, "Mode": 3, "Setpoint": 2100},
    {"Index": 1, "Name": "Master", "ZoneType": 3, "SensType": 3, "Mode": 2, "Setpoint": 2300},
    {"Index": 2, "Name": "Bypass", "ZoneType": 2, "SensType": 255, "Mode": 2, "Setpoint": 0,
     "Bypass": 1, "SensorFault": 1},
]


async def req(r):
    b = (await r.json())["iZoneV2Request"]
    if b["Type"] == 1:
        return web.Response(text=json.dumps({"AirStreamDeviceUId": "000012345", "SystemV2": SYS}))
    if b["Type"] == 3:
        return web.Response(text=json.dumps({"SchedulesV2": {"Name": "", "Zones": []}}))
    return web.Response(text=json.dumps({"ZonesV2": Z[b["No"]]}))


async def setup(session, host):
    """Run the integration's async_setup_entry the way Home Assistant would."""
    integration = importlib.import_module("izone_local")
    from izone_local.coordinator import IZoneCoordinator

    IZoneCoordinator.async_config_entry_first_refresh = IZoneCoordinator.async_refresh
    SESSION["session"] = session
    unloads = []
    entry = ha_stubs.ConfigEntry({"host": host, "uid": "000012345"})
    entry.entry_id = "entry1"
    entry.async_on_unload = unloads.append

    async def forward(entry, platforms):
        for platform in platforms:
            module = importlib.import_module(f"izone_local.{platform}")

            def add(entities):
                for entity in entities:
                    info = entity.device_info
                    REGISTRY.async_get_or_create(config_entry_id=entry.entry_id, **info)

            await module.async_setup_entry(hass, entry, add)

    hass = SimpleNamespace(config_entries=SimpleNamespace(async_forward_entry_setups=forward))
    ok = await integration.async_setup_entry(hass, entry)
    for close in unloads:
        close()
    return ok


async def main():
    app = web.Application()
    app.router.add_post("/iZoneRequestV2", req)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host = f"127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    async with aiohttp.ClientSession() as s:
        check("setup succeeds", await setup(s, host) is True)
        ac = REGISTRY.async_get_device({("izone_local", "000012345")})
        zones = [REGISTRY.async_get_device({("izone_local", f"000012345_zone{i}")})
                 for i in range(3)]
        check("no deprecated via_device used", not REGISTRY.deprecated, REGISTRY.deprecated)
        check("AC device registered, named from Tag1", ac is not None and ac.name == "Ghar",
              ac and ac.name)
        check("every zone device linked to the AC by device ID",
              all(z is not None and z.via_device_id == ac.id for z in zones),
              [(z and z.via_device_id) for z in zones])
        count = len(REGISTRY.devices)
        check("second setup (a restart): no new devices, still linked",
              await setup(s, host) is True and len(REGISTRY.devices) == count
              and all(z.via_device_id == ac.id for z in zones), len(REGISTRY.devices))
    await runner.cleanup()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
