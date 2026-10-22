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
import socket
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
        self.sw_version = None


class Registry:
    """Just enough of Home Assistant's device registry."""

    def __init__(self):
        self.devices = []
        self.deprecated = []  # identifiers created with via_device

    def _find(self, identifiers):
        return next((d for d in self.devices if d.identifiers & identifiers), None)

    def async_get_or_create(self, *, config_entry_id, identifiers, via_device=None,
                            via_device_id=None, name=None, sw_version=None, **_):
        if via_device is not None:
            self.deprecated.append(identifiers)
        device = self._find(identifiers)
        if device is None:
            device = Device(len(self.devices), set(identifiers))
            self.devices.append(device)
        device.name = name or device.name
        device.sw_version = sw_version or device.sw_version
        if via_device_id is not None:
            device.via_device_id = via_device_id
        return device

    def async_get_device(self, identifiers):
        return self._find(identifiers)

    def async_update_device(self, device_id, *, via_device_id=None,
                            remove_config_entry_id=None):
        device = next(d for d in self.devices if d.id == device_id)
        if via_device_id is not None:
            device.via_device_id = via_device_id
        if remove_config_entry_id is not None:
            self.devices.remove(device)


REGISTRY = Registry()
RELOADS, LISTENERS, ENTRY, SYSTEM_READS, UNLOADED, BROADCAST = [], [], [], [], [], []
dr = sys.modules["homeassistant.helpers.device_registry"]
dr.async_get = lambda hass: REGISTRY
dr.DeviceRegistry = Registry
dr.async_entries_for_config_entry = lambda registry, entry_id: list(registry.devices)
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
        SYSTEM_READS.append(1)
        return web.Response(text=json.dumps({"AirStreamDeviceUId": "000012345", "SystemV2": SYS}))
    if b["Type"] == 6:
        return web.Response(text=json.dumps({"Fmw": "COCB-2F5 4.36 (000000042),CR 3.23"}))
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
    ENTRY[:] = [entry]
    entry.async_on_unload = unloads.append

    async def forward(entry, platforms):
        for platform in platforms:
            module = importlib.import_module(f"izone_local.{platform}")

            def add(entities):
                for entity in entities:
                    info = entity.device_info
                    REGISTRY.async_get_or_create(config_entry_id=entry.entry_id, **info)

            await module.async_setup_entry(hass, entry, add)

    async def reload(entry_id):
        RELOADS.append(entry_id)

    async def unload_platforms(entry, platforms):
        UNLOADED.append(list(platforms))
        return True

    hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_forward_entry_setups=forward, async_reload=reload,
        async_unload_platforms=unload_platforms))
    await integration.async_setup(hass, {})
    ok = await integration.async_setup_entry(hass, entry)

    # The bridge broadcasts a change (if the port could be opened here).
    reads = len(SYSTEM_READS)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(b"iZoneChanged_Zones", ("127.0.0.1", 7005))
    sock.close()
    await asyncio.sleep(0.3)
    BROADCAST.append(len(SYSTEM_READS) > reads)

    if len(entry.update_listeners) == 1:
        # Updated, but not the options (e.g. a new address): no reload here.
        await entry.update_listeners[0](hass, entry)
        # Options changed: notifications turned off.
        entry.options = {"notifications": False}
        ha_stubs.notifications["izone_local_000012345_filter"] = ("Filter", "")
        await entry.update_listeners[0](hass, entry)
    LISTENERS.append(len(entry.update_listeners))
    for close in unloads:
        close()
    await integration.async_unload_entry(hass, entry)
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
        # A zone that has since been taken out of the system.
        REGISTRY.async_get_or_create(config_entry_id="entry1",
                                     identifiers={("izone_local", "000012345_zone7")})
        check("setup succeeds", await setup(s, host) is True)
        check("a zone the bridge no longer reports is removed",
              REGISTRY.async_get_device({("izone_local", "000012345_zone7")}) is None)
        ac = REGISTRY.async_get_device({("izone_local", "000012345")})
        zones = [REGISTRY.async_get_device({("izone_local", f"000012345_zone{i}")})
                 for i in range(3)]
        check("no deprecated via_device used", not REGISTRY.deprecated, REGISTRY.deprecated)
        check("AC device registered, named from Tag1", ac is not None and ac.name == "Ghar",
              ac and ac.name)
        check("every zone device linked to the AC by device ID",
              all(z is not None and z.via_device_id == ac.id for z in zones),
              [(z and z.via_device_id) for z in zones])
        check("a change broadcast from the bridge triggers a read", BROADCAST[0], BROADCAST)
        check("unload unloads every platform", len(UNLOADED[0]) == 6, UNLOADED)
        check("hub firmware is the AC device's software version", ac.sw_version == "4.36",
              ac.sw_version)
        check("options listener: turning notifications off clears them and reloads",
              LISTENERS == [1] and RELOADS == ["entry1"] and not ha_stubs.notifications,
              (LISTENERS, RELOADS, ha_stubs.notifications))
        import izone_local
        removable = await izone_local.async_remove_config_entry_device(
            None, ENTRY[0], SimpleNamespace(identifiers={("izone_local", "000012345_zone9")}))
        kept = await izone_local.async_remove_config_entry_device(None, ENTRY[0], zones[0])
        check("the user can delete only devices the bridge doesn't report",
              removable and not kept)
        count = len(REGISTRY.devices)
        check("second setup (a restart): no new devices, still linked",
              await setup(s, host) is True and len(REGISTRY.devices) == count
              and all(z.via_device_id == ac.id for z in zones), len(REGISTRY.devices))
    await runner.cleanup()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
