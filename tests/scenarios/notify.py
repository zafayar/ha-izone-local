"""Offline test: change broadcasts trigger a refresh (only from the bridge)."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))
sys.path.insert(0, str(_ROOT / "custom_components"))

import asyncio
import socket
import sys

import ha_stubs  # noqa: F401

from izone_local import api  # noqa: E402
from izone_local.coordinator import IZoneCoordinator  # noqa: E402
from ha_stubs import ConfigEntry  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        fails.append(name)


def send(data, port, src="127.0.0.1"):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((src, 0))
    s.sendto(data, ("127.0.0.1", port))
    s.close()


async def main():
    port = 47005  # a free port, so nothing else on this PC interferes
    got = []
    t = await api.async_listen_for_changes("127.0.0.1", got.append, port=port)
    check("listener opened", t is not None)
    send(b"iZoneChanged_Zones", port)
    send(b"something else", port)
    await asyncio.sleep(0.2)
    check("bridge broadcast passed on; other messages ignored",
          got == ["iZoneChanged_Zones"], got)

    # A second bridge's listener can share the port. (Which socket gets a
    # unicast datagram on a shared port differs by OS, so it isn't sent one;
    # real broadcasts reach every socket.)
    t2 = await api.async_listen_for_changes("10.9.9.9", got.append, port=port)
    check("second listener can share the port", t2 is not None)
    t.close()
    t2.close()

    other = []
    t3 = await api.async_listen_for_changes("10.9.9.9", other.append, port=port + 1)
    send(b"iZoneChanged_System", port + 1)
    await asyncio.sleep(0.2)
    check("a broadcast from another address is ignored", other == [], other)
    t3.close()

    # The coordinator refreshes on a broadcast, unless its loop is running.
    entry = ConfigEntry({"host": "x", "uid": "1"})
    co = IZoneCoordinator(None, entry, None, "1")
    calls = []

    async def refresh():
        calls.append(1)

    co.async_request_refresh = refresh
    co.notify_changed()
    await asyncio.sleep(0.05)
    check("broadcast -> refresh", calls == [1], calls)
    co._loop_task = asyncio.create_task(asyncio.sleep(1))
    co.notify_changed()
    await asyncio.sleep(0.05)
    check("no extra refresh while the reconcile loop runs", calls == [1], calls)
    co._loop_task.cancel()
    print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
