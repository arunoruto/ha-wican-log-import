"""Polling coordinator: download WiCAN SD logs and import them."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import (
    InvalidFilenameError,
    LogDatabase,
    LogListing,
    WiCanClient,
    WiCanConnectionError,
    WiCanError,
    WiCanNotSupportedError,
    validate_log_filename,
)
from .const import (
    ACTIVE_DB_MIN_INTERVAL,
    CACHE_DIR_NAME,
    CONF_ENTITY_OVERRIDES,
    CONF_HOST,
    CONF_IGNORED_PARAMS,
    CONF_PARAM_COOLANT,
    CONF_PARAM_DISTANCE_MIL,
    CONF_PARAM_FUEL_RATE,
    CONF_PARAM_INTAKE_TEMP,
    CONF_PARAM_ODOMETER,
    CONF_PARAM_VOLTAGE,
    CONF_POLL_INTERVAL,
    CONF_PREFER_SD_DATA,
    CONF_TRIP_GAP,
    DEFAULT_IGNORED_PARAMS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PREFER_SD_DATA,
    DEFAULT_TRIP_GAP,
    DEFAULT_TRIP_PARAMS,
    DOMAIN,
    DOWNLOAD_TIMEOUT,
    EVENT_TRIP_IMPORTED,
    HOUR_SETTLE_DELAY,
    LIST_TIMEOUT,
    OWNED_HOURS_RETENTION,
    PROBE_TIMEOUT,
    STORAGE_KEY,
    STORAGE_VERSION,
    TRIPS_SEEN_RETENTION,
)
from .importer import (
    MIN_VALID_TS,
    HourlyAggregate,
    LogReadError,
    bucket_hourly,
    filter_timestamps,
    hour_is_settled,
    hour_start,
    is_mostly_invalid,
    read_log_database,
)
from .mapping import ParameterMapping, build_mapping, parse_overrides, parse_patterns
from .recorder_import import async_write_hours
from .trips import TripParams, TripSummary, completed_trips, split_trips, summarize_trip

_LOGGER = logging.getLogger(__name__)

MAX_READ_FAILURES = 3


def _empty_state() -> dict[str, Any]:
    return {
        "files": {},
        "owned": {},
        "trips_seen": {},
        "last_trip": None,
        "totals": {"files_imported": 0, "rows_imported": 0},
        "last_success": None,
    }


@dataclass(slots=True)
class ImportStatus:
    """Snapshot exposed to the entities."""

    reachable: bool = False
    supported: bool | None = None
    last_seen: datetime | None = None
    last_success: datetime | None = None
    files_imported: int = 0
    rows_imported: int = 0
    rows_discarded: int = 0
    last_error: str | None = None
    pending_files: int = 0
    skipped_parameters: dict[str, list[str]] = field(default_factory=dict)
    last_trip: TripSummary | None = None


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


def _sort_key(db: LogDatabase) -> tuple[str, str]:
    return (db.created, db.filename)


class WiCanLogCoordinator(DataUpdateCoordinator[ImportStatus]):
    """Probe the WiCAN, download new log databases and import them."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: WiCanClient
    ) -> None:
        """Initialize the coordinator."""
        poll = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_HOST]}",
            update_interval=timedelta(seconds=poll),
        )
        self.client = client
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
        )
        self._state: dict[str, Any] = _empty_state()
        self._cache_dir = Path(hass.config.path(CACHE_DIR_NAME, entry.entry_id))
        self._run_lock = asyncio.Lock()
        self._last_active_download = 0.0
        self._reachable = False
        self._supported: bool | None = None
        self._last_seen: datetime | None = None
        self._last_error: str | None = None
        self._pending = 0
        self._skipped: dict[str, list[str]] = {}
        self._warned_unsupported = False

    # ------------------------------------------------------------------ options

    @property
    def _options(self) -> dict[str, Any]:
        return dict(self.config_entry.options)

    @property
    def trip_gap_seconds(self) -> float:
        """Gap between readings that starts a new trip."""
        return float(self._options.get(CONF_TRIP_GAP, DEFAULT_TRIP_GAP)) * 60

    @property
    def trip_params(self) -> TripParams:
        """Configured parameter names for the trip summary."""
        opts = self._options

        def get(key: str) -> str:
            return str(opts.get(key) or DEFAULT_TRIP_PARAMS[key])

        return TripParams(
            odometer=get(CONF_PARAM_ODOMETER),
            coolant=get(CONF_PARAM_COOLANT),
            voltage=get(CONF_PARAM_VOLTAGE),
            fuel_rate=get(CONF_PARAM_FUEL_RATE),
            intake_temp=get(CONF_PARAM_INTAKE_TEMP),
            distance_mil=get(CONF_PARAM_DISTANCE_MIL),
        )

    def _mapping(self, names: set[str]) -> ParameterMapping:
        opts = self._options
        ignored = opts.get(CONF_IGNORED_PARAMS)
        patterns = (
            parse_patterns(ignored) if ignored is not None else list(DEFAULT_IGNORED_PARAMS)
        )
        return build_mapping(
            self.hass,
            self.config_entry.data[CONF_HOST],
            names,
            parse_overrides(opts.get(CONF_ENTITY_OVERRIDES, "")),
            patterns,
        )

    # ------------------------------------------------------------------ state

    async def async_load(self) -> None:
        """Load persisted import state."""
        stored = await self._store.async_load()
        if stored:
            state = _empty_state()
            state.update(stored)
            self._state = state
        self.async_set_updated_data(self._status())

    async def async_reset_progress(self) -> None:
        """Forget per-file progress so every log on the SD card is re-read.

        Ownership of already imported hours is kept, so re-importing merges
        instead of double counting or clobbering HA's own statistics.
        """
        async with self._run_lock:
            self._state["files"] = {}
            await self._store.async_save(self._state)
        self.async_set_updated_data(self._status())

    def _status(self) -> ImportStatus:
        state = self._state
        last_success = state.get("last_success")
        last_trip = state.get("last_trip")
        return ImportStatus(
            reachable=self._reachable,
            supported=self._supported,
            last_seen=self._last_seen,
            last_success=datetime.fromisoformat(last_success) if last_success else None,
            files_imported=state["totals"]["files_imported"],
            rows_imported=state["totals"]["rows_imported"],
            rows_discarded=sum(
                f.get("discarded_past", 0) + f.get("discarded_future", 0)
                for f in state["files"].values()
            ),
            last_error=self._last_error,
            pending_files=self._pending,
            skipped_parameters=self._skipped,
            last_trip=TripSummary.from_dict(last_trip) if last_trip else None,
        )

    # ------------------------------------------------------------------ cycle

    async def _async_update_data(self) -> ImportStatus:
        await self.async_run_cycle(dry_run=False)
        return self._status()

    async def async_run_cycle(self, dry_run: bool) -> dict[str, Any]:
        """Run one probe/download/import cycle. Returns a report."""
        async with self._run_lock:
            state = copy.deepcopy(self._state) if dry_run else self._state
            report: dict[str, Any] = {"dry_run": dry_run, "reachable": False, "files": []}
            try:
                await self.client.async_get_status(PROBE_TIMEOUT)
            except WiCanError as err:
                _LOGGER.debug("WiCAN not reachable (normal while asleep): %s", err)
                self._reachable = False
                return report

            self._reachable = True
            self._last_seen = datetime.now(UTC)
            report["reachable"] = True
            try:
                listing = await self.client.async_list_logs(LIST_TIMEOUT)
            except WiCanNotSupportedError:
                self._supported = False
                self._last_error = (
                    "Device has no /obd_logs endpoint (needs a WiCAN Pro with "
                    "SD card logging enabled)"
                )
                if not self._warned_unsupported:
                    _LOGGER.warning(self._last_error)
                    self._warned_unsupported = True
                report["error"] = self._last_error
                return report
            except WiCanError as err:
                _LOGGER.debug("Listing logs failed: %s", err)
                report["error"] = str(err)
                return report

            self._supported = True
            todo = self._select_files(listing, state, time.time(), dry_run)
            report["listed"] = [
                {"filename": d.filename, "size": d.size, "created": d.created}
                for d in listing.databases
            ]
            report["selected"] = [d.filename for d in todo]
            self._pending = len(todo)
            for db in todo:
                active = db.filename == listing.current_db
                try:
                    outcome = await self._async_process_file(db, active, state, dry_run)
                except WiCanConnectionError as err:
                    _LOGGER.debug("Device went away during download: %s", err)
                    report["files"].append({"filename": db.filename, "error": str(err)})
                    break
                except WiCanError as err:
                    self._last_error = f"{db.filename}: {err}"
                    _LOGGER.warning("Downloading %s failed: %s", db.filename, err)
                    report["files"].append({"filename": db.filename, "error": str(err)})
                    continue
                report["files"].append(outcome)
                self._pending -= 1
                if not dry_run:
                    await self._store.async_save(state)
            if not dry_run:
                self._prune(state, time.time())
                await self._store.async_save(state)
            return report

    def _select_files(
        self,
        listing: LogListing,
        state: dict[str, Any],
        now: float,
        dry_run: bool,
    ) -> list[LogDatabase]:
        files: dict[str, Any] = state["files"]
        selected: list[LogDatabase] = []
        for db in sorted(listing.databases, key=_sort_key, reverse=True):
            try:
                validate_log_filename(db.filename)
            except InvalidFilenameError as err:
                _LOGGER.warning("%s", err)
                continue
            st = files.get(db.filename)
            if db.filename == listing.current_db:
                if not dry_run and now - self._last_active_download < ACTIVE_DB_MIN_INTERVAL:
                    continue
                if st is not None and db.size <= st.get("size", 0) and st.get("complete"):
                    continue
                if st is not None and st.get("failures", 0) >= MAX_READ_FAILURES and db.size == st.get("size"):
                    continue
                selected.append(db)
                continue
            if st is None:
                selected.append(db)
                continue
            size_changed = db.size != st.get("size")
            if size_changed:
                selected.append(db)
            elif st.get("status") in ("invalid_clock", "corrupt"):
                continue
            elif not st.get("complete"):
                selected.append(db)
        return selected

    async def _async_process_file(
        self,
        db: LogDatabase,
        active: bool,
        state: dict[str, Any],
        dry_run: bool,
    ) -> dict[str, Any]:
        """Download, read and import one log database."""
        hass = self.hass
        name = db.filename
        outcome: dict[str, Any] = {"filename": name, "active": active, "size": db.size}
        data = await self.client.async_download_log(name, DOWNLOAD_TIMEOUT)
        if active and not dry_run:
            self._last_active_download = time.time()
        outcome["downloaded_bytes"] = len(data)
        path = self._cache_dir / name
        await hass.async_add_executor_job(_write_bytes, path, data)
        del data

        files: dict[str, Any] = state["files"]
        st: dict[str, Any] = files.setdefault(name, {})
        now = time.time()

        since: float | None = None
        last_ts = st.get("last_imported_ts")
        if last_ts is not None and db.size >= st.get("size", 0):
            since = float(hour_start(last_ts)) - 1
            if (open_start := st.get("open_trip_start")) is not None:
                since = min(since, float(open_start) - 1)
        else:
            st.pop("discarded_past", None)
        outcome["since"] = since

        try:
            result = await hass.async_add_executor_job(read_log_database, path, since)
        except LogReadError as err:
            st["failures"] = st.get("failures", 0) + 1
            st["size"] = db.size
            if st["failures"] >= MAX_READ_FAILURES and not active:
                st["status"] = "corrupt"
            self._last_error = f"{name}: {err}"
            _LOGGER.warning(
                "Could not read %s (attempt %d), will retry: %s",
                name,
                st["failures"],
                err,
            )
            outcome["error"] = str(err)
            return outcome

        if result.used_fallback:
            _LOGGER.debug("%s read with corruption fallback", name)
        valid, discarded = filter_timestamps(result.readings, now)
        past = sum(1 for r in result.readings if r.ts < MIN_VALID_TS)
        st["discarded_past"] = st.get("discarded_past", 0) + past
        st["discarded_future"] = discarded - past
        outcome.update(
            rows_read=result.rows_total,
            rows_valid=len(valid),
            rows_discarded=discarded,
            rows_non_numeric=result.rows_non_numeric,
            used_fallback=result.used_fallback,
        )

        if is_mostly_invalid(len(valid), discarded):
            st.update(status="invalid_clock", size=db.size, failures=0)
            outcome["skipped"] = "invalid_clock"
            if not dry_run:
                self._notify_invalid_clock(name, discarded, len(valid))
                await hass.async_add_executor_job(_unlink, path)
            return outcome

        # ---- statistics
        mapping = self._mapping({r.name for r in valid})
        self._skipped = mapping.skipped_summary()
        if mapping.no_state_class:
            _LOGGER.info(
                "Not importing statistics for entities without state_class "
                "'measurement': %s",
                ", ".join(sorted(mapping.no_state_class.values())),
            )
        outcome["skipped_parameters"] = self._skipped
        buckets = bucket_hourly(valid)
        prefer_sd = self._options.get(CONF_PREFER_SD_DATA, DEFAULT_PREFER_SD_DATA)
        owned_all: dict[str, Any] = state["owned"]
        rows_imported = 0
        planned: dict[str, Any] = {}
        errors: list[str] = []
        for pname, entity in mapping.entities.items():
            hours = {
                h: agg
                for h, agg in buckets.get(pname, {}).items()
                if hour_is_settled(h, now, HOUR_SETTLE_DELAY)
            }
            if not hours:
                continue
            owned = owned_all.setdefault(pname, {})
            owned_before = {int(h) for h in owned}
            merged: dict[int, HourlyAggregate] = {}
            for h, agg in hours.items():
                total = HourlyAggregate()
                total.merge(agg)
                for fname, raw in owned.get(str(h), {}).items():
                    if fname != name:
                        total.merge(HourlyAggregate.from_list(raw))
                merged[h] = total
            write = await async_write_hours(
                hass, entity, merged, owned_before, prefer_sd, dry_run
            )
            if write.error:
                errors.append(write.error)
                _LOGGER.warning("Statistics import skipped: %s", write.error)
                continue
            for h in write.written:
                owned.setdefault(str(h), {})[name] = hours[h].as_list()
                rows_imported += hours[h].count
            planned[entity.entity_id] = {
                "hours_written": len(write.written),
                "hours_skipped_existing": len(write.skipped_existing),
            }
        outcome["statistics"] = planned
        if errors:
            outcome["statistics_errors"] = errors

        # ---- progress bookkeeping
        settled = [r for r in valid if hour_is_settled(hour_start(r.ts), now, HOUR_SETTLE_DELAY)]
        if settled:
            st["last_imported_ts"] = max(r.ts for r in settled)
        unsettled = len(settled) != len(valid)

        # ---- trips
        gap = self.trip_gap_seconds
        trips = split_trips(valid, gap)
        done = completed_trips(trips, now, gap)
        done_until = float(st.get("trips_done_until", 0))
        new_trips = [t for t in done if t.end > done_until]
        done_ids = {id(t) for t in done}
        open_trips = [t for t in trips if id(t) not in done_ids]
        st["open_trip_start"] = open_trips[-1].start if open_trips else None
        if done:
            st["trips_done_until"] = max(done_until, done[-1].end)
        params = self.trip_params
        summaries = [summarize_trip(t, params) for t in new_trips]
        outcome["trips"] = [s.as_event_data() for s in summaries]
        for summary in summaries:
            key = str(int(summary.start))
            if key in state["trips_seen"]:
                continue
            state["trips_seen"][key] = summary.end
            last = state.get("last_trip")
            if last is None or summary.end > last["end"]:
                state["last_trip"] = summary.as_dict()
            if not dry_run:
                hass.bus.async_fire(EVENT_TRIP_IMPORTED, summary.as_event_data())

        # Complete: every hour was written and every trip has ended. A complete
        # active database is only downloaded again once it grows.
        st.update(
            size=db.size,
            complete=not unsettled and not open_trips,
            status="ok",
            failures=0,
            imported_at=datetime.now(UTC).isoformat(),
        )
        state["totals"]["rows_imported"] += rows_imported
        if not st.get("counted"):
            state["totals"]["files_imported"] += 1
            st["counted"] = True
        state["last_success"] = datetime.now(UTC).isoformat()
        outcome["rows_imported"] = rows_imported
        if not dry_run:
            self._last_error = None
            await hass.async_add_executor_job(_unlink, path)
        _LOGGER.debug("Processed %s: %s", name, outcome)
        return outcome

    def _notify_invalid_clock(self, filename: str, discarded: int, valid: int) -> None:
        persistent_notification.async_create(
            self.hass,
            (
                f"The WiCAN log `{filename}` was not imported: {discarded} of "
                f"{discarded + valid} readings have impossible timestamps "
                "(before 2024 or in the future). The WiCAN clock was most likely "
                "not set during that recording (no NTP time after power loss). "
                "The file will be retried if it changes."
            ),
            title="WiCAN log import: device clock not set",
            notification_id=f"{DOMAIN}_clock_{self.config_entry.entry_id}_{filename}",
        )

    @staticmethod
    def _prune(state: dict[str, Any], now: float) -> None:
        owned_cutoff = now - OWNED_HOURS_RETENTION
        for pname in list(state["owned"]):
            hours = state["owned"][pname]
            for h in [h for h in hours if int(h) < owned_cutoff]:
                del hours[h]
            if not hours:
                del state["owned"][pname]
        trip_cutoff = now - TRIPS_SEEN_RETENTION
        seen = state["trips_seen"]
        for key in [k for k, end in seen.items() if end < trip_cutoff]:
            del seen[key]
