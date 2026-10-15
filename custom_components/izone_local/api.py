"""Minimal async client for the iZone local (LAN) API v2.

Protocol summary (https://developer.izone.com.au/docs/getting-started/local/):
- Discovery: UDP broadcast "IASD" to 255.255.255.255:12107. Each bridge replies
  "ASPort_12107,Mac_<9-digit id>,IP_<address>,<capabilities...>".
- Query:   POST http://<ip>/iZoneRequestV2  {"iZoneV2Request": {"Type": t, "No": n, "No1": 0}}
           Type 1 = AC system, 2 = zone n, 3 = schedule n.
- Command: POST http://<ip>/iZoneCommandV2  {"<Command>": value}  -> "OK"
Temperatures are integers in hundredths of a degree (2350 == 23.5 C).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import socket
from dataclasses import dataclass, field
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

DISCOVERY_PORT = 12107
DISCOVERY_MESSAGE = b"IASD"
REQUEST_TIMEOUT = 10
# Back-off (seconds) after each "{BUSY}" reply; the last attempt's reply is final.
BUSY_RETRY_DELAYS = (0.5, 1, 2, 3, 0)

QUERY_SYSTEM = 1
QUERY_ZONE = 2
QUERY_SCHEDULE = 3

# ZoneMode_e values used by the client itself (full list in const.py).
ZONE_MODE_AUTO = 3
ZONE_MODE_OVERRIDE = 4


class IZoneError(Exception):
    """Base error for the iZone client."""


class IZoneConnectionError(IZoneError):
    """The bridge could not be reached or returned garbage."""


class IZoneCommandError(IZoneError):
    """The bridge rejected a command."""


@dataclass
class DiscoveredBridge:
    """A bridge that answered the UDP discovery broadcast."""

    uid: str
    host: str
    capabilities: list[str] = field(default_factory=list)

    @property
    def supports_v2(self) -> bool:
        return "iZoneV2" in self.capabilities


_MAC_RE = re.compile(r"Mac_(\w+)")
_IP_RE = re.compile(r"IP_([0-9.]+)")


def parse_discovery_reply(data: bytes) -> DiscoveredBridge | None:
    """Parse one discovery reply, or return None if it isn't an iZone bridge."""
    text = data.decode("ascii", errors="ignore").strip()
    if not text.startswith("ASPort_"):
        return None
    mac = _MAC_RE.search(text)
    ip = _IP_RE.search(text)
    if not mac or not ip:
        return None
    tokens = [t.strip() for t in text.split(",")]
    caps = [
        t
        for t in tokens
        if t and t != "X" and not t.startswith(("ASPort_", "Mac_", "IP_"))
    ]
    return DiscoveredBridge(uid=mac.group(1), host=ip.group(1), capabilities=caps)


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: dict[str, DiscoveredBridge] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        bridge = parse_discovery_reply(data)
        if bridge:
            self.found[bridge.uid] = bridge


async def async_discover(
    timeout: float = 3.0, target: str = "255.255.255.255"
) -> list[DiscoveredBridge]:
    """Send a discovery request and collect replies for `timeout` seconds.

    The default broadcast only reaches bridges on the same subnet; pass a
    bridge's IP as `target` to probe it directly (it replies to the sender).
    """
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    sock.bind(("", 0))
    transport, protocol = await loop.create_datagram_endpoint(
        _DiscoveryProtocol, sock=sock
    )
    try:
        # UDP is lossy; send a few times.
        for _ in range(3):
            transport.sendto(DISCOVERY_MESSAGE, (target, DISCOVERY_PORT))
            await asyncio.sleep(timeout / 3)
    finally:
        transport.close()
    return list(protocol.found.values())


class IZoneClient:
    """HTTP client for one iZone bridge.

    The bridge is a small embedded device that copes badly with concurrent
    connections, so every request is serialised through a lock.
    """

    def __init__(self, session: aiohttp.ClientSession, host: str) -> None:
        self._session = session
        self.host = host
        self._lock = asyncio.Lock()

    async def _post(self, path: str, payload: dict[str, Any]) -> str:
        url = f"http://{self.host}/{path}"
        async with self._lock:
            try:
                async with (
                    asyncio.timeout(REQUEST_TIMEOUT),
                    self._session.post(url, json=payload) as resp,
                ):
                    status = resp.status
                    # The bridge sends no Content-Type and some fields
                    # (e.g. LockCode) contain raw 0xFF bytes, so
                    # resp.text() would raise UnicodeDecodeError.
                    text = (await resp.read()).decode("utf-8", errors="replace")
            except (aiohttp.ClientError, TimeoutError) as err:
                raise IZoneConnectionError(f"Error talking to {url}: {err}") from err
        if status != 200:
            raise IZoneConnectionError(f"{url} returned HTTP {status}: {text}")
        return text

    async def query(self, qtype: int, number: int = 0) -> dict[str, Any]:
        payload = {"iZoneV2Request": {"Type": qtype, "No": number, "No1": 0}}
        text = await self._post("iZoneRequestV2", payload)
        try:
            data = json.loads(text)
        except ValueError as err:
            raise IZoneConnectionError(f"Invalid JSON from bridge: {text!r}") from err
        if not isinstance(data, dict):
            raise IZoneConnectionError(f"Unexpected reply from bridge: {text!r}")
        return data

    async def get_system(self) -> tuple[str, dict[str, Any]]:
        """Return (bridge uid, SystemV2 datagram)."""
        data = await self.query(QUERY_SYSTEM)
        system = data.get("SystemV2")
        if not isinstance(system, dict):
            raise IZoneConnectionError(f"No SystemV2 in reply: {data}")
        return str(data.get("AirStreamDeviceUId", "")), system

    async def get_zone(self, index: int) -> dict[str, Any]:
        data = await self.query(QUERY_ZONE, index)
        zone = data.get("ZonesV2")
        if isinstance(zone, dict):
            return zone
        # The docs don't name the zone wrapper key; fall back to the first
        # dict that looks like a zone datagram.
        for value in data.values():
            if isinstance(value, dict) and "Index" in value:
                return value
        raise IZoneConnectionError(f"No zone datagram in reply: {data}")

    async def command(self, name: str, value: Any) -> None:
        """Send one command. The bridge replies "{OK}", or "{BUSY}" while it is
        still processing a previous command, in which case we back off and retry.
        """
        for delay in BUSY_RETRY_DELAYS:
            text = await self._post("iZoneCommandV2", {name: value})
            reply = text.strip().strip('"{}').upper()
            if reply == "OK":
                return
            if reply != "BUSY":
                break
            _LOGGER.debug("Bridge busy for %s, retrying in %ss", name, delay)
            await asyncio.sleep(delay)
        raise IZoneCommandError(f"{name}={value!r} rejected: {text!r}")

    # Convenience wrappers ------------------------------------------------

    async def set_power(self, on: bool) -> None:
        await self.command("SysOn", 1 if on else 0)

    async def set_mode(self, mode: int) -> None:
        await self.command("SysMode", mode)

    async def set_fan(self, fan: int) -> None:
        await self.command("SysFan", fan)

    async def set_setpoint(self, celsius: float) -> None:
        # Note: the datagram field is "Setpoint" but the command is "SysSetpoint".
        await self.command("SysSetpoint", to_centi(celsius))

    async def set_isave(self, on: bool) -> None:
        await self.command("iSave", 1 if on else 0)

    async def set_sleep_timer(self, minutes: int) -> None:
        await self.command("SysSleepTimer", minutes)

    async def set_zone_mode(self, index: int, mode: int) -> None:
        await self.command("ZoneMode", {"Index": index, "Mode": mode})

    async def set_zone_setpoint(self, index: int, celsius: float) -> None:
        # Only takes effect while the zone is in Auto; on a closed/open zone the
        # bridge switches it to Auto and drops the value. Callers that don't
        # know the mode should use ensure_zone_setpoint.
        await self.command(
            "ZoneSetpoint", {"Index": index, "Setpoint": to_centi(celsius)}
        )

    async def ensure_zone_setpoint(
        self, index: int, celsius: float, current_mode: int | None
    ) -> None:
        """Set a zone's temperature, switching it to Auto first if needed."""
        if current_mode not in (ZONE_MODE_AUTO, ZONE_MODE_OVERRIDE):
            await self.set_zone_mode(index, ZONE_MODE_AUTO)
        await self.set_zone_setpoint(index, celsius)

    async def set_zone_max_air(self, index: int, percent: int) -> None:
        await self.command("ZoneMaxAir", {"Index": index, "MaxAir": percent})

    async def set_zone_min_air(self, index: int, percent: int) -> None:
        await self.command("ZoneMinAir", {"Index": index, "MinAir": percent})


def to_centi(celsius: float) -> int:
    """15.0-30.0 C in 0.5 steps -> 1500-3000."""
    # floor(x + 0.5), not round(): round() is banker's rounding (22.25 -> 22.0
    # but 22.75 -> 23.0).
    value = math.floor(celsius * 2 + 0.5) * 50
    return max(1500, min(3000, value))


def from_centi(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return value / 100
