"""Config and options flow for WiCAN log import."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .api import (
    WiCanClient,
    WiCanConnectionError,
    WiCanError,
    WiCanNotSupportedError,
    normalize_base_url,
)
from .const import (
    CONF_ENTITY_OVERRIDES,
    CONF_HOST,
    CONF_IGNORED_PARAMS,
    CONF_POLL_INTERVAL,
    CONF_PREFER_SD_DATA,
    CONF_TRIP_GAP,
    DEFAULT_IGNORED_PARAMS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PREFER_SD_DATA,
    DEFAULT_TRIP_GAP,
    DEFAULT_TRIP_PARAMS,
    DOMAIN,
    LIST_TIMEOUT,
    PROBE_TIMEOUT,
    WICAN_DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _host_key(address: str) -> str:
    return normalize_base_url(address).split("://", 1)[1].lower()


class WiCanLogImportConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up one WiCAN device."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._host: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the WiCAN address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                key = _host_key(host)
            except ValueError:
                errors[CONF_HOST] = "invalid_host"
            else:
                await self.async_set_unique_id(key)
                self._abort_if_unique_id_configured()
                self._host = host
                client = WiCanClient(async_get_clientsession(self.hass), host)
                try:
                    await client.async_get_status(PROBE_TIMEOUT + 2)
                except WiCanConnectionError:
                    return await self.async_step_unreachable()
                except WiCanError:
                    errors["base"] = "unexpected_response"
                else:
                    try:
                        await client.async_list_logs(LIST_TIMEOUT)
                    except WiCanNotSupportedError:
                        errors["base"] = "logs_not_supported"
                    except WiCanError:
                        # Reachable but listing failed (slow ESP32); accept.
                        _LOGGER.debug("Listing logs failed during setup")
                    if not errors:
                        return self._create(host)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_HOST, default=self._suggested_host()): str}
            ),
            errors=errors,
        )

    async def async_step_unreachable(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The device is asleep (normal). Offer to save anyway."""
        assert self._host is not None
        if user_input is not None:
            return self._create(self._host)
        return self.async_show_form(
            step_id="unreachable",
            description_placeholders={"host": self._host},
        )

    def _create(self, host: str) -> ConfigFlowResult:
        return self.async_create_entry(
            title=f"WiCAN logs {host}", data={CONF_HOST: host}
        )

    def _suggested_host(self) -> str:
        """Prefill with the IP the official integration knows, if any."""
        for entry in self.hass.config_entries.async_entries(WICAN_DOMAIN):
            if ip := entry.data.get("ip"):
                return str(ip)
        return ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return WiCanLogImportOptionsFlow()


class WiCanLogImportOptionsFlow(OptionsFlow):
    """Options: intervals, mapping overrides, ignore list, trip parameters."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        opts = self.config_entry.options
        multiline = TextSelector(TextSelectorConfig(multiline=True))
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_POLL_INTERVAL,
                default=opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=15, max=3600, step=1, unit_of_measurement="s",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_TRIP_GAP, default=opts.get(CONF_TRIP_GAP, DEFAULT_TRIP_GAP)
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=240, step=1, unit_of_measurement="min",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_PREFER_SD_DATA,
                default=opts.get(CONF_PREFER_SD_DATA, DEFAULT_PREFER_SD_DATA),
            ): BooleanSelector(),
            vol.Optional(
                CONF_ENTITY_OVERRIDES,
                description={
                    "suggested_value": opts.get(CONF_ENTITY_OVERRIDES, "")
                },
            ): multiline,
            vol.Optional(
                CONF_IGNORED_PARAMS,
                default=opts.get(
                    CONF_IGNORED_PARAMS, "\n".join(DEFAULT_IGNORED_PARAMS)
                ),
            ): multiline,
        }
        for key, default in DEFAULT_TRIP_PARAMS.items():
            schema[vol.Required(key, default=opts.get(key, default))] = str
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
