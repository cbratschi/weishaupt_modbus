"""Read the WSB pump setpoints and operating hours from WEM Lokal."""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging
import re

from bs4 import BeautifulSoup
from weishaupt_webif_api import WebifConnection
from weishaupt_webif_api.exceptions import WeishauptWebifError

from custom_components.weishaupt_modbus.configentry import (
    MyConfigEntry,
    entity_identity,
)
from custom_components.weishaupt_modbus.const import CONST
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

_LOGGER = logging.getLogger(__name__)
_ROOT = "/settings_export.html"
WSB_REQUEST_DELAY_SECONDS = 1.0
_EXPORT_LINK = re.compile(r"/settings_export\.html\?stack=[0-9a-fA-F,]+")
_CONTROLLER_FIRMWARE = re.compile(r"\bWWP-CPU COM V(\d+\.\d+) Rev\.\s*(\d+)\b")


@dataclass(frozen=True)
class WsbField:
    """One named WEM value and its existing entity identity."""

    report: str
    key: str
    label: str
    unit: str


_PUMP = (
    WsbField("heat_pump", "heating_pump_power_setpoint", "Leistung Heizen", PERCENTAGE),
    WsbField(
        "heat_pump", "hot_water_pump_power_setpoint", "Leistung Warmwasser", PERCENTAGE
    ),
    WsbField("heat_pump", "cooling_pump_power_setpoint", "Leistung Kühlen", PERCENTAGE),
)
_HOURS = (
    WsbField(
        "second_heat_source",
        "electric_heater_1_operating_hours",
        "Betriebsstunden E1",
        UnitOfTime.HOURS,
    ),
    WsbField(
        "second_heat_source",
        "electric_heater_2_operating_hours",
        "Betriebsstunden E2",
        UnitOfTime.HOURS,
    ),
)
WSB_FIELDS = _PUMP + _HOURS


def _menu(page: BeautifulSoup, label: str) -> str:
    links = [
        str(link["href"])
        for link in page.find_all("a", href=_EXPORT_LINK)
        if _EXPORT_LINK.fullmatch(str(link["href"]))
        and " ".join(link.stripped_strings) == label
    ]
    if not links:
        raise ValueError(f"Missing WEM menu {label}")
    # This controller presents the same second-generator menu twice. Its last
    # link contains the full page, including the operating-hour counters.
    return links[-1]


def _pump_values(page: BeautifulSoup) -> dict[str, int]:
    labels = [" ".join(anchor.stripped_strings) for anchor in page.find_all("a")]
    values = {}
    for field in _PUMP:
        matches = [
            match
            for label in labels
            if (
                match := re.fullmatch(
                    rf"{re.escape(field.label)}\s+(\d{{1,3}})\s*%", label
                )
            )
        ]
        if len(matches) == 1 and (value := int(matches[0][1])) <= 100:
            values[field.key] = value
    return values


def _hour_values(page: BeautifulSoup) -> dict[str, int]:
    values = {}
    for field in _HOURS:
        matches = []
        for row in page.select("div.nav-link.browseobj"):
            heading = row.find("h5")
            if heading and heading.get_text(strip=True) == field.label:
                matches.append(
                    row.get_text(" ", strip=True).removeprefix(field.label).strip()
                )
        if len(matches) == 1 and (match := re.fullmatch(r"(\d+)\s*h", matches[0])):
            values[field.key] = int(match[1])
    return values


class WsbWebifCoordinator(DataUpdateCoordinator[dict[str, int]]):
    """Keep WEM availability independent of the Modbus coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: WebifConnection | None,
        mcu_lock: asyncio.Lock,
    ) -> None:
        """Set up WEM polling."""
        super().__init__(
            hass,
            _LOGGER,
            name="Weishaupt WSB WEM",
            update_interval=timedelta(minutes=5),
        )
        self.api = api
        self.mcu_lock = mcu_lock
        self.controller_firmware: str | None = None

    async def _get_page(self, path: str) -> BeautifulSoup:
        if self.api is None:
            raise ValueError("WEM is disabled")
        # The pinned client handles pacing and reauthentication. The profile
        # follows only same-device export menu links and never submits settings.
        response = await self.api._request("GET", path)  # noqa: SLF001
        if response.status_code != 200:
            raise ValueError("WEM export unavailable")
        return BeautifulSoup(response.text, "html.parser")

    async def _async_update_data(self) -> dict[str, int]:
        if self.api is None:
            return {}
        values = {}
        async with self.mcu_lock:
            try:
                async with asyncio.timeout(6):
                    root = await self._get_page(_ROOT)
            except (TimeoutError, ValueError, WeishauptWebifError) as err:
                _LOGGER.debug("WSB WEM root unavailable (%s)", type(err).__name__)
                return values
            if match := _CONTROLLER_FIRMWARE.search(root.get_text(" ", strip=True)):
                self.controller_firmware = f"WWP-CPU COM V{match[1]} R{match[2]}"
            for first, second, parse in (
                ("Wärmepumpe", "Pumpe", _pump_values),
                ("Info", "2. WEZ", _hour_values),
            ):
                try:
                    async with asyncio.timeout(6):
                        section = await self._get_page(_menu(root, first))
                        page = await self._get_page(_menu(section, second))
                    values.update(parse(page))
                except (TimeoutError, ValueError, WeishauptWebifError) as err:
                    _LOGGER.debug(
                        "WSB WEM menu %s unavailable (%s)", second, type(err).__name__
                    )
        return values


class WsbWebifSensor(CoordinatorEntity[WsbWebifCoordinator], SensorEntity):
    """A confirmed WEM field with the original Modbus sensor's registry ID."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WsbWebifCoordinator,
        entry: MyConfigEntry,
        field: WsbField,
    ) -> None:
        """Set up the sensor and its historical identity."""
        super().__init__(coordinator)
        self.field = field
        identity = entity_identity(entry)
        self._attr_unique_id = f"{identity}_{field.report}_{field.key}"
        self._attr_translation_key = f"{field.report}_{field.key}"
        self._attr_native_unit_of_measurement = field.unit
        self._attr_state_class = SensorStateClass.MEASUREMENT
        if field.unit == UnitOfTime.HOURS:
            self._attr_device_class = SensorDeviceClass.DURATION
            # The stable sensor recorded hourly statistics as measurements.
            # Preserve that contract across the change of data source.
            self._attr_suggested_display_precision = 0
        self._attr_device_info = DeviceInfo(
            identifiers={(CONST.DOMAIN, f"{identity}_{field.report}")},
            translation_key=f"dev_{field.report}",
            manufacturer="Weishaupt",
        )

    @property
    def available(self) -> bool:
        """Do not present a previous reading after a failed or partial poll."""
        return super().available and self.field.key in (self.coordinator.data or {})

    @property
    def native_value(self) -> int | None:
        """Return the value from the current poll."""
        return (self.coordinator.data or {}).get(self.field.key)
