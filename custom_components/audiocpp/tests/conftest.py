"""Pytest setup for the audio.cpp integration tests."""

from importlib.util import find_spec

import pytest

# From HA 2026.11 the harness is core's pytest-homeassistant, which registers
# itself; before that, pytest-homeassistant-custom-component is loaded by name.
if find_spec("pytest_homeassistant") is None:
    pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Give each test its own config dir (pytest-homeassistant doesn't create one)."""
    return hass_tmp_config_dir
