"""Trip summary sensors and import status sensor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfLength,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WiCanLogConfigEntry
from .const import CONF_HOST, DOMAIN
from .coordinator import ImportStatus, WiCanLogCoordinator
from .trips import TripSummary

PARALLEL_UPDATES = 0


def _ts(value: float | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value, UTC)


@dataclass(frozen=True, kw_only=True)
class TripSensorDescription(SensorEntityDescription):
    """Describes a trip summary sensor."""

    value_fn: Callable[[TripSummary], Any]


TRIP_SENSORS: tuple[TripSensorDescription, ...] = (
    TripSensorDescription(
        key="last_trip_start",
        translation_key="last_trip_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda t: _ts(t.start),
    ),
    TripSensorDescription(
        key="last_trip_end",
        translation_key="last_trip_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda t: _ts(t.end),
    ),
    TripSensorDescription(
        key="last_trip_duration",
        translation_key="last_trip_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda t: t.duration_min,
    ),
    TripSensorDescription(
        key="last_trip_distance",
        translation_key="last_trip_distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda t: t.distance_km,
    ),
    TripSensorDescription(
        key="last_trip_max_coolant_temp",
        translation_key="last_trip_max_coolant_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda t: t.max_coolant_temp,
    ),
    TripSensorDescription(
        key="last_trip_min_voltage",
        translation_key="last_trip_min_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda t: t.min_voltage,
    ),
    TripSensorDescription(
        key="last_trip_max_voltage",
        translation_key="last_trip_max_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda t: t.max_voltage,
    ),
    TripSensorDescription(
        key="last_trip_avg_fuel_rate",
        translation_key="last_trip_avg_fuel_rate",
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
        native_unit_of_measurement=UnitOfVolumeFlowRate.LITERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda t: t.avg_fuel_rate,
    ),
    TripSensorDescription(
        key="last_trip_max_intake_temp",
        translation_key="last_trip_max_intake_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda t: t.max_intake_temp,
    ),
    TripSensorDescription(
        key="last_trip_distance_mil_on",
        translation_key="last_trip_distance_mil_on",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda t: t.distance_mil_on_km,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WiCanLogConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        TripSensor(coordinator, description) for description in TRIP_SENSORS
    ]
    entities.append(ImportStatusSensor(coordinator))
    async_add_entities(entities)


class _Base(CoordinatorEntity[WiCanLogCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: WiCanLogCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"WiCAN log import ({entry.data[CONF_HOST]})",
            manufacturer="MeatPi",
            model="WiCAN SD log import",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def status(self) -> ImportStatus:
        return self.coordinator.data


class TripSensor(_Base):
    """One value of the most recent completed trip."""

    entity_description: TripSensorDescription

    def __init__(
        self, coordinator: WiCanLogCoordinator, description: TripSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        trip = self.status.last_trip if self.status else None
        return None if trip is None else self.entity_description.value_fn(trip)


class ImportStatusSensor(_Base):
    """Diagnostic sensor: time of the last successful import."""

    _attr_translation_key = "import_status"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"skipped_parameters", "last_error"})

    def __init__(self, coordinator: WiCanLogCoordinator) -> None:
        super().__init__(coordinator, "import_status")

    @property
    def native_value(self) -> datetime | None:
        return self.status.last_success if self.status else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self.status
        if s is None:
            return {}
        return {
            "device_reachable": s.reachable,
            "logs_supported": s.supported,
            "last_seen": s.last_seen.isoformat() if s.last_seen else None,
            "files_imported": s.files_imported,
            "rows_imported": s.rows_imported,
            "rows_discarded": s.rows_discarded,
            "pending_files": s.pending_files,
            "last_error": s.last_error,
            "skipped_parameters": s.skipped_parameters,
        }
