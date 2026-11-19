"""Constants for the WiCAN log import integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "wican_log_import"
WICAN_DOMAIN: Final = "wican"

CONF_HOST: Final = "host"
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_TRIP_GAP: Final = "trip_gap"
CONF_ENTITY_OVERRIDES: Final = "entity_overrides"
CONF_IGNORED_PARAMS: Final = "ignored_params"
CONF_PREFER_SD_DATA: Final = "prefer_sd_data"

DEFAULT_POLL_INTERVAL: Final = 60  # seconds
DEFAULT_TRIP_GAP: Final = 10  # minutes
DEFAULT_PREFER_SD_DATA: Final = False

# Glob patterns (case-insensitive) for log parameters that are never imported
# as statistics. Parameters whose entity unit is "Encoded" are always skipped.
DEFAULT_IGNORED_PARAMS: Final = (
    "*PIDsSupported*",
    "*MonitorStatus*",
    "*FuelSystemStatus*",
    "*OBDStandard*",
    "*FuelType*",
)
ENCODED_UNIT: Final = "encoded"

# Trip summary parameter names (configurable in the options flow).
CONF_PARAM_ODOMETER: Final = "param_odometer"
CONF_PARAM_COOLANT: Final = "param_coolant"
CONF_PARAM_VOLTAGE: Final = "param_voltage"
CONF_PARAM_FUEL_RATE: Final = "param_fuel_rate"
CONF_PARAM_INTAKE_TEMP: Final = "param_intake_temp"
CONF_PARAM_DISTANCE_MIL: Final = "param_distance_mil"

DEFAULT_TRIP_PARAMS: Final[dict[str, str]] = {
    CONF_PARAM_ODOMETER: "ODOMETER",
    CONF_PARAM_COOLANT: "05-EngineCoolantTemp",
    CONF_PARAM_VOLTAGE: "42-ControlModuleVolt",
    CONF_PARAM_FUEL_RATE: "5E-EngineFuelRate",
    CONF_PARAM_INTAKE_TEMP: "0F-IntakeAirTemperature",
    CONF_PARAM_DISTANCE_MIL: "21-DistanceMILOn",
}

# Timeouts in seconds. The ESP32 is slow; downloads get a generous budget.
PROBE_TIMEOUT: Final = 4
LIST_TIMEOUT: Final = 15
DOWNLOAD_TIMEOUT: Final = 120

# The database that is still being written is downloaded at most this often.
ACTIVE_DB_MIN_INTERVAL: Final = 600  # seconds

# An hour is only written once HA has had the chance to compile it itself
# (the recorder compiles hour H shortly after H+1h). Writing earlier would
# collide with the recorder's own insert for that hour.
HOUR_SETTLE_DELAY: Final = 15 * 60  # seconds after the hour has ended

# Per-hour aggregates this integration wrote are remembered for this long, so
# that an hour spanning two log files can be merged instead of overwritten.
OWNED_HOURS_RETENTION: Final = 30 * 24 * 3600  # seconds
TRIPS_SEEN_RETENTION: Final = 90 * 24 * 3600  # seconds

STORAGE_KEY: Final = DOMAIN
STORAGE_VERSION: Final = 1
CACHE_DIR_NAME: Final = ".wican_log_import_cache"

EVENT_TRIP_IMPORTED: Final = "wican_log_import_trip_imported"
SERVICE_DRY_RUN: Final = "dry_run"
SERVICE_IMPORT_NOW: Final = "import_now"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
