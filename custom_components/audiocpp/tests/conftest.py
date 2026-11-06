"""Pytest setup for the audio.cpp integration tests."""

import pytest


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Give each test its own config dir (pytest-homeassistant doesn't create one)."""
    return hass_tmp_config_dir
