"""Write hourly aggregates into the recorder's long-term statistics."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    STATISTIC_UNIT_TO_UNIT_CONVERTER,
    UNIT_CLASS_TO_UNIT_CONVERTER,
    async_import_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.components.sensor.const import UNIT_CONVERTERS
from homeassistant.core import HomeAssistant

from .importer import HourlyAggregate
from .mapping import MappedEntity

_LOGGER = logging.getLogger(__name__)

RECORDER_SOURCE = "recorder"


@dataclass(slots=True)
class WriteResult:
    """Outcome of writing one entity's hours."""

    written: list[int] = field(default_factory=list)
    skipped_existing: list[int] = field(default_factory=list)
    error: str | None = None


def unit_class_for(device_class: str | None, unit: str | None) -> str | None:
    """Mirror the sensor recorder's unit class choice (sensor/recorder.py)."""
    if device_class and (conv := UNIT_CONVERTERS.get(device_class)):  # type: ignore[call-overload]
        if unit in conv.VALID_UNITS:
            return conv.UNIT_CLASS
    if conv := STATISTIC_UNIT_TO_UNIT_CONVERTER.get(unit):
        return conv.UNIT_CLASS
    return None


def _converter(
    unit_class: str | None, from_unit: str | None, to_unit: str | None
) -> Callable[[float], float] | None:
    """Return a value converter, identity if units match, None if impossible."""
    if from_unit == to_unit:
        return lambda value: value
    conv = UNIT_CLASS_TO_UNIT_CONVERTER.get(unit_class)
    if conv is None or from_unit not in conv.VALID_UNITS or to_unit not in conv.VALID_UNITS:
        return None
    return conv.converter_factory(from_unit, to_unit)


async def async_write_hours(
    hass: HomeAssistant,
    entity: MappedEntity,
    hours: dict[int, HourlyAggregate],
    owned_hours: set[int],
    prefer_sd_data: bool,
    dry_run: bool = False,
) -> WriteResult:
    """Import ``hours`` for one entity.

    Hours that already have a statistics row compiled by HA from live states
    are left alone unless ``prefer_sd_data`` is set. Hours this integration
    wrote earlier (``owned_hours``) are rewritten with the merged aggregate.
    """
    result = WriteResult()
    if not hours:
        return result
    statistic_id = entity.entity_id
    instance = get_instance(hass)

    existing_meta = await instance.async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={statistic_id})
    )
    if statistic_id in existing_meta:
        meta = existing_meta[statistic_id][1]
        if meta["mean_type"] == StatisticMeanType.NONE:
            result.error = f"{statistic_id} has statistics without a mean; skipped"
            return result
        metadata = StatisticMetaData(
            mean_type=meta["mean_type"],
            has_sum=meta["has_sum"],
            name=meta["name"],
            source=meta["source"],
            statistic_id=statistic_id,
            unit_class=meta["unit_class"],
            unit_of_measurement=meta["unit_of_measurement"],
        )
        if meta["source"] != RECORDER_SOURCE:
            result.error = f"{statistic_id} statistics have source {meta['source']}"
            return result
    else:
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.ARITHMETIC,
            has_sum=False,
            name=None,
            source=RECORDER_SOURCE,
            statistic_id=statistic_id,
            unit_class=unit_class_for(entity.device_class, entity.state_unit),
            unit_of_measurement=entity.state_unit,
        )

    convert = _converter(
        metadata["unit_class"], entity.native_unit, metadata["unit_of_measurement"]
    )
    if convert is None:
        result.error = (
            f"cannot convert {entity.native_unit} to "
            f"{metadata['unit_of_measurement']} for {statistic_id}"
        )
        return result

    first, last = min(hours), max(hours)
    existing_rows = await instance.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime.fromtimestamp(first, UTC),
        datetime.fromtimestamp(last + 3600, UTC),
        {statistic_id},
        "hour",
        None,
        {"mean"},
    )
    existing = {int(row["start"]) for row in existing_rows.get(statistic_id, [])}

    stats: list[StatisticData] = []
    for hour in sorted(hours):
        if hour in existing and hour not in owned_hours and not prefer_sd_data:
            result.skipped_existing.append(hour)
            continue
        agg = hours[hour]
        lo, hi = convert(agg.minimum), convert(agg.maximum)
        stats.append(
            StatisticData(
                start=datetime.fromtimestamp(hour, UTC),
                mean=convert(agg.mean),
                min=min(lo, hi),
                max=max(lo, hi),
            )
        )
        result.written.append(hour)

    if stats and not dry_run:
        async_import_statistics(hass, metadata, stats)
    _LOGGER.debug(
        "%s %s: %d hours written, %d skipped (already compiled by HA)",
        "Dry run" if dry_run else "Imported",
        statistic_id,
        len(result.written),
        len(result.skipped_existing),
    )
    return result
