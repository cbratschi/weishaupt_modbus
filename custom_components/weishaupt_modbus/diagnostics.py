"""Diagnostics support for Weishaupt Modbus."""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from .configentry import MyData
from .const import CONF

TO_REDACT = {
    CONF.HOST,
    CONF.MAC,
    CONF.PASSWORD,
    CONF.USERNAME,
    CONF.WEBIF_TOKEN,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry[MyData],
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime_data = entry.runtime_data
    coordinator = runtime_data.weishaupt_coordinator
    wsb_webif = runtime_data.wsb_coordinator

    return {
        "config_entry": async_redact_data(entry.data, TO_REDACT),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "data": {
                "updated": (
                    sorted(coordinator.data.updated)
                    if coordinator.last_update_success and coordinator.data is not None
                    else []
                ),
                "failed": coordinator.failed_reports,
            },
        },
        "webif": {
            "enabled": runtime_data.webif_api is not None,
            "coordinator_available": runtime_data.webif_coordinator is not None
            or wsb_webif is not None,
            "wsb_last_update_success": (
                wsb_webif.last_update_success if wsb_webif is not None else None
            ),
            "wsb_available_fields": (
                sorted(wsb_webif.data or {})
                if wsb_webif is not None and wsb_webif.last_update_success
                else []
            ),
        },
    }
