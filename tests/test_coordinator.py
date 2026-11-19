"""End-to-end tests: fake device -> coordinator -> recorder statistics."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.wican_log_import.api import (
    LogDatabase,
    LogListing,
    WiCanConnectionError,
)
from custom_components.wican_log_import.const import DOMAIN, EVENT_TRIP_IMPORTED
from custom_components.wican_log_import.importer import hour_start

from .logdb import make_log_db

CLIENT = "custom_components.wican_log_import.coordinator.WiCanClient"
API = "custom_components.wican_log_import.api.WiCanClient"

COOLANT = "sensor.my_car_05_enginecoolanttemp"
VOLT = "sensor.my_car_42_controlmodulevolt"
RPM = "sensor.my_car_0c_enginerpm"
MONITOR = "sensor.my_car_01_monitorstatus"


@pytest.fixture
def wican_entities(hass: HomeAssistant) -> MockConfigEntry:
    """Register entities as the official wican integration would."""
    wican = MockConfigEntry(domain="wican", data={"ip": "192.168.1.118"})
    wican.add_to_hass(hass)
    reg = er.async_get(hass)

    def add(name: str, entity_id: str, unit: str, dc: str | None, sc: str | None) -> None:
        reg.async_get_or_create(
            "sensor",
            "wican",
            f"{wican.entry_id}_pid_{name}",
            config_entry=wican,
            suggested_object_id=entity_id.split(".", 1)[1],
            original_name=name,
            unit_of_measurement=unit,
            original_device_class=dc,
        )
        attrs: dict[str, Any] = {"unit_of_measurement": unit}
        if sc:
            attrs["state_class"] = sc
        if dc:
            attrs["device_class"] = dc
        hass.states.async_set(entity_id, "0", attrs)

    add("05-EngineCoolantTemp", COOLANT, "degC", "temperature", "measurement")
    add("42-ControlModuleVolt", VOLT, "V", "voltage", "measurement")
    add("0C-EngineRPM", RPM, "rpm", None, None)
    add("01-MonitorStatus", MONITOR, "Encoded", None, "measurement")
    return wican


def _drive(start: int, minutes: int, coolant0: float = 20) -> list[tuple[str, float, object]]:
    rows: list[tuple[str, float, object]] = []
    for i in range(minutes):
        ts = start + i * 60
        rows += [
            ("05-EngineCoolantTemp", ts, coolant0 + i),
            ("42-ControlModuleVolt", ts, 14.0 + (i % 2) * 0.4),
            ("0C-EngineRPM", ts, 1500),
            ("01-MonitorStatus", ts, 0),
            ("ODOMETER", ts, 62000 + i * 0.5),
        ]
    return rows


class FakeDevice:
    """In-memory WiCAN with log databases built from rows."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.files: dict[str, bytes] = {}
        self.current: str | None = None
        self.asleep = False
        self.downloads: list[str] = []

    def add(self, name: str, rows: list[tuple[str, float, object]]) -> None:
        path = make_log_db(self.tmp_path / f"src_{name}", rows)
        self.files[name] = path.read_bytes()

    async def status(self, *_: Any, **__: Any) -> dict[str, Any]:
        if self.asleep:
            raise WiCanConnectionError("asleep")
        return {}

    async def listing(self, *_: Any, **__: Any) -> LogListing:
        return LogListing(
            self.current,
            [LogDatabase(n, n, len(b), "") for n, b in self.files.items()],
        )

    async def download(self, name: str, *_: Any, **__: Any) -> bytes:
        self.downloads.append(name)
        return self.files[name]


@pytest.fixture
def device(tmp_path: Path):
    """Patch the API client with a fake device."""
    dev = FakeDevice(tmp_path)
    with (
        patch(f"{API}.async_get_status", AsyncMock(side_effect=dev.status)),
        patch(f"{API}.async_list_logs", AsyncMock(side_effect=dev.listing)),
        patch(f"{API}.async_download_log", AsyncMock(side_effect=dev.download)),
    ):
        yield dev


async def _setup(hass: HomeAssistant, options: dict[str, Any] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"host": "192.168.1.118"}, options=options or {}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    return entry


async def _stats(hass: HomeAssistant, entity_id: str, start: int) -> list[dict[str, Any]]:
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime.fromtimestamp(start, UTC),
        None,
        {entity_id},
        "hour",
        None,
        {"mean", "min", "max"},
    )
    return result.get(entity_id, [])


async def test_import_and_idempotent(
    recorder_mock: Any, hass: HomeAssistant, wican_entities: MockConfigEntry, device: FakeDevice
) -> None:
    base = hour_start(time.time()) - 5 * 3600
    device.add("trip1.db", _drive(base, 90))  # 1.5 h: two hours of data
    device.current = "active.db"
    device.add("active.db", [("05-EngineCoolantTemp", base + 3 * 3600, 30)])
    events: list[Event] = []
    hass.bus.async_listen(EVENT_TRIP_IMPORTED, events.append)

    entry = await _setup(hass)

    stats = await _stats(hass, COOLANT, base)
    assert [s["start"] for s in stats] == [base, base + 3600, base + 3 * 3600]
    first = stats[0]
    assert first["min"] == 20 and first["max"] == 79
    assert first["mean"] == pytest.approx(sum(range(20, 80)) / 60)
    assert stats[1]["min"] == 80 and stats[1]["max"] == 109
    assert len(await _stats(hass, VOLT, base)) == 2
    # Skipped: no state_class, and unit "Encoded".
    assert await _stats(hass, RPM, base) == []
    assert await _stats(hass, MONITOR, base) == []

    status = hass.states.get("sensor.wican_log_import_192_168_1_118_last_log_import")
    assert status is not None and status.state not in ("unknown", "unavailable")
    assert status.attributes["files_imported"] == 2
    assert COOLANT not in str(status.attributes.get("skipped_parameters"))

    # Trip sensors and event (two trips: the drive, and the single active reading).
    assert len(events) == 2
    (trip,) = [e.data for e in events if e.data["distance_km"] is not None]
    assert trip["distance_km"] == 44.5
    assert trip["max_coolant_temp"] == 109
    dist = hass.states.get("sensor.wican_log_import_192_168_1_118_last_trip_distance")
    assert dist is not None

    # Second run with unchanged files: nothing downloaded, nothing changes.
    device.downloads.clear()
    coordinator = entry.runtime_data
    coordinator._last_active_download = 0
    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    assert device.downloads == []
    assert await _stats(hass, COOLANT, base) == stats

    # Forced re-import of everything: values identical, no double counting.
    await coordinator.async_reset_progress()
    coordinator._last_active_download = 0
    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    assert sorted(device.downloads) == ["active.db", "trip1.db"]
    assert await _stats(hass, COOLANT, base) == stats
    assert len(events) == 2  # trips are not announced twice


async def test_hours_compiled_by_ha_are_kept(
    recorder_mock: Any, hass: HomeAssistant, wican_entities: MockConfigEntry, device: FakeDevice
) -> None:
    base = hour_start(time.time()) - 5 * 3600
    # HA already has the first hour from live data.
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.ARITHMETIC,
            "has_sum": False,
            "name": None,
            "source": "recorder",
            "statistic_id": COOLANT,
            "unit_class": None,
            "unit_of_measurement": "degC",
        },
        [{"start": datetime.fromtimestamp(base, UTC), "mean": 1.0, "min": 0.0, "max": 2.0}],
    )
    await async_wait_recording_done(hass)
    device.add("trip1.db", _drive(base, 90))
    await _setup(hass)
    stats = await _stats(hass, COOLANT, base)
    assert stats[0]["mean"] == 1.0  # untouched
    assert stats[1]["min"] == 80  # imported


async def test_prefer_sd_data_overwrites(
    recorder_mock: Any, hass: HomeAssistant, wican_entities: MockConfigEntry, device: FakeDevice
) -> None:
    base = hour_start(time.time()) - 5 * 3600
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.ARITHMETIC,
            "has_sum": False,
            "name": None,
            "source": "recorder",
            "statistic_id": COOLANT,
            "unit_class": None,
            "unit_of_measurement": "degC",
        },
        [{"start": datetime.fromtimestamp(base, UTC), "mean": 1.0, "min": 0.0, "max": 2.0}],
    )
    await async_wait_recording_done(hass)
    device.add("trip1.db", _drive(base, 90))
    await _setup(hass, {"prefer_sd_data": True})
    stats = await _stats(hass, COOLANT, base)
    assert stats[0]["min"] == 20


async def test_hour_split_across_files_is_merged(
    recorder_mock: Any, hass: HomeAssistant, wican_entities: MockConfigEntry, device: FakeDevice
) -> None:
    base = hour_start(time.time()) - 5 * 3600
    device.add("a.db", [("05-EngineCoolantTemp", base + 60 * i, 10) for i in range(10)])
    device.add("b.db", [("05-EngineCoolantTemp", base + 1800 + 60 * i, 30) for i in range(10)])
    await _setup(hass)
    (stat,) = await _stats(hass, COOLANT, base)
    assert stat["mean"] == 20
    assert (stat["min"], stat["max"]) == (10, 30)


async def test_unsettled_hours_wait(
    recorder_mock: Any, hass: HomeAssistant, wican_entities: MockConfigEntry, device: FakeDevice
) -> None:
    now_hour = hour_start(time.time())
    device.current = "active.db"
    device.add("active.db", [("05-EngineCoolantTemp", now_hour + 1, 50)])
    entry = await _setup(hass)
    assert await _stats(hass, COOLANT, now_hour - 3600) == []
    state = entry.runtime_data._state["files"]["active.db"]
    assert state["complete"] is False


async def test_invalid_clock(
    recorder_mock: Any, hass: HomeAssistant, wican_entities: MockConfigEntry, device: FakeDevice
) -> None:
    rows = [("05-EngineCoolantTemp", 1000 + 60 * i, 80) for i in range(50)]
    device.add("noclock.db", rows)
    entry = await _setup(hass)
    assert entry.runtime_data._state["files"]["noclock.db"]["status"] == "invalid_clock"
    status = hass.states.get("sensor.wican_log_import_192_168_1_118_last_log_import")
    assert status.attributes["rows_discarded"] == 50
    assert await _stats(hass, COOLANT, 0) == []


async def test_asleep_is_quiet(
    recorder_mock: Any,
    hass: HomeAssistant,
    wican_entities: MockConfigEntry,
    device: FakeDevice,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.wican_log_import")
    device.asleep = True
    await _setup(hass)
    status = hass.states.get("sensor.wican_log_import_192_168_1_118_last_log_import")
    assert status.attributes["device_reachable"] is False
    ours = [r for r in caplog.records if r.name.startswith("custom_components.wican_log_import")]
    assert ours, "expected a debug message about the device being asleep"
    assert all(r.levelno <= logging.DEBUG for r in ours)


async def test_dry_run_writes_nothing(
    recorder_mock: Any, hass: HomeAssistant, wican_entities: MockConfigEntry, device: FakeDevice
) -> None:
    device.asleep = True
    entry = await _setup(hass)
    device.asleep = False
    base = hour_start(time.time()) - 5 * 3600
    device.add("trip1.db", _drive(base, 90))
    response = await hass.services.async_call(
        DOMAIN, "dry_run", {}, blocking=True, return_response=True
    )
    await async_wait_recording_done(hass)
    report = response["entries"][entry.entry_id]
    (file_report,) = report["files"]
    assert file_report["statistics"][COOLANT]["hours_written"] == 2
    assert await _stats(hass, COOLANT, base) == []
    assert entry.runtime_data._state["files"] == {}
