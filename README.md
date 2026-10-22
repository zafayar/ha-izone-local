# iZone Local (API v2) for Home Assistant

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=zafayar&repository=ha-izone-local&category=integration)

A custom integration for iZone ducted air conditioning that talks to the bridge
over your LAN with the [iZone local API v2](https://developer.izone.com.au/docs/getting-started/local/).
It does not use the cloud.

> Home Assistant has a built-in `izone` integration (it uses the `pizone`
> library). This one is separate. Its domain is `izone_local`, so both can be
> installed. Don't add the same bridge to both.

## What you get

| Entity | Notes |
| --- | --- |
| AC climate | Off / Cool / Heat / Fan only / Dry / Heat-Cool (iZone "Auto"); fan speeds as the unit reports them (`FanAutoEn`, `FanAutoType`): low, medium and high, plus auto and top where the fan has them. When a control zone is set (`CtrlZone`), the target and current temperature come from that zone. |
| Zone climate (one per zone, on its own device named after the zone, which suggests a room of the same name) | Off = damper closed (or the AC is off), Fan only = open, Auto = temperature-controlled (only on zones of type Auto). Setting a temperature on a closed or open zone switches it to Auto. Zones also switch the AC on and off; see below. Bypass zones get no climate entity, because the system controls them itself. |
| Sensors | Supply air temperature, return air temperature, AC error code, AC run hours, control zone (the zone the unit is following, or `unit`), firmware version of each module (hub, wall controller, boards; read every 6 hours, and the hub's is also the AC device's software version); zone temperature and damper position, plus battery and signal strength for wireless zone sensors. |
| Binary sensors | Filter warning, AC fault, bridge problem; zone sensor fault and damper fault. |
| Switch | iSave (only if the system has iSave enabled). |
| Scenes | One per favourite saved on the controller (see below). |
| Numbers | Each zone's minimum and maximum airflow (settings, off by default). |

### The AC follows the zones

The AC's power is tied to the zones, so you can run the house from the zone
entities alone:

- **Turning a zone on** (Fan only, Auto, or setting its temperature) turns the
  AC on if it is off. Any other zones left open from before are closed first,
  so only the zone you chose comes on.
- **Turning the last zone off** turns the AC off. Before deciding, the
  integration re-reads every zone, in case one was opened from the iZone app
  or wall controller.
- **While the AC is off, every zone reports Off**, whatever its damper setting
  on the bridge.
- **The AC's own on/off still works.** Turning it off leaves the zones as they
  are on the bridge, so turning it back on resumes the same zones.

Bypass and constant zones are opened by the system itself, so they don't count
as "on". Changes made outside Home Assistant (the iZone app, the wall
controller) are not overridden: turning the AC on there with every zone closed
leaves it on.

### How changes are applied

Every action states the result you want (for example "Master in Auto, the AC
on"), and one loop makes the bridge match it:

1. **Show it.** The entities change at once, before any command is sent.
2. **Sync.** Read the system, plus the zones the change touches (every zone
   when the AC might turn off). That takes 15–60 ms.
3. **Compare.** Drop anything the bridge already shows.
4. **Send the next command**, in a safe order: zone closes and the AC mode
   first, then zone modes, then setpoints (only once the zone shows Auto, as
   the bridge drops them otherwise), then AC power. Commands are 1 s apart.
5. **Repeat** every 0.4 s until the bridge shows everything. A command the
   bridge accepts but doesn't apply is re-sent after 2.5 s, up to 3 times.

The action finishes when the bridge shows the change, so automations that
call it wait until the AC has actually responded. If it can't be applied, the
entity goes back to the bridge's state and the action raises an error.

Two cases are handled specially:

- **Changed elsewhere.** If a value moves somewhere neither the bridge nor
  Home Assistant was heading (someone used the wall controller or the iZone
  app), that newer change wins: the integration stops re-sending, without an
  error.
- **A zone sensor has dropped out.** The controller then reports the zone as
  closed and remembers its real mode, and it can't take Auto or a temperature
  until the sensor is back. Such a change waits for the sensor (up to 12 h)
  instead of failing, with the status `waiting for sensor`, and is applied
  when the sensor returns. The zone shows no temperature meanwhile, and it
  doesn't count as "on" when deciding whether the AC can turn off.

Measured on a real bridge: turning on a zone while the AC is on is confirmed
in about 0.7 s. Turning on a zone while the AC is off (one other zone to close
first, then the AC on) takes about 3.7 s.

### Status

Every climate entity (and the iSave switch) has a `status` attribute and a
matching icon:

| Status | Icon | Meaning |
| --- | --- | --- |
| `ready` | normal | Nothing waiting. |
| `queued` | `mdi:timer-sand` | Waiting for other commands to go first. |
| `sending (1 of 2)` | `mdi:sync` | Commands for this change are going out. |
| `confirming` | `mdi:sync` | Sent; waiting for the bridge to report it. |
| `failed` | `mdi:alert-circle-outline` | The bridge didn't apply it (shown for 10 s). |
| `waiting for sensor` | `mdi:thermometer-off` | The zone's sensor is out; applied when it's back. |

A change to the AC's power changes every zone's state, so it shows on every
zone. There's also a `pending: true` attribute while anything is unconfirmed.

To show the status under the entity's name on a tile card:

```yaml
type: tile
entity: climate.master
state_content:
  - state
  - status
```

### Temperatures and modes

- The setpoint range is 15–30 °C in 0.5° steps, or the controller's eco-lock
  limits while the eco lock is on. Setpoints outside the range are brought
  within it.
- `climate.set_temperature` can include an `hvac_mode`. On the AC both are
  applied together. A zone set to `off` or `fan_only` that way skips the
  temperature (a zone only holds one in `auto`, and the controller would
  switch it back to `auto` to take it).
- Heat, cool and dry are whole-unit modes, set on the AC. Asking a zone for
  one gives a clear error.

### Favourites

Each favourite saved in the iZone app (AC mode, fan and per-zone settings)
is a scene, named after the favourite. Slots still called "Favourite 1" to
"Favourite 9" are added but disabled; enable them if you use them. A scene's
attributes show what it does, for example `zones: {Master: auto 22.0,
Living: close}`. Favourites are read when the integration starts, so reload
it after renaming or adding one in the app.

Activating a scene sends one `FavouriteSet` command, and returns once the
controller accepts it. The controller turns the AC on within a second or two,
but can take up to a minute to set every zone, and ignores separate zone
commands meanwhile. So the integration watches, and after 60 s sends zone by
zone whatever is still missing. The entities show the progress meanwhile.

If the favourite needs a zone whose sensor is out, the controller refuses the
whole favourite. The integration then applies it zone by zone instead, and
that zone waits for its sensor.

### Bridge health and diagnostics

The **Bridge problem** binary sensor turns on after 3 failures within 5
minutes (a change the bridge didn't apply, or a failed read), with the count
in its `recent_failures` attribute. It stays available while the bridge is
unreachable, so an automation can act on it, for example power-cycling the
bridge with a smart plug:

```yaml
automation:
  - alias: Power-cycle the iZone bridge when it keeps failing
    triggers:
      - trigger: state
        entity_id: binary_sensor.ghar_bridge_problem
        to: "on"
        for: "00:02:00"
    actions:
      - action: switch.turn_off
        target: { entity_id: switch.izone_bridge_power }
      - delay: "00:00:20"
      - action: switch.turn_on
        target: { entity_id: switch.izone_bridge_power }
```

For issue reports, **Download diagnostics** on the AC's device page gives the
raw system, zone and favourite data and any pending changes. The bridge's
password and lock code, its IP address and its ID are removed.

### Names

Names come from the system itself:

- **AC device name:** the name shown on the wall controller (`Tag1`, e.g.
  "Ghar"), or the brand if that is empty.
- **Manufacturer:** the brand the system is sold under (`OemMake`, e.g.
  "Rinnai").
- **AC model:** the detected unit (`AcUnitBrand`, e.g. "Rinnai 20kW").
- **Integration entry:** brand plus name, e.g. "Rinnai Ghar".

The brand and unit lists are from `iZone_JSON_datastrings.h` v1.41. Unknown
values fall back to "iZone".

## Install

**With HACS (recommended):**

1. Click the **Open in HACS** badge above, or in HACS open the ⋮ menu →
   **Custom repositories**, add `https://github.com/zafayar/ha-izone-local`
   with the type **Integration**.
2. Install **iZone Local (API v2)**, then restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → iZone Local**.

HACS offers updates as new releases are published.

**By hand:** copy `custom_components/izone_local` into your Home Assistant
`config/custom_components/` folder, restart, then add the integration as in
step 3.

Home Assistant 2025.2 or later is needed. The integration's icon shows from
2026.3 (earlier versions show a placeholder).

Setup broadcasts `IASD` on UDP port 12107 to find bridges. If nothing answers
(for example, Home Assistant is in Docker without host networking, or on
another VLAN), you can type the bridge's IP address. Give the bridge a DHCP
reservation. If its IP address changes, run setup again: the integration
recognises the bridge by its ID and updates the existing entry.

## How it works

- **Polling:** every 15 seconds the integration makes one `Type 1` request
  (system) and one `Type 2` request for each zone, all to
  `POST /iZoneRequestV2`. Requests are sent one at a time because the bridge is
  a small embedded device. A dropped request or server error is retried twice
  before an entity goes unavailable.
- **Change broadcasts:** a few seconds after any change (including ones made
  at the wall controller or in the iZone app) the bridge broadcasts
  `iZoneChanged_*` on UDP 7005, and the integration reads it straight away.
  Broadcasts only reach Home Assistant on the bridge's subnet (and, in Docker,
  with host networking); otherwise such changes show at the next poll.
- **Commands:** sent to `POST /iZoneCommandV2` (for example `{"SysOn": 1}` or
  `{"ZoneMode": {"Index": 0, "Mode": 3}}`). The bridge replies `OK`, and
  reports the change 0–2 s later. See "How changes are applied" above.
- **Temperatures:** the API sends whole numbers in hundredths of a degree
  (2350 = 23.5 °C). Setpoints are limited to 15–30 °C in 0.5° steps.

## Not yet supported

Schedules (timed favourites), editing favourites, split and VRF systems,
iLight, iDrate and iPower.

The sleep timer is left out on purpose: on a real controller, setting it
turns the AC on, and setting it back to 0 doesn't cancel it (only turning the
AC off does, about 10 s later), so it doesn't work as a setting.

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
  the command. From 1 s on, commands work. Commands are therefore sent 1 s
  apart, and every change is read back and re-sent if it was dropped.
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

## Development

The tests run against a fake bridge, with small stand-ins for the parts of
Home Assistant the integration uses, so all they need is `aiohttp`:

```bash
python -m unittest discover -s tests -v
```

Each scenario in `tests/scenarios/` can also be run on its own and prints a
line per check. `tests/live/` has scripts that drive a real bridge and then
restore it (see its README). CI (`.github/workflows/ci.yml`) runs the tests,
Home Assistant's hassfest validation and the HACS validation.

The HACS validation also checks the GitHub repository itself: it needs a
description, topics (for example `home-assistant`, `hacs`, `izone`) and
issues turned on.

To release: set `version` in `manifest.json`, commit, then publish a GitHub
release tagged with the same version (for example `v0.5.0`).
