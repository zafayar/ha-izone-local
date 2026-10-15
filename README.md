# iZone Local (API v2) for Home Assistant

A custom integration for iZone ducted air conditioning that talks to the bridge
over your LAN with the [iZone local API v2](https://developer.izone.com.au/docs/getting-started/local/).
It does not use the cloud.

> Home Assistant has a built-in `izone` integration (it uses the `pizone`
> library). This one is separate. Its domain is `izone_local`, so both can be
> installed. Don't add the same bridge to both.

## What you get

| Entity | Notes |
| --- | --- |
| AC climate | Off / Cool / Heat / Fan only / Dry / Heat-Cool (iZone "Auto"); fan low, medium, high, auto, top. When a control zone is set (`CtrlZone`), the target and current temperature come from that zone. |
| Zone climate (one per zone, on its own device named after the zone) | Off = damper closed, Fan only = open, Auto = temperature-controlled (only on zones of type Auto). Setting a temperature on a closed or open zone switches it to Auto. Bypass zones get no climate entity, because the system controls them itself. |
| Sensors | Supply air temperature, return air temperature, AC error code; zone temperature and damper position. |
| Binary sensors | Filter warning, AC fault; zone sensor fault and damper fault. |
| Switch | iSave (only if the system has iSave enabled). |

## Install

1. Copy `custom_components/izone_local` into your Home Assistant `config/custom_components/` folder. Or add this repo to HACS as a custom repository (Integration).
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → iZone Local**.

Setup broadcasts `IASD` on UDP port 12107 to find bridges. If nothing answers
(for example, Home Assistant is in Docker without host networking, or on
another VLAN), you can type the bridge's IP address. Give the bridge a DHCP
reservation. If its IP address changes, run setup again: the integration
recognises the bridge by its ID and updates the existing entry.

## How it works

- **Polling:** every 15 seconds the integration makes one `Type 1` request
  (system) and one `Type 2` request for each zone, all to
  `POST /iZoneRequestV2`. Requests are sent one at a time because the bridge is
  a small embedded device.
- **Commands:** sent to `POST /iZoneCommandV2` (for example `{"SysOn": 1}` or
  `{"ZoneMode": {"Index": 0, "Mode": 3}}`). The bridge replies `OK`, and the
  integration polls again straight away.
- **Temperatures:** the API sends whole numbers in hundredths of a degree
  (2350 = 23.5 °C). Setpoints are limited to 15–30 °C in 0.5° steps.

## Not yet supported

Schedules (`Type 3`), zone min/max airflow (the client methods exist, but there
are no entities for them), sleep timer, split and VRF systems, iLight, iDrate
and iPower.

## Testing status

Discovery and every read request have been tested against a real bridge
(capabilities `iZoneV2,iLight,iDrate,iPower,Split,Monitor,HotWater,VRF`, five
zones including a bypass zone). Things learnt from that test:

- The zone reply is wrapped in `ZonesV2`.
- The bridge sends no `Content-Type`, and `LockCode` contains raw `0xFF`
  bytes, so replies are decoded as UTF-8 with errors replaced.
- The bridge replies to `IASD` at the sender's port, whether the request was
  broadcast or sent to its IP address. A broadcast only reaches bridges on the
  same subnet as Home Assistant. On other subnets, enter the IP address instead.
- A zone with no sensor (`SensType: 255`, e.g. a bypass) reports
  `SensorFault: 1` and `Temp: 0`, so no temperature or sensor-fault entities
  are created for it.

Control commands tested on the same bridge: power, mode, fan, unit setpoint,
sleep timer, zone mode and zone setpoint. More findings:

- The bridge replies `{OK}`, or `{BUSY}` while it is still processing an
  earlier command. A `{BUSY}` reply is retried with back-off. An unknown
  command gets an empty reply.
- A zone setpoint only takes effect while the zone is in Auto. Sent to a closed
  or open zone, the command switches the zone to Auto and drops the
  temperature. The integration therefore switches the zone to Auto first.
- Back-to-back commands are unreliable. Right after a zone mode change, the
  bridge replies `{BUSY}`. About 0.5 s later it replies `{OK}` but still drops
  the command. From 1 s on, commands work. Commands are therefore sent at least
  1.5 s apart, and zone setpoints are read back to confirm they were applied.
- Each zone was controlled through its own entity, and the other zones were
  confirmed unchanged.
- While a zone is the control zone (`CtrlZone` 0–13), the unit's `Setpoint`
  shows that zone's setpoint and `SysSetpoint` is ignored. The AC entity then
  sends its temperature to that zone instead. `CtrlZone` can't be set directly;
  the bridge picks it.

If you see errors, turn on debug logging and open an issue with the raw reply:

```yaml
logger:
  logs:
    custom_components.izone_local: debug
```
