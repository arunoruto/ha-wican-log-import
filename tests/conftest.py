"""Shared fixtures."""

from __future__ import annotations

from typing import Any

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: Any, enable_custom_integrations: None
) -> None:
    """Run every test with a recorder (a manifest dependency) and custom integrations.

    recorder_mock must be requested before hass is created.
    """
