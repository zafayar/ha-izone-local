"""Snapshot and robust restore of the bridge's AC + zone state (for live tests)."""
import asyncio


async def snap(c):
    _, s = await c.get_system()
    zones = [await c.get_zone(i) for i in range(s["NoOfZones"])]
    return ((s["SysOn"], s["SysMode"], s["SysFan"]),
            [(z["Mode"], z["Setpoint"], bool(z.get("Bypass"))) for z in zones])


async def _wait(c, i, field, value, tries=20):
    for _ in range(tries):
        await asyncio.sleep(0.5)
        if (await c.get_zone(i))[field] == value:
            return True
    return False


async def restore(c, orig):
    """Put every zone's mode and stored setpoint, then the AC, back."""
    (on, mode, fan), zones = orig
    for i, (zm, zs, bypass) in enumerate(zones):
        if bypass:
            continue
        z = await c.get_zone(i)
        if z["Setpoint"] != zs:
            # A setpoint only sticks in Auto: switch, set, then put the mode back.
            if z["Mode"] not in (3, 4):
                await c.set_zone_mode(i, 3)
                await _wait(c, i, "Mode", 3)
            await c.set_zone_setpoint(i, zs / 100)
            await _wait(c, i, "Setpoint", zs)
            z = await c.get_zone(i)
        if z["Mode"] != zm and zm in (1, 2, 3):
            await c.set_zone_mode(i, zm)
            await _wait(c, i, "Mode", zm)
    _, sd = await c.get_system()
    if sd["SysMode"] != mode:
        await c.set_mode(mode)
    if sd["SysFan"] != fan:
        await c.set_fan(fan)
    if sd["SysOn"] != on:
        await c.set_power(bool(on))
    await asyncio.sleep(3)
    now = await snap(c)
    return now == orig, now
