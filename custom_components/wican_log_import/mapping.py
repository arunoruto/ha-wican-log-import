"""Map WiCAN log parameter names to entities of the official integration.

This layer is specific to running alongside ``jay-oswald/ha-wican``. Inside
that integration it is unnecessary, because it owns the entities.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field

from homeassistant.components.sensor import ATTR_STATE_CLASS, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .api import normalize_base_url
from .const import ENCODED_UNIT, WICAN_DOMAIN

_LOGGER = logging.getLogger(__name__)

PID_UNIQUE_ID_MARKER = "_pid_"


@dataclass(frozen=True, slots=True)
class MappedEntity:
    """An entity that hourly statistics can be imported for."""

    entity_id: str
    # Unit the device reports (and the SD log uses).
    native_unit: str | None
    # Unit of the entity state, which HA uses for new statistics metadata.
    state_unit: str | None
    device_class: str | None


@dataclass(slots=True)
class ParameterMapping:
    """Result of mapping log parameter names to entities."""

    entities: dict[str, MappedEntity] = field(default_factory=dict)
    # Parameter names grouped by why they are not imported.
    ignored: set[str] = field(default_factory=set)
    unmapped: set[str] = field(default_factory=set)
    encoded: set[str] = field(default_factory=set)
    no_state_class: dict[str, str] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)

    def skipped_summary(self) -> dict[str, list[str]]:
        """Return the skipped parameters for diagnostics."""
        return {
            "ignored": sorted(self.ignored),
            "unmapped": sorted(self.unmapped),
            "encoded": sorted(self.encoded),
            "no_state_class": sorted(self.no_state_class.values()),
            "unavailable": sorted(self.unavailable.values()),
        }


def parse_overrides(text: str) -> dict[str, str]:
    """Parse ``name=entity_id`` lines. Blank lines and ``#`` comments allowed."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        name, entity_id = (part.strip() for part in line.split("=", 1))
        if name and entity_id:
            result[name] = entity_id
    return result


def parse_patterns(text: str) -> list[str]:
    """Parse a comma or newline separated list of names/glob patterns."""
    return [p.strip() for p in text.replace(",", "\n").splitlines() if p.strip()]


def is_ignored(name: str, patterns: list[str]) -> bool:
    """Return True if a parameter name matches any ignore pattern."""
    lname = name.lower()
    return any(fnmatch.fnmatchcase(lname, p.lower()) for p in patterns)


def wican_entry_ids(hass: HomeAssistant, address: str) -> set[str] | None:
    """Return the wican config entries for our device, or None for all."""
    entries: list[ConfigEntry] = hass.config_entries.async_entries(WICAN_DOMAIN)
    if len(entries) <= 1:
        return None
    try:
        ours = normalize_base_url(address).split("://", 1)[1].lower()
    except ValueError:
        return None
    matched = set()
    for entry in entries:
        candidates = {
            str(entry.data.get(key, "")).lower().rstrip(".")
            for key in ("ip", "host", "mdns")
        }
        candidates |= {c.split("://", 1)[-1] for c in candidates}
        if ours.rstrip(".") in candidates:
            matched.add(entry.entry_id)
    return matched or None


def _param_name(entry: er.RegistryEntry) -> str | None:
    if PID_UNIQUE_ID_MARKER in entry.unique_id:
        return entry.unique_id.split(PID_UNIQUE_ID_MARKER, 1)[1]
    return entry.original_name


def build_mapping(
    hass: HomeAssistant,
    address: str,
    names: set[str],
    overrides: dict[str, str],
    ignore_patterns: list[str],
) -> ParameterMapping:
    """Map log parameter names to importable entities."""
    registry = er.async_get(hass)
    entry_ids = wican_entry_ids(hass, address)

    by_name: dict[str, str] = {}
    for entry in registry.entities.values():
        if entry.platform != WICAN_DOMAIN or entry.domain != Platform.SENSOR:
            continue
        if entry_ids is not None and entry.config_entry_id not in entry_ids:
            continue
        for key in {_param_name(entry), entry.original_name}:
            if key:
                by_name.setdefault(key, entry.entity_id)
                by_name.setdefault(key.lower(), entry.entity_id)

    result = ParameterMapping()
    for name in sorted(names):
        if name in overrides:
            entity_id = overrides[name]
        elif is_ignored(name, ignore_patterns):
            result.ignored.add(name)
            continue
        else:
            entity_id = by_name.get(name) or by_name.get(name.lower())
        if entity_id is None:
            result.unmapped.add(name)
            continue

        state = hass.states.get(entity_id)
        reg_entry = registry.async_get(entity_id)
        if state is None and reg_entry is None:
            result.unavailable[name] = entity_id
            continue
        attrs = state.attributes if state is not None else {}
        state_unit = attrs.get(ATTR_UNIT_OF_MEASUREMENT)
        native_unit = (
            reg_entry.unit_of_measurement if reg_entry is not None else None
        ) or state_unit
        if state_unit is None:
            state_unit = native_unit
        unit = native_unit
        if isinstance(unit, str) and unit.lower() == ENCODED_UNIT:
            result.encoded.add(name)
            continue
        state_class = attrs.get(ATTR_STATE_CLASS)
        if state_class is None and reg_entry is not None and reg_entry.capabilities:
            state_class = reg_entry.capabilities.get(ATTR_STATE_CLASS)
        if state_class != SensorStateClass.MEASUREMENT:
            result.no_state_class[name] = entity_id
            continue
        device_class = attrs.get(ATTR_DEVICE_CLASS)
        if device_class is None and reg_entry is not None:
            device_class = reg_entry.device_class or reg_entry.original_device_class
        result.entities[name] = MappedEntity(
            entity_id, native_unit, state_unit, device_class
        )
    return result
