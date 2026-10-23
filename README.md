# iZone Local (API v2) for Home Assistant

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=zafayar&repository=ha-izone-local&category=integration)

A custom integration for iZone ducted air conditioning that talks to the bridge
over your LAN with the [iZone local API v2](https://developer.izone.com.au/docs/getting-started/local/).
It does not use the cloud.

> Home Assistant has a built-in `izone` integration (it uses the `pizone`
> library). This one is separate. Its domain is `izone_local`, so both can be
> installed. Don't add the same bridge to both.

## Supported devices

- **Supported:** iZone ducted systems (also sold as Rinnai and other brands,
  see [Names](#names)) whose bridge supports the v2 local API. The bridge
  says so when it answers discovery: its capabilities include `iZoneV2`.
- **Tested with:** a Rinnai-branded system with a COCB-2F5 hub (firmware
  4.36), a C225 board and a CCTSL wall controller, five zones including a
  bypass zone. Details under [Testing status](#testing-status).
- **Not supported:** bridges without the v2 API (use Home Assistant's
  built-in `izone` integration), and the split, VRF, iLight, iDrate and
  iPower parts of a system (see [Known limitations](#known-limitations)).

## What you can do with it

- Run each room from its own zone entity: turning on a zone starts the AC,
  and turning off the last one stops it.
- Use the zone temperatures and modes in automations, for example warming
  the bedrooms before bedtime or closing zones when everyone leaves.
- Run the favourites saved in the iZone app as scenes, and change what they
  store from Home Assistant.
- Set the sleep timer, as on the wall panel, for example to run the AC for
  an hour at bedtime.
- Get told when the filter is due a clean, and keep an eye on wireless zone
  sensors' batteries and signal.
- Recover a bridge that stops responding, with the Bridge problem sensor and
  a smart plug.

## What you get

| Entity | Notes |
| --- | --- |
| AC climate | Off / Cool / Heat / Fan only / Dry / Heat-Cool (iZone "Auto"); fan speeds as the unit reports them (`FanAutoEn`, `FanAutoType`): low, medium and high, plus auto and top where the fan has them. When a control zone is set (`CtrlZone`), the target and current temperature come from that zone. |
| Zone climate (one per zone, on its own device named after the zone, which suggests a room of the same name) | Off = damper closed (or the AC is off), Fan only = open, Auto = temperature-controlled (only on zones of type Auto). Setting a temperature on a closed or open zone switches it to Auto. Zones also switch the AC on and off; see below. Bypass zones get no climate entity, because the system controls them itself. |
| Sensors | Supply air temperature, return air temperature, AC error code, AC run hours, control zone (the zone the unit is following, or `unit`), when the sleep timer ends, firmware version of each module (hub, wall controller, boards; off by default, as the hub's version is also the AC device's software version); zone temperature and damper position, plus battery and signal strength for wireless zone sensors. |
| Binary sensors | Filter warning, AC fault, bridge problem; zone sensor fault and damper fault. |
| Select | Sleep timer (see below). |
| Switch | iSave (only if the system has iSave enabled). |
| Scenes | One per favourite saved on the controller (see below). |
| Numbers | Each zone's minimum and maximum airflow (settings, off by default). |

The integration has one action, `izone_local.update_favourite` (see
[Favourites](#favourites)). It adds no triggers or conditions of its own; use
the entities' states and attributes in automations.

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

### Sleep timer

The **Sleep timer** select sets the controller's own timer, which turns the
AC off when it runs out. It offers the wall panel's steps: off, 30 minutes,
1, 2, 4, 6, 8 and 12 hours.

- It runs on the controller and shows on the wall panel, so it still turns
  the AC off if Home Assistant is down. A timer set on the panel shows here
  too.
- While it runs, the select shows the time left rounded up to a step (5 h
  59 min left shows as 6 hours), and its `minutes_left` attribute has the
  minutes. The **Sleep timer ends** sensor has the time it will go off.
- Choosing a time while the AC is off turns the AC on for that long, as the
  panel's button does. Choosing a time while a timer runs replaces it.
- **Off** cancels the timer and leaves the AC running. Turning the AC off
  also ends the timer.

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

To change what a favourite stores, use the **iZone Local: Update favourite**
action (`izone_local.update_favourite`) on its scene. It changes one zone
(by name or number) and/or the fan and AC mode; `unchanged` makes the
favourite leave the fan or mode alone. The change is saved on the
controller, so the app and wall controller see it too, and nothing happens
until the favourite is next run:

```yaml
action: izone_local.update_favourite
target:
  entity_id: scene.evening
data:
  zone: Master
  temperature: 21.5   # a temperature alone sets the zone to auto
  fan: unchanged
  mode: heat
```

The controller's replies to these edits aren't reliable, so each one is
checked by reading the favourite back, and sent again (2 s apart) if it
didn't stick.

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
raw system, zone and favourite data, firmware versions and any pending
changes. The bridge's password and lock code, its IP address, its ID and the
hub's serial number are removed.

If the bridge answers with every field zeroed (it does now and then while the
unit runs), the last good state is kept for up to 5 minutes rather than
showing the AC as off; changes being applied wait for real data.

### Notifications

Home Assistant shows a notification:

- **when the filter is due a clean**, while the controller's filter warning is
  on. It's raised once per warning (dismissing it won't bring it back every
  poll) and clears itself when the warning goes, after the reminder is reset
  on the wall controller or in the iZone app.
- **when firmware changes**, listing which modules changed (checked every 6
  hours).

Turn them off with **Configure** on the integration's entry.

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

**Before you start:**

- Home Assistant 2025.2 or later. (The integration's icon shows from 2026.3;
  earlier versions show a placeholder.)
- The iZone bridge on the same network as Home Assistant, with the v2 local
  API. Give it a fixed address (a DHCP reservation in your router).
- For discovery and instant updates, Home Assistant must receive UDP
  broadcasts from the bridge's subnet (ports 12107 and 7005). In Docker, that
  needs host networking. Without them you can still enter the address, and
  changes made elsewhere show at the next poll.

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

Once the integration is installed, a bridge found on the network also shows
under **Discovered** in Devices & services; click **Add** to set it up.

### Setup fields

- **Bridge:** the v2 bridges found by a broadcast (`IASD` on UDP 12107).
  Choose **Enter an IP address** if yours isn't listed.
- **IP address:** asked for when no bridge is found (for example, Home
  Assistant is on another VLAN), or when you choose to enter it.

The bridge is recognised by its ID, so it can't be added twice. If its
address changes, discovery updates the entry on its own (when broadcasts
reach Home Assistant); otherwise use **Reconfigure** on the entry and enter
the new address.

### Options

**Configure** on the integration's entry has one option:

- **Show notifications** (on by default): notify when the filter is due a
  clean and when firmware changes. See [Notifications](#notifications).

### Removing it

1. Go to **Settings → Devices & services → iZone Local**, open the ⋮ menu on
   the entry and choose **Delete**. Its devices and entities go with it.
2. To uninstall it too, remove **iZone Local (API v2)** in HACS (or delete
   `config/custom_components/izone_local`), then restart Home Assistant.

Nothing is changed on the bridge or the controller, apart from any
favourites you edited.

## How data is updated

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
- **Less often:** firmware versions every 6 hours; favourites when the
  integration starts (reload it after changing them in the app); a discovery
  broadcast when Home Assistant starts and every 30 minutes, which also picks
  up a bridge's new address.
- **Zones added or removed** on the controller are picked up at the next
  poll: the entry reloads, and devices for zones that are gone are removed.

## Known limitations

- Not supported: schedules (timed favourites), renaming favourites, split
  and VRF systems, iLight, iDrate and iPower.
- The sleep timer only takes the panel's steps. The controller counts
  whole minutes, so the end time is good to about a minute.
- Favourites are read when the integration starts. After adding or renaming
  one in the app, reload the integration.
- Changes made at the wall controller or in the app show within seconds only
  if the bridge's broadcasts reach Home Assistant; otherwise within 15 s.
- The controller can take up to a minute to finish a favourite, and ignores
  zone commands meanwhile.
- One bridge per entry; add each bridge as its own entry.

## Examples

Warm the bedrooms on cold evenings:

```yaml
automation:
  - alias: Warm the bedrooms at 8:30 pm
    triggers:
      - trigger: time
        at: "20:30:00"
    conditions:
      - condition: numeric_state
        entity_id: climate.master
        attribute: current_temperature
        below: 18
    actions:
      - action: climate.set_temperature
        target: { entity_id: [climate.master, climate.kids] }
        data: { temperature: 21, hvac_mode: auto }
```

The AC turns on with the first zone, in whatever mode it was last in.

Close every zone when the house is empty (the AC turns off with the last
one):

```yaml
automation:
  - alias: Everyone out, zones off
    triggers:
      - trigger: state
        entity_id: zone.home
        to: "0"
        for: "00:10:00"
    actions:
      - action: climate.turn_off
        target: { entity_id: [climate.living, climate.master, climate.kids] }
```

Switch a favourite to heating for winter:

```yaml
automation:
  - alias: Evening favourite heats from May
    triggers:
      - trigger: template
        value_template: "{{ now().month == 5 and now().day == 1 }}"
    actions:
      - action: izone_local.update_favourite
        target: { entity_id: scene.evening }
        data: { mode: heat }
```

Run the AC for an hour at bedtime (this turns it on if it's off; the entity
ID follows your AC device's name):

```yaml
automation:
  - alias: AC for the first hour of the night
    triggers:
      - trigger: time
        at: "22:00:00"
    actions:
      - action: select.select_option
        target: { entity_id: select.ac_sleep_timer }
        data: { option: 1h }
```

Warn when a wireless zone sensor's battery is running out:

```yaml
automation:
  - alias: iZone sensor battery low
    triggers:
      - trigger: state
        entity_id: sensor.master_sensor_battery
        to: empty
    actions:
      - action: persistent_notification.create
        data:
          message: The Master zone sensor needs new batteries.
```

The power-cycle example for an unresponsive bridge is under
[Bridge health and diagnostics](#bridge-health-and-diagnostics).

## Troubleshooting

- **The bridge isn't found during setup.** Discovery uses UDP broadcasts,
  which don't cross VLANs or reach Docker without host networking. Enter the
  bridge's IP address instead. If that fails too, check the address, and that
  the bridge has the v2 API (its discovery reply includes `iZoneV2`; older
  bridges only have v1).
- **Changes made at the wall controller take 15 s to show.** The bridge's
  change broadcasts (UDP 7005) aren't reaching Home Assistant; see above.
  Polling still picks them up.
- **Entities are unavailable.** Home Assistant can't reach the bridge. Check
  that its address hasn't changed (use **Reconfigure** if it has) and that
  it's powered. The **Bridge problem** sensor turns on after repeated
  failures, and the log says what failed.
- **A change shows `failed`, or the action raises an error.** The bridge
  accepted the command 3 times without applying it, or nothing was confirmed
  within 30 s. The wall controller is often just busy; try again. A zone
  whose sensor has dropped out can't take Auto or a temperature; such a
  change waits with `waiting for sensor` instead.
- **A favourite doesn't fully apply.** The controller can take up to a minute;
  the integration sends what's still missing after 60 s.
- **Editing a favourite raises "didn't keep the change".** The controller
  didn't store it after 3 tries. Try again, or check the favourite in the
  iZone app.

For anything else, turn on debug logging (see below) and download the
diagnostics from the AC's device page.

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
- The sleep timer (`SysSleepTimer`) takes 0 or a multiple of 30 up to 720
  minutes, and answers `{ERROR}` to anything else. `SleepTimerM` is the
  minutes left, counting down; it shows a new timer 1–10 s after the
  command, usually a minute less, so the select takes about 10 s to confirm. `SleepTimer` only echoes the last value set: it can
  lag, and isn't cleared when the timer is. 0 cancels the timer (this
  integration's first tests read `SleepTimer` and wrongly concluded it
  couldn't be cancelled). Setting a timer while the AC is off turns it on.
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
Home Assistant the integration uses, so all they need is `aiohttp` and
`voluptuous`:

```bash
python -m unittest discover -s tests -v
```

Each scenario in `tests/scenarios/` can also be run on its own and prints a
line per check. `tests/live/` has scripts that drive a real bridge and then
restore it (see its README). CI (`.github/workflows/ci.yml`) runs the tests
with coverage (it fails below 95%), ruff (settings in `pyproject.toml`; run
`ruff check custom_components tests`), Home Assistant's hassfest validation
and the HACS validation.

How the integration measures up to Home Assistant's
[integration quality scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
is recorded rule by rule in
[`quality_scale.yaml`](custom_components/izone_local/quality_scale.yaml).

The HACS validation also checks the GitHub repository itself: it needs a
description, topics (for example `home-assistant`, `hacs`, `izone`) and
issues turned on.

To release: set `version` in `manifest.json`, commit, then publish a GitHub
release tagged with the same version (for example `v0.5.0`).
