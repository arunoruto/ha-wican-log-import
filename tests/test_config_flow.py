"""Config and options flow tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wican_log_import.api import (
    LogListing,
    WiCanConnectionError,
    WiCanNotSupportedError,
)
from custom_components.wican_log_import.const import DOMAIN

CLIENT = "custom_components.wican_log_import.config_flow.WiCanClient"


async def test_flow_reachable(hass: HomeAssistant) -> None:
    with (
        patch(f"{CLIENT}.async_get_status", AsyncMock(return_value={})),
        patch(f"{CLIENT}.async_list_logs", AsyncMock(return_value=LogListing(None, []))),
        patch("custom_components.wican_log_import.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.118"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"host": "192.168.1.118"}
    assert result["result"].unique_id == "192.168.1.118"


async def test_flow_asleep_can_save_anyway(hass: HomeAssistant) -> None:
    with (
        patch(f"{CLIENT}.async_get_status", AsyncMock(side_effect=WiCanConnectionError)),
        patch("custom_components.wican_log_import.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.118"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "unreachable"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_flow_logs_not_supported(hass: HomeAssistant) -> None:
    with (
        patch(f"{CLIENT}.async_get_status", AsyncMock(return_value={})),
        patch(f"{CLIENT}.async_list_logs", AsyncMock(side_effect=WiCanNotSupportedError("x", 404))),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.118"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "logs_not_supported"}


async def test_flow_duplicate(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="192.168.1.118", data={"host": "192.168.1.118"}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "http://192.168.1.118/"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "192.168.1.118"})
    entry.add_to_hass(hass)
    with patch("custom_components.wican_log_import.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "poll_interval": 120,
                "trip_gap": 15,
                "prefer_sd_data": True,
                "entity_overrides": "ODOMETER=sensor.odo",
                "ignored_params": "*PIDsSupported*",
                "param_odometer": "ODOMETER",
                "param_coolant": "05-EngineCoolantTemp",
                "param_voltage": "42-ControlModuleVolt",
                "param_fuel_rate": "5E-EngineFuelRate",
                "param_intake_temp": "0F-IntakeAirTemperature",
                "param_distance_mil": "21-DistanceMILOn",
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["trip_gap"] == 15
