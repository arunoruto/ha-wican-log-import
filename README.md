# WiCAN Log Import

> [!WARNING]
> **Untested on real hardware (0.0.x).** The integration passes its automated tests with
> generated log files, but it has not yet imported a log from a real WiCAN. The log format
> is taken from [wican-cli](https://github.com/philipkocanda/wican-cli) and may differ on your
> firmware. Try `wican_log_import.dry_run` first and report what you find. Version 0.1.0
> will follow once imports from a real device are confirmed.

Imports the SD card trip logs of a **WiCAN Pro** OBD-II dongle into Home Assistant's own
long-term statistics. Data recorded while the car was away from home Wi-Fi then shows up in
your history graphs as hourly mean/min/max. No InfluxDB, no Grafana, no cloud.

It works alongside the official [WiCAN integration](https://github.com/jay-oswald/ha-wican)
(domain `wican`), which provides the live entities. This integration writes the logged
values into the statistics of those same entities.

## Requirements

- Home Assistant 2026.11 or newer, with the recorder enabled.
- WiCAN Pro with SD logging enabled (Logger: SD card, FATFS). Firmware 4.52 is tested.
- The official `wican` integration set up for the same device.
- The `wican` sensor entities you want history for must have `state_class: measurement`
  (see below).

## How it works

1. Every 60 s it probes `http://<wican>/check_status`. The WiCAN is usually asleep, so
   "unreachable" is the normal state and is only logged at debug level.
2. When the car is home and the WiCAN is awake, it lists `/obd_logs` and downloads databases
   it has not imported yet, newest first. The database still being written is fetched at most
   every 10 minutes, and only if it grew.
3. Each SQLite log is read read-only (falling back to a per-parameter scan if the file is
   partially corrupt), rows with impossible timestamps are discarded, and readings are
   bucketed per UTC hour.
4. Hours are written with the recorder's `async_import_statistics` to the matching `wican`
   entity. **Hours that HA already compiled from live data are never overwritten** (unless you
   enable *Prefer SD data*). An hour is only written once it ended more than 15 minutes ago,
   so it can never collide with the recorder's own compile.
5. Readings are split into trips (gap > 10 min), and the trip sensors plus a
   `wican_log_import_trip_imported` event are updated.

The SD card is never modified; logs are never deleted.

## Installation

Marketplace: *Settings → Marketplace → ⋮ → Custom repositories*, add this repository as an
*Integration*, install, restart. Then *Settings → Devices & services → Add integration →
WiCAN Log Import*, and enter the WiCAN's IP address (more reliable than the `.local` name).
The address can be saved while the WiCAN is asleep.

Manual: copy `custom_components/wican_log_import` into your `config/custom_components/`.

## Entities need `state_class: measurement`

HA only keeps long-term statistics for sensors with a state class. Parameters whose entity
has none are skipped and listed in the status sensor's `skipped_parameters.no_state_class`
attribute. If the official integration did not set it, you can add it with
[customize](https://www.home-assistant.io/docs/configuration/customizing-devices/):

```yaml
homeassistant:
  customize:
    sensor.my_car_05_enginecoolanttemp:
      state_class: measurement
```

After adding state classes, call `wican_log_import.reimport` so older logs are read again.

## Options

| Option | Default | |
| --- | --- | --- |
| Check interval | 60 s | How often to probe the WiCAN |
| Trip gap | 10 min | Readings further apart start a new trip |
| Prefer SD data | off | Overwrite hours HA already compiled from live data |
| Overrides | – | `name=entity_id` lines, e.g. `05-EngineCoolantTemp=sensor.coolant` |
| Ignored parameters | `*PIDsSupported*`, `*MonitorStatus*`, `*FuelSystemStatus*`, `*OBDStandard*`, `*FuelType*` | Glob patterns, case-insensitive. Unit `Encoded` is always skipped |
| Trip parameter names | `ODOMETER`, `05-EngineCoolantTemp`, `42-ControlModuleVolt`, `5E-EngineFuelRate`, `0F-IntakeAirTemperature`, `21-DistanceMILOn` | Change if your AutoPID names differ |

## Entities

| Entity | |
| --- | --- |
| Last trip start / end | timestamps |
| Last trip duration | min |
| Last trip distance | km (`ODOMETER` max − min) |
| Last trip max coolant temperature | °C |
| Last trip min / max control module voltage | V |
| Last trip average fuel rate | L/h |
| Last trip max intake air temperature | °C |
| Last trip distance with check-engine light | km (max of `21-DistanceMILOn`) |
| Last log import (diagnostic) | time of last successful import; attributes: `files_imported`, `rows_imported`, `rows_discarded`, `pending_files`, `device_reachable`, `last_error`, `skipped_parameters` |

## Services

- `wican_log_import.dry_run`: download and parse, return what would be imported, write nothing.
- `wican_log_import.import_now`: check the device now.
- `wican_log_import.reimport`: forget per-file progress; all logs are re-read at the next wake-up.
  Re-importing is idempotent.

## Event

`wican_log_import_trip_imported`, once per completed trip, with `start`, `end`, `start_iso`,
`end_iso`, `duration_min`, `distance_km`, `max_coolant_temp`, `min_voltage`, `max_voltage`,
`avg_fuel_rate`, `max_intake_temp`, `distance_mil_on_km`, `readings`.

```yaml
triggers:
  - trigger: event
    event_type: wican_log_import_trip_imported
conditions:
  - condition: template
    value_template: >
      {{ (trigger.event.data.max_coolant_temp or 0) > 110
         or (trigger.event.data.distance_mil_on_km or 0) > 0 }}
actions:
  - action: notify.notify
    data:
      message: >
        Car trip {{ trigger.event.data.start_iso }}: coolant max
        {{ trigger.event.data.max_coolant_temp }} °C, MIL distance
        {{ trigger.event.data.distance_mil_on_km }} km
```

## Device clock

The WiCAN has no RTC battery; time comes from NTP. Rows dated before 2024-01-01 or more than
10 minutes in the future are discarded and counted in `rows_discarded`. If most of a file is
invalid, it is skipped and a persistent notification explains why.

## Limitations

- Hourly means are plain arithmetic means of the logged samples (the log period is fixed, so
  this matches a time-weighted mean closely).
- A trip that spans two log files is reported as two trips.

## Development

```bash
pip install -r requirements_test.txt
pytest
```

`api.py`, `importer.py` and `trips.py` have no Home Assistant imports, so they can be moved
into the official integration as they are. `mapping.py` is the only layer specific to running
next to it.

## License

MIT
