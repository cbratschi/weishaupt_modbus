"""Tests for the Weishaupt coordinator."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.weishaupt_modbus.modbus.coordinator import WeishauptCoordinator
from custom_components.weishaupt_modbus.modbus.weishaupt_modbus_client.model.modbus_connection_device import (
    UpdateReport,
)
from homeassistant.core import HomeAssistant


@pytest.mark.asyncio
async def test_partial_update_failure_is_logged(tmp_path, caplog):
    """Test that failed reports are logged."""
    device = MagicMock()

    error = RuntimeError("test failure")
    device.async_update = AsyncMock(
        return_value=UpdateReport(
            updated={"system"},
            failed={"heat_pump": error},
        )
    )

    coordinator = WeishauptCoordinator(
        hass=HomeAssistant(str(tmp_path)),
        device=device,
        mcu_lock=asyncio.Lock(),
    )

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert caplog.text.count("Failed to update Weishaupt reports: heat_pump: RuntimeError") == 1
    assert "test failure" not in caplog.text


@pytest.mark.asyncio
async def test_total_failure_and_recovery(tmp_path):
    """Report an outage to HA and accept fresh data after recovery."""
    device = MagicMock()
    device.async_update = AsyncMock(
        side_effect=[
            UpdateReport(updated={"system"}, failed={}),
            UpdateReport(updated=set(), failed={"system": TimeoutError("private detail")}),
            UpdateReport(updated={"system"}, failed={}),
        ]
    )
    coordinator = WeishauptCoordinator(
        hass=HomeAssistant(str(tmp_path)), device=device, mcu_lock=asyncio.Lock()
    )
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert coordinator.failed_reports == {"system": "TimeoutError"}
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert not coordinator.failed_reports
    assert coordinator.data.updated == {"system"}
