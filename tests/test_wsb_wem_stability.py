"""Replay incomplete WEM pages without connecting to a heat pump."""

import asyncio
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.weishaupt_modbus.webif.wsb import WsbWebifCoordinator
from homeassistant.core import HomeAssistant

ROOT = "/settings_export.html"
PUMP_MENU = f"{ROOT}?stack=01"
PUMP = f"{PUMP_MENU},02"
INFO_MENU = f"{ROOT}?stack=03"
HEATER = f"{INFO_MENU},04"
HEAT_PUMP = f"{INFO_MENU},05"
HEATING_CIRCUIT = f"{INFO_MENU},06"
MAIN_HEATER = f"{ROOT}?stack=07"
MAIN_HEATING_CIRCUIT = f"{ROOT}?stack=08"


def link(path: str, label: str) -> str:
    """Create an export menu link using synthetic stack codes."""
    return f'<a href="{path}"><h5>{label}</h5></a>'


def info(label: str, value: str) -> str:
    """Create a WEM Info row."""
    return f'<div class="nav-link browseobj"><h5>{label}</h5>{value}</div>'


class WemPages:
    """Return the recorded page shapes with synthetic values and link IDs."""

    def __init__(self, lock: asyncio.Lock) -> None:
        """Initialize a healthy controller."""
        self.lock = lock
        self.requests: list[str] = []
        self.next_response: dict[str, str] = {}
        self.main_menu = (
            link(PUMP_MENU, "Wärmepumpe")
            + link(INFO_MENU, "Info")
            + link(MAIN_HEATER, "2. WEZ")
            + link(MAIN_HEATING_CIRCUIT, "Heizkreis")
        )
        self.pages = {
            ROOT: self.main_menu,
            PUMP_MENU: link(PUMP, "Pumpe"),
            PUMP: "<a>Regelungsart Heizen Konstantbetrieb</a>",
            INFO_MENU: (
                self.main_menu
                + link(HEATER, "2. WEZ")
                + link(HEAT_PUMP, "Wärmepumpe")
                + link(HEATING_CIRCUIT, "Heizkreis")
            ),
            MAIN_HEATER: self.main_menu,
            MAIN_HEATING_CIRCUIT: self.main_menu,
            HEATER: info("Betriebsstunden E1", "37 h"),
            HEAT_PUMP: (
                info("Ist Frequenz Verdichter", "20 Hz")
                + info("Volumenstrom", "1.0 m³/h")
            ),
            HEATING_CIRCUIT: info("AT Mittelwert", "7 °C"),
        }

    async def _request(self, method: str, path: str) -> SimpleNamespace:
        """Serve only serialized read requests."""
        assert self.lock.locked()
        assert method == "GET"
        self.requests.append(path)
        await asyncio.sleep(0)
        return SimpleNamespace(
            status_code=200, text=self.next_response.pop(path, self.pages[path])
        )


async def setup_wem(tmp_path: Path) -> tuple[WemPages, WsbWebifCoordinator]:
    """Populate routes from a successful cold poll."""
    lock = asyncio.Lock()
    api = WemPages(lock)
    coordinator = WsbWebifCoordinator(HomeAssistant(str(tmp_path)), api, lock)
    await coordinator.async_refresh()
    assert len(api.requests) == 7
    assert coordinator.data["compressor_frequency_actual"] == 20
    assert coordinator.data["electric_heater_1_operating_hours"] == 37
    api.requests.clear()
    return api, coordinator


@pytest.mark.asyncio
async def test_cached_routes_bypass_incomplete_navigation(tmp_path: Path) -> None:
    """Keep receiving fresh values when a menu omits its child links."""
    api, coordinator = await setup_wem(tmp_path)
    api.pages[INFO_MENU] = api.main_menu
    api.pages[PUMP_MENU] = api.main_menu
    api.pages[HEAT_PUMP] = info("Ist Frequenz Verdichter", "22 Hz") + info(
        "Volumenstrom", "1.2 m³/h"
    )

    await coordinator.async_refresh()

    assert api.requests == [ROOT, PUMP, HEATER, HEAT_PUMP, HEATING_CIRCUIT]
    assert coordinator.data["compressor_frequency_actual"] == 22
    assert coordinator.data["actual_flow_rate"] == 1.2
    assert coordinator.data["outdoor_temperature_average"] == 7
    assert coordinator.data["pump_regulation_heating"] == "constant_speed"


@pytest.mark.asyncio
async def test_cold_incomplete_info_does_not_follow_main_menu_links(tmp_path: Path) -> None:
    """The same label in the main menu is not a substitute for an Info link."""
    lock = asyncio.Lock()
    api = WemPages(lock)
    api.pages[INFO_MENU] = api.main_menu
    coordinator = WsbWebifCoordinator(HomeAssistant(str(tmp_path)), api, lock)

    await coordinator.async_refresh()

    assert api.requests == [ROOT, PUMP_MENU, PUMP, INFO_MENU]
    assert coordinator.data == {"pump_regulation_heating": "constant_speed"}


@pytest.mark.asyncio
async def test_one_partial_leaf_uses_one_saved_slot_for_fresh_data(tmp_path: Path) -> None:
    """Recover missing fields without polling the whole menu again."""
    api, coordinator = await setup_wem(tmp_path)
    api.next_response[HEAT_PUMP] = info("Volumenstrom", "1.2 m³/h")
    api.pages[HEAT_PUMP] = info("Ist Frequenz Verdichter", "23 Hz")

    await coordinator.async_refresh()

    assert len(api.requests) == 6
    assert Counter(api.requests)[HEAT_PUMP] == 2
    assert coordinator.data["compressor_frequency_actual"] == 23
    assert coordinator.data["actual_flow_rate"] == 1.2


@pytest.mark.asyncio
async def test_empty_pages_respect_budget_and_rediscover_next_cycle(tmp_path: Path) -> None:
    """Expose a prolonged outage and accept relocated leaf URLs on recovery."""
    api, coordinator = await setup_wem(tmp_path)
    healthy = dict(api.pages)
    for path in (PUMP, HEATER, HEAT_PUMP, HEATING_CIRCUIT):
        api.pages[path] = "<html><body>Incomplete</body></html>"

    await coordinator.async_refresh()

    assert len(api.requests) == 7
    assert max(Counter(api.requests).values()) == 2
    assert not coordinator.last_update_success
    api.requests.clear()
    api.pages = healthy
    updated_path = f"{INFO_MENU},07"
    api.pages[INFO_MENU] = api.pages[INFO_MENU].replace(HEAT_PUMP, updated_path)
    api.pages[updated_path] = api.pages.pop(HEAT_PUMP)

    await coordinator.async_refresh()

    assert len(api.requests) == 7
    assert updated_path in api.requests
    assert coordinator.last_update_success
    assert coordinator.data["compressor_frequency_actual"] == 20


@pytest.mark.asyncio
async def test_rejected_login_does_not_probe_cached_routes(tmp_path: Path) -> None:
    """Stop at the root when WebIF requires authentication."""
    api, coordinator = await setup_wem(tmp_path)
    api.pages[ROOT] = "<title>Login</title>"

    await coordinator.async_refresh()

    assert api.requests == [ROOT]
    assert not coordinator.last_update_success


@pytest.mark.asyncio
async def test_modbus_can_acquire_lock_between_wem_requests(tmp_path: Path) -> None:
    """Yield the shared lock to a waiting Modbus poll between export pages."""
    api, coordinator = await setup_wem(tmp_path)
    original = api._request
    entered = asyncio.Event()
    release = asyncio.Event()
    modbus_seen_after = []

    async def pause_root(method: str, path: str) -> SimpleNamespace:
        if path == ROOT:
            entered.set()
            await release.wait()
        return await original(method, path)

    async def modbus_poll() -> None:
        async with api.lock:
            modbus_seen_after.append(list(api.requests))

    api._request = pause_root
    refreshing = asyncio.create_task(coordinator.async_refresh())
    await asyncio.wait_for(entered.wait(), timeout=1)
    polling = asyncio.create_task(modbus_poll())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(refreshing, polling), timeout=1)

    assert modbus_seen_after == [[ROOT]]
    assert len(api.requests) == 5
