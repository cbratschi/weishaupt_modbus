"""my config entry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from weishaupt_webif_api import WebifConnection

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF
from .modbus.coordinator import WeishauptCoordinator
from .webif.coordinator import WeishauptWebifCoordinator

@dataclass
class MyData:
    """My config data."""

    config_dir: str
    hass: HomeAssistant
    powermap: Any
    mcu_lock: asyncio.Lock
    weishaupt_coordinator: WeishauptCoordinator
    webif_api: WebifConnection | None = None
    webif_coordinator: WeishauptWebifCoordinator | None = None


type MyConfigEntry = ConfigEntry[MyData]


def entity_identity(entry: MyConfigEntry) -> str:
    """Return the stable registry identity, independently of reconfiguration."""
    if seed := entry.data.get(CONF.IDENTITY_SEED):
        return seed
    if (mac := entry.data.get(CONF.MAC)) and mac != "CHANGEME":
        return mac
    return f"entry_{entry.entry_id}"
