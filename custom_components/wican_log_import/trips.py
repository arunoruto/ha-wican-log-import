"""Split log readings into trips and summarize them.

Free of Home Assistant imports; blocking-free and cheap.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .importer import Reading


@dataclass(frozen=True, slots=True)
class TripParams:
    """Log parameter names used for the trip summary."""

    odometer: str = "ODOMETER"
    coolant: str = "05-EngineCoolantTemp"
    voltage: str = "42-ControlModuleVolt"
    fuel_rate: str = "5E-EngineFuelRate"
    intake_temp: str = "0F-IntakeAirTemperature"
    distance_mil: str = "21-DistanceMILOn"


@dataclass(slots=True)
class Trip:
    """A run of readings without gaps longer than the trip gap."""

    start: float
    end: float
    readings: list[Reading] = field(default_factory=list)

    def values(self, name: str) -> list[float]:
        """Return all values of one parameter within the trip."""
        return [r.value for r in self.readings if r.name == name]


@dataclass(frozen=True, slots=True)
class TripSummary:
    """Summary values of one trip. Missing parameters are None."""

    start: float
    end: float
    duration_min: float
    distance_km: float | None
    max_coolant_temp: float | None
    min_voltage: float | None
    max_voltage: float | None
    avg_fuel_rate: float | None
    max_intake_temp: float | None
    distance_mil_on_km: float | None
    readings: int

    def as_dict(self) -> dict[str, Any]:
        """Serialize for storage."""
        return asdict(self)

    def as_event_data(self) -> dict[str, Any]:
        """Event payload with ISO timestamps added."""
        data = self.as_dict()
        data["start_iso"] = datetime.fromtimestamp(self.start, UTC).isoformat()
        data["end_iso"] = datetime.fromtimestamp(self.end, UTC).isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TripSummary:
        """Deserialize from storage, ignoring unknown keys."""
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in names})


def split_trips(readings: Sequence[Reading], gap_seconds: float) -> list[Trip]:
    """Split time-sorted readings into trips.

    A new trip starts when two consecutive timestamps are more than
    ``gap_seconds`` apart.
    """
    trips: list[Trip] = []
    current: Trip | None = None
    for reading in sorted(readings, key=lambda r: r.ts):
        if current is None or reading.ts - current.end > gap_seconds:
            current = Trip(start=reading.ts, end=reading.ts)
            trips.append(current)
        current.end = reading.ts
        current.readings.append(reading)
    return trips


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def summarize_trip(trip: Trip, params: TripParams) -> TripSummary:
    """Compute the summary values of one trip."""
    odo = trip.values(params.odometer)
    # Odometer 0 means "not read yet" on many ECUs; ignore it.
    odo = [v for v in odo if v > 0]
    distance = max(odo) - min(odo) if odo else None
    if distance is not None and distance < 0:  # pragma: no cover - defensive
        distance = None

    coolant = trip.values(params.coolant)
    voltage = trip.values(params.voltage)
    fuel = trip.values(params.fuel_rate)
    intake = trip.values(params.intake_temp)
    mil = trip.values(params.distance_mil)

    return TripSummary(
        start=trip.start,
        end=trip.end,
        duration_min=round((trip.end - trip.start) / 60, 1),
        distance_km=_round(distance, 1),
        max_coolant_temp=_round(max(coolant)) if coolant else None,
        min_voltage=_round(min(voltage)) if voltage else None,
        max_voltage=_round(max(voltage)) if voltage else None,
        avg_fuel_rate=_round(sum(fuel) / len(fuel)) if fuel else None,
        max_intake_temp=_round(max(intake)) if intake else None,
        distance_mil_on_km=_round(max(mil), 1) if mil else None,
        readings=len(trip.readings),
    )


def completed_trips(
    trips: Sequence[Trip], now: float, gap_seconds: float
) -> list[Trip]:
    """Return trips that have ended (no reading for longer than the gap)."""
    return [t for t in trips if now - t.end > gap_seconds]
