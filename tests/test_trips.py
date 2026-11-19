"""Tests for trips.py (no Home Assistant needed)."""

from __future__ import annotations

from custom_components.wican_log_import.importer import Reading
from custom_components.wican_log_import.trips import (
    TripParams,
    TripSummary,
    completed_trips,
    split_trips,
    summarize_trip,
)

T0 = 1_790_000_000
GAP = 600


def _trip_rows(start: int, minutes: int, odo_start: float) -> list[Reading]:
    rows: list[Reading] = []
    for i in range(minutes):
        ts = start + i * 60
        rows += [
            Reading("ODOMETER", ts, odo_start + i * 0.5),
            Reading("05-EngineCoolantTemp", ts, 20 + i),
            Reading("42-ControlModuleVolt", ts, 14.0 + (i % 3) * 0.1),
            Reading("5E-EngineFuelRate", ts, 2.0 if i % 2 else 4.0),
            Reading("0F-IntakeAirTemperature", ts, 10 + i / 10),
            Reading("21-DistanceMILOn", ts, 0),
        ]
    return rows


def test_split_on_gap() -> None:
    rows = _trip_rows(T0, 20, 1000) + _trip_rows(T0 + 20 * 60 + GAP + 1, 5, 1010)
    trips = split_trips(rows, GAP)
    assert len(trips) == 2
    assert trips[0].start == T0
    assert trips[0].end == T0 + 19 * 60
    assert len(trips[1].readings) == 5 * 6


def test_gap_equal_to_threshold_does_not_split() -> None:
    rows = [Reading("X", T0, 1), Reading("X", T0 + GAP, 1)]
    assert len(split_trips(rows, GAP)) == 1


def test_unsorted_input() -> None:
    rows = [Reading("X", T0 + 60, 1), Reading("X", T0, 1)]
    (trip,) = split_trips(rows, GAP)
    assert (trip.start, trip.end) == (T0, T0 + 60)


def test_summary_values() -> None:
    (trip,) = split_trips(_trip_rows(T0, 21, 62000), GAP)
    s = summarize_trip(trip, TripParams())
    assert s.duration_min == 20.0
    assert s.distance_km == 10.0
    assert s.max_coolant_temp == 40
    assert s.min_voltage == 14.0
    assert s.max_voltage == 14.2
    assert s.avg_fuel_rate == round((11 * 4 + 10 * 2) / 21, 2)
    assert s.max_intake_temp == 12.0
    assert s.distance_mil_on_km == 0
    assert TripSummary.from_dict(s.as_dict()) == s
    assert s.as_event_data()["start_iso"].startswith("2026-")


def test_summary_missing_params_and_zero_odometer() -> None:
    rows = [Reading("ODOMETER", T0, 0), Reading("ODOMETER", T0 + 60, 0)]
    (trip,) = split_trips(rows, GAP)
    s = summarize_trip(trip, TripParams())
    assert s.distance_km is None
    assert s.max_coolant_temp is None


def test_completed_trips() -> None:
    trips = split_trips(_trip_rows(T0, 5, 0) + _trip_rows(T0 + 3600, 5, 0), GAP)
    last_end = trips[-1].end
    assert len(completed_trips(trips, last_end + 60, GAP)) == 1
    assert len(completed_trips(trips, last_end + GAP + 1, GAP)) == 2
