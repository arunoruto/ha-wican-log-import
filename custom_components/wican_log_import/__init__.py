"""Import WiCAN Pro SD card trip logs into Home Assistant statistics."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import STATE_ON, Platform
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.typing import ConfigType

from .api import WiCanClient
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    CONF_HOST,
    DOMAIN,
    SERVICE_DRY_RUN,
    SERVICE_IMPORT_NOW,
    WICAN_DOMAIN,
)
from .coordinator import WiCanLogCoordinator
from .mapping import wican_entry_ids

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_REIMPORT = "reimport"
SERVICE_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})

type WiCanLogConfigEntry = ConfigEntry[WiCanLogCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services."""

    def _coordinators(call: ServiceCall) -> list[WiCanLogCoordinator]:
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        entries = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.state is ConfigEntryState.LOADED
            and (entry_id is None or e.entry_id == entry_id)
        ]
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_loaded_entry"
            )
        return [e.runtime_data for e in entries]

    async def _dry_run(call: ServiceCall) -> ServiceResponse:
        reports = {}
        for coordinator in _coordinators(call):
            report = await coordinator.async_run_cycle(dry_run=True)
            _LOGGER.info("Dry run for %s: %s", coordinator.name, report)
            reports[coordinator.config_entry.entry_id] = report
        return {"entries": reports}

    async def _import_now(call: ServiceCall) -> None:
        for coordinator in _coordinators(call):
            await coordinator.async_refresh()

    async def _reimport(call: ServiceCall) -> None:
        for coordinator in _coordinators(call):
            await coordinator.async_reset_progress()

    hass.services.async_register(
        DOMAIN,
        SERVICE_DRY_RUN,
        _dry_run,
        schema=SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_IMPORT_NOW, _import_now, schema=SERVICE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REIMPORT, _reimport, schema=SERVICE_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: WiCanLogConfigEntry) -> bool:
    """Set up one WiCAN device."""
    client = WiCanClient(async_get_clientsession(hass), entry.data[CONF_HOST])
    coordinator = WiCanLogCoordinator(hass, entry, client)
    await coordinator.async_load()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Start a check as soon as the official integration reports the ECU awake,
    # so imports begin while the short wake window is still open.
    ecu_entities = _ecu_status_entities(hass, entry.data[CONF_HOST])
    if ecu_entities:

        @callback
        def _ecu_changed(event: Event[EventStateChangedData]) -> None:
            new = event.data["new_state"]
            old = event.data["old_state"]
            if new is not None and new.state == STATE_ON and (
                old is None or old.state != STATE_ON
            ):
                _LOGGER.debug("%s turned on, checking for logs", new.entity_id)
                hass.async_create_task(coordinator.async_request_refresh())

        entry.async_on_unload(
            async_track_state_change_event(hass, ecu_entities, _ecu_changed)
        )

    # First check in the background; setup must not wait for slow downloads.
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"{DOMAIN}_first_refresh"
    )
    return True


def _ecu_status_entities(hass: HomeAssistant, address: str) -> list[str]:
    registry = er.async_get(hass)
    entry_ids = wican_entry_ids(hass, address)
    return [
        e.entity_id
        for e in registry.entities.values()
        if e.platform == WICAN_DOMAIN
        and e.domain == Platform.BINARY_SENSOR
        and e.unique_id.endswith("_ecu_status")
        and (entry_ids is None or e.config_entry_id in entry_ids)
    ]


async def _async_options_updated(hass: HomeAssistant, entry: WiCanLogConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: WiCanLogConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
