"""Tests for importer.py (no Home Assistant needed)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from custom_components.wican_log_import.importer import (
    MIN_VALID_TS,
    HourlyAggregate,
    LogReadError,
    LogSchemaError,
    Reading,
    bucket_hourly,
    filter_timestamps,
    hour_is_settled,
    hour_start,
    is_mostly_invalid,
    read_log_database,
)

from .logdb import corrupt_index, make_log_db

T0 = int(datetime(2026, 11, 18, 10, 0, tzinfo=UTC).timestamp())


def test_read_joined(tmp_path: Path) -> None:
    db = make_log_db(
        tmp_path / "a.db",
        [
            ("05-EngineCoolantTemp", T0 + 120, 80),
            ("05-EngineCoolantTemp", T0 + 60, 70),
            ("0C-EngineRPM", T0 + 60, 900.5),
        ],
    )
    result = read_log_database(db)
    assert not result.used_fallback
    assert result.rows_total == 3
    assert [r.ts for r in result.readings] == [T0 + 60, T0 + 60, T0 + 120]
    assert Reading("05-EngineCoolantTemp", T0 + 120, 80.0) in result.readings
    assert set(result.schema.tables) == {"param_info", "param_data"}


def test_read_since(tmp_path: Path) -> None:
    db = make_log_db(tmp_path / "a.db", [("X", T0 + i * 60, i) for i in range(10)])
    result = read_log_database(db, since=T0 + 5 * 60)
    assert [r.value for r in result.readings] == [6, 7, 8, 9]


def test_non_numeric_skipped(tmp_path: Path) -> None:
    db = make_log_db(
        tmp_path / "a.db", [("X", T0, "N/A"), ("X", T0 + 60, "12.5"), ("X", T0 + 120, None)]
    )
    result = read_log_database(db)
    assert [r.value for r in result.readings] == [12.5]
    assert result.rows_non_numeric == 2


def test_millisecond_timestamps(tmp_path: Path) -> None:
    db = make_log_db(tmp_path / "a.db", [("X", T0 * 1000, 1)])
    assert read_log_database(db).readings[0].ts == T0


def test_corrupt_index_uses_fallback(tmp_path: Path) -> None:
    rows = [("X", T0 + i * 60, i) for i in range(500)] + [
        ("Y", T0 + i * 60, -i) for i in range(500)
    ]
    db = make_log_db(tmp_path / "a.db", rows, with_ts_index=True)
    corrupt_index(db)
    result = read_log_database(db, since=T0 - 1)
    assert result.used_fallback
    assert len(result.readings) == 1000
    assert result.readings == sorted(result.readings, key=lambda r: r.ts)


def test_garbage_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.db"
    path.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(LogReadError):
        read_log_database(path)


def test_wrong_schema_raises(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "other.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE foo (a)")
    conn.commit()
    conn.close()
    with pytest.raises(LogSchemaError):
        read_log_database(path)


def test_filter_timestamps() -> None:
    now = T0 + 3600
    readings = [
        Reading("X", 1000, 1),  # 1970: device clock not set
        Reading("X", MIN_VALID_TS - 1, 1),
        Reading("X", T0, 1),
        Reading("X", now + 599, 1),
        Reading("X", now + 601, 1),  # too far in the future
    ]
    valid, discarded = filter_timestamps(readings, now)
    assert [r.ts for r in valid] == [T0, now + 599]
    assert discarded == 3


def test_mostly_invalid() -> None:
    assert is_mostly_invalid(1, 2)
    assert not is_mostly_invalid(2, 2)
    assert not is_mostly_invalid(0, 0)


def test_bucket_hourly() -> None:
    readings = [
        Reading("X", T0 + 0, 10),
        Reading("X", T0 + 1800, 20),
        Reading("X", T0 + 3599, 30),
        Reading("X", T0 + 3600, 100),
        Reading("Y", T0 + 10, -5),
    ]
    buckets = bucket_hourly(readings)
    x = buckets["X"]
    assert set(x) == {T0, T0 + 3600}
    assert x[T0].count == 3
    assert x[T0].mean == 20
    assert (x[T0].minimum, x[T0].maximum) == (10, 30)
    assert x[T0 + 3600].mean == 100
    assert buckets["Y"][T0].mean == -5


def test_hour_helpers() -> None:
    assert hour_start(T0 + 3599.9) == T0
    assert datetime.fromtimestamp(hour_start(T0 + 1234), UTC).minute == 0
    assert not hour_is_settled(T0, T0 + 3600 + 899, 900)
    assert hour_is_settled(T0, T0 + 3600 + 900, 900)


def test_aggregate_roundtrip_and_merge() -> None:
    a = HourlyAggregate()
    for v in (1, 2, 3):
        a.add(v)
    b = HourlyAggregate.from_list(a.as_list())
    b.merge(HourlyAggregate(1, 10, 10, 10))
    assert b.count == 4
    assert b.mean == 4
    assert (b.minimum, b.maximum) == (1, 10)
