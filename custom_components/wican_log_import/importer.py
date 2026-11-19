"""Read WiCAN SD card log databases and turn them into hourly aggregates.

Kept free of Home Assistant imports so it can be ported into other projects
(e.g. the official ``wican`` integration) unchanged. All functions here are
blocking and must run in an executor when called from an event loop.

Log database schema (WiCAN Pro firmware 4.x)::

    param_info(Id, Name, ...)
    param_data(param_id, timestamp, value)

``timestamp`` is Unix time in seconds (UTC).
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

_LOGGER = logging.getLogger(__name__)

MIN_VALID_TS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
FUTURE_TOLERANCE = 600  # seconds
# Timestamps above this are assumed to be milliseconds (year 5138 in seconds).
_MS_THRESHOLD = 100_000_000_000


class Reading(NamedTuple):
    """One logged value."""

    name: str
    ts: float
    value: float


class LogReadError(Exception):
    """The database could not be read, not even with the fallback."""


class LogSchemaError(LogReadError):
    """The database does not have the expected tables/columns."""


@dataclass(slots=True)
class LogSchema:
    """Resolved table and column names of a log database."""

    info_id: str
    info_name: str
    data_param_id: str
    data_ts: str
    data_value: str
    tables: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class ReadResult:
    """Readings extracted from one log database."""

    readings: list[Reading]
    rows_total: int
    rows_non_numeric: int
    used_fallback: bool
    schema: LogSchema
    failed_params: list[str] = field(default_factory=list)


def _pick(columns: list[str], *candidates: str) -> str | None:
    lower = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand in lower:
            return lower[cand]
    return None


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _inspect_schema(conn: sqlite3.Connection) -> LogSchema:
    tables: dict[str, list[str]] = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall():
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({_quote(name)})")]
        tables[name] = cols
    _LOGGER.debug("Log database tables and columns: %s", tables)

    lower_tables = {t.lower(): t for t in tables}
    info = lower_tables.get("param_info")
    data = lower_tables.get("param_data")
    if info is None or data is None:
        raise LogSchemaError(f"Missing param_info/param_data tables, found {list(tables)}")
    info_id = _pick(tables[info], "id", "param_id")
    info_name = _pick(tables[info], "name", "param_name")
    data_param_id = _pick(tables[data], "param_id", "paramid", "id_param")
    data_ts = _pick(tables[data], "timestamp", "time", "ts")
    data_value = _pick(tables[data], "value", "val")
    if None in (info_id, info_name, data_param_id, data_ts, data_value):
        raise LogSchemaError(f"Unexpected columns: {tables}")
    return LogSchema(
        info_id=info_id,  # type: ignore[arg-type]
        info_name=info_name,  # type: ignore[arg-type]
        data_param_id=data_param_id,  # type: ignore[arg-type]
        data_ts=data_ts,  # type: ignore[arg-type]
        data_value=data_value,  # type: ignore[arg-type]
        tables=tables,
    )


def _to_float(value: object) -> float | None:
    if value is None or isinstance(value, bytes):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _normalize_ts(ts: object) -> float | None:
    value = _to_float(ts)
    if value is None:
        return None
    if value > _MS_THRESHOLD:
        value /= 1000.0
    return value


def _sql_since(since: float | None, schema: LogSchema) -> tuple[str, tuple[float, ...]]:
    if since is None:
        return "", ()
    return f" AND (d.{_quote(schema.data_ts)} > ?)", (since,)


def read_log_database(path: str | Path, since: float | None = None) -> ReadResult:
    """Read all numeric readings with ``timestamp > since`` from a log database.

    Tries a JOIN first. If the database is damaged (typically the active
    database downloaded mid-write), falls back to reading ``param_info``
    separately and scanning ``param_data`` per parameter without indexes.
    Raises LogReadError when neither works.
    """
    uri = f"file:{Path(path).as_posix()}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as err:
        raise LogReadError(f"Cannot open {path}: {err}") from err
    try:
        try:
            schema = _inspect_schema(conn)
        except sqlite3.DatabaseError as err:
            raise LogReadError(f"Cannot read schema of {path}: {err}") from err
        try:
            return _read_joined(conn, schema, since)
        except sqlite3.DatabaseError as err:
            _LOGGER.debug("JOIN query failed on %s (%s), using fallback", path, err)
        return _read_fallback(conn, schema, since, path)
    finally:
        conn.close()


def _read_joined(
    conn: sqlite3.Connection, schema: LogSchema, since: float | None
) -> ReadResult:
    where, params = _sql_since(since, schema)
    query = (
        f"SELECT i.{_quote(schema.info_name)}, d.{_quote(schema.data_ts)}, "
        f"d.{_quote(schema.data_value)} "
        f"FROM param_data d JOIN param_info i "
        f"ON i.{_quote(schema.info_id)} = d.{_quote(schema.data_param_id)} "
        f"WHERE 1=1{where} ORDER BY d.{_quote(schema.data_ts)}"
    )
    return _collect(conn.execute(query, params), schema, used_fallback=False)


def _collect(
    rows: Iterable[tuple[object, object, object]],
    schema: LogSchema,
    used_fallback: bool,
    failed_params: list[str] | None = None,
) -> ReadResult:
    readings: list[Reading] = []
    total = 0
    non_numeric = 0
    for name, raw_ts, raw_value in rows:
        total += 1
        ts = _normalize_ts(raw_ts)
        value = _to_float(raw_value)
        if ts is None or value is None or name is None:
            non_numeric += 1
            continue
        readings.append(Reading(str(name), ts, value))
    readings.sort(key=lambda r: r.ts)
    return ReadResult(
        readings=readings,
        rows_total=total,
        rows_non_numeric=non_numeric,
        used_fallback=used_fallback,
        schema=schema,
        failed_params=failed_params or [],
    )


def _read_fallback(
    conn: sqlite3.Connection, schema: LogSchema, since: float | None, path: object
) -> ReadResult:
    try:
        params = conn.execute(
            f"SELECT {_quote(schema.info_id)}, {_quote(schema.info_name)} "
            "FROM param_info"
        ).fetchall()
    except sqlite3.DatabaseError as err:
        raise LogReadError(f"param_info unreadable in {path}: {err}") from err

    ts_col = _quote(schema.data_ts)
    where = f" AND {ts_col} > ?" if since is not None else ""
    rows: list[tuple[object, object, object]] = []
    failed: list[str] = []
    for param_id, name in params:
        try:
            for ts, value in conn.execute(
                f"SELECT {ts_col}, {_quote(schema.data_value)} "
                f"FROM param_data NOT INDEXED "
                f"WHERE {_quote(schema.data_param_id)} = ?{where}",
                (param_id, since) if since is not None else (param_id,),
            ):
                rows.append((name, ts, value))
        except sqlite3.DatabaseError as err:
            _LOGGER.debug("Fallback read of %s failed in %s: %s", name, path, err)
            failed.append(str(name))
    if failed:
        # A partial read would silently lose data; let the caller retry later.
        raise LogReadError(
            f"{len(failed)} of {len(params)} parameters unreadable in {path}"
        )
    return _collect(rows, schema, used_fallback=True, failed_params=failed)


def filter_timestamps(
    readings: Iterable[Reading],
    now: float,
    min_ts: float = MIN_VALID_TS,
    future_tolerance: float = FUTURE_TOLERANCE,
) -> tuple[list[Reading], int]:
    """Drop readings with implausible timestamps. Returns (valid, discarded)."""
    max_ts = now + future_tolerance
    valid: list[Reading] = []
    discarded = 0
    for reading in readings:
        if min_ts <= reading.ts <= max_ts:
            valid.append(reading)
        else:
            discarded += 1
    return valid, discarded


def is_mostly_invalid(valid: int, discarded: int, threshold: float = 0.5) -> bool:
    """Return True when more than ``threshold`` of the rows were discarded."""
    total = valid + discarded
    return total > 0 and discarded / total > threshold


def hour_start(ts: float) -> int:
    """Return the Unix timestamp of the UTC hour containing ``ts``."""
    return int(ts // 3600 * 3600)


@dataclass(slots=True)
class HourlyAggregate:
    """Count/sum/min/max of the readings within one hour."""

    count: int = 0
    total: float = 0.0
    minimum: float = math.inf
    maximum: float = -math.inf

    def add(self, value: float) -> None:
        """Add one reading."""
        self.count += 1
        self.total += value
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)

    def merge(self, other: HourlyAggregate) -> None:
        """Merge another aggregate of the same hour into this one."""
        self.count += other.count
        self.total += other.total
        self.minimum = min(self.minimum, other.minimum)
        self.maximum = max(self.maximum, other.maximum)

    @property
    def mean(self) -> float:
        """Arithmetic mean of the readings."""
        return self.total / self.count

    def as_list(self) -> list[float]:
        """Serialize for JSON storage."""
        return [self.count, self.total, self.minimum, self.maximum]

    @classmethod
    def from_list(cls, data: list[float]) -> HourlyAggregate:
        """Deserialize from JSON storage."""
        count, total, minimum, maximum = data
        return cls(int(count), float(total), float(minimum), float(maximum))


def bucket_hourly(readings: Iterable[Reading]) -> dict[str, dict[int, HourlyAggregate]]:
    """Group readings per parameter into UTC hours."""
    result: dict[str, dict[int, HourlyAggregate]] = defaultdict(dict)
    for reading in readings:
        hours = result[reading.name]
        start = hour_start(reading.ts)
        if (agg := hours.get(start)) is None:
            agg = hours[start] = HourlyAggregate()
        agg.add(reading.value)
    return dict(result)


def hour_is_settled(hour_ts: int, now: float, settle_delay: float) -> bool:
    """Return True if the hour ended at least ``settle_delay`` seconds ago."""
    return hour_ts + 3600 + settle_delay <= now
