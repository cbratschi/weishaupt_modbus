"""Read WSB pump settings, measurements and counters from WEM Lokal."""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging
import math
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
from homeassistant.const import PERCENTAGE, UnitOfTime, UnitOfVolumeFlowRate
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

_LOGGER = logging.getLogger(__name__)
_ROOT = "/settings_export.html"
WSB_REQUEST_DELAY_SECONDS = 1.0
_REQUEST_TIMEOUT = 10
_EXPORT_LINK = re.compile(r"/settings_export\.html\?stack=[0-9a-fA-F,]+")
_CONTROLLER_FIRMWARE = re.compile(r"\bWWP-CPU COM V(\d+\.\d+) Rev\.\s*(\d+)\b")
_FLOW_VALUE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m\s*[3³]\s*/\s*h")


@dataclass(frozen=True)
class WsbField:
    """One named WEM value and its entity identity."""

    report: str
    key: str
    label: str
    unit: str | None = None
    choices: tuple[tuple[str, str], ...] = ()
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass = SensorStateClass.MEASUREMENT


_PUMP = (
    WsbField("heat_pump", "heating_pump_power_setpoint", "Leistung Heizen", PERCENTAGE),
    WsbField(
        "heat_pump", "hot_water_pump_power_setpoint", "Leistung Warmwasser", PERCENTAGE
    ),
    WsbField("heat_pump", "cooling_pump_power_setpoint", "Leistung Kühlen", PERCENTAGE),
)
_PUMP_REGULATION = (
    ("Konstantbetrieb", "constant_speed"),
    ("Volumenstrom", "flow_rate"),
)
_PUMP_SETTINGS = (
    WsbField(
        "heat_pump",
        "pump_regulation_heating",
        "Regelungsart Heizen",
        choices=_PUMP_REGULATION,
    ),
    WsbField(
        "heat_pump",
        "pump_regulation_hot_water",
        "Regelungsart WW",
        choices=_PUMP_REGULATION,
    ),
    WsbField(
        "heat_pump",
        "pump_regulation_cooling",
        "Regelungsart Kühlen",
        choices=_PUMP_REGULATION,
    ),
    WsbField(
        "heat_pump",
        "pump_function",
        "Funktion",
        choices=(("Zubringerpumpe", "feed_pump"), ("HK-Pumpe", "heating_circuit_pump")),
    ),
    WsbField(
        "heat_pump",
        "pump_utility_lock_release",
        "Freigabe bei EVU-Sperre",
        choices=(("Aus", "off"), ("Ein", "on")),
    ),
)
_HEATER_COUNTERS = (
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
    WsbField(
        "second_heat_source",
        "electric_heater_1_switch_cycles",
        "Schaltspiele E1",
        "",
    ),
)
_HEAT_PUMP_INFO = (
    WsbField(
        "heat_pump",
        "actual_flow_rate",
        "Volumenstrom",
        UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR,
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
    ),
    WsbField("heat_pump", "actual_pump_speed", "Drehzahl Pumpe M1", PERCENTAGE),
    WsbField(
        "heat_pump",
        "compressor_starts",
        "Schaltspiele Verdichter",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    WsbField(
        "heat_pump",
        "defrost_cycles",
        "Schaltspiele Abtauen",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
)
WSB_FIELDS = _PUMP + _PUMP_SETTINGS + _HEATER_COUNTERS + _HEAT_PUMP_INFO


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


def _pump_values(page: BeautifulSoup) -> dict[str, int | str]:
    labels = [" ".join(anchor.stripped_strings) for anchor in page.find_all("a")]
    values: dict[str, int | str] = {}
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
    for field in _PUMP_SETTINGS:
        choices = dict(field.choices)
        prefix = field.label + " "
        matches = [
            label.removeprefix(prefix) for label in labels if label.startswith(prefix)
        ]
        if len(matches) == 1 and matches[0] in choices:
            values[field.key] = choices[matches[0]]
    return values


def _browse_value(page: BeautifulSoup, label: str) -> str | None:
    matches = []
    for row in page.select("div.nav-link.browseobj"):
        heading = row.find("h5")
        if heading and heading.get_text(" ", strip=True) == label:
            matches.append(row.get_text(" ", strip=True).removeprefix(label).strip())
    return matches[0] if len(matches) == 1 else None


def _hour_values(page: BeautifulSoup) -> dict[str, int]:
    values = {}
    for field in _HEATER_COUNTERS:
        raw = _browse_value(page, field.label)
        pattern = r"(\d+)\s*h" if field.unit == UnitOfTime.HOURS else r"(\d+)"
        if raw is not None and (match := re.fullmatch(pattern, raw)):
            values[field.key] = int(match[1])
    return values


def _heat_pump_values(page: BeautifulSoup) -> dict[str, int | float]:
    values: dict[str, int | float] = {}
    for field in _HEAT_PUMP_INFO:
        raw = _browse_value(page, field.label)
        if raw is None:
            continue
        if field.device_class == SensorDeviceClass.VOLUME_FLOW_RATE:
            if match := _FLOW_VALUE.fullmatch(raw):
                flow = float(match[1].replace(",", "."))
                if math.isfinite(flow):
                    values[field.key] = flow
        elif field.unit == PERCENTAGE:
            if (match := re.fullmatch(r"(\d{1,3})\s*%", raw)) and int(match[1]) <= 100:
                values[field.key] = int(match[1])
        elif re.fullmatch(r"\d+", raw):
            values[field.key] = int(raw)
    return values


class WsbWebifCoordinator(DataUpdateCoordinator[dict[str, int | float | str]]):
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
        async with self.mcu_lock, asyncio.timeout(_REQUEST_TIMEOUT):
            response = await self.api._request("GET", path)  # noqa: SLF001
        if response.status_code != 200:
            raise ValueError("WEM export unavailable")
        return BeautifulSoup(response.text, "html.parser")

    async def _async_update_data(self) -> dict[str, int | float | str]:
        if self.api is None:
            return {}
        values: dict[str, int | float | str] = {}
        try:
            root = await self._get_page(_ROOT)
        except (TimeoutError, ValueError, WeishauptWebifError) as err:
            _LOGGER.debug("WSB WEM root unavailable (%s)", type(err).__name__)
            return values
        if match := _CONTROLLER_FIRMWARE.search(root.get_text(" ", strip=True)):
            self.controller_firmware = f"WWP-CPU COM V{match[1]} R{match[2]}"
        for first, pages in (
            ("Wärmepumpe", (("Pumpe", _pump_values),)),
            ("Info", (("2. WEZ", _hour_values), ("Wärmepumpe", _heat_pump_values))),
        ):
            try:
                section = await self._get_page(_menu(root, first))
            except (TimeoutError, ValueError, WeishauptWebifError) as err:
                _LOGGER.debug(
                    "WSB WEM menu %s unavailable (%s)", first, type(err).__name__
                )
                continue
            for second, parse in pages:
                try:
                    page = await self._get_page(_menu(section, second))
                    values.update(parse(page))
                except (TimeoutError, ValueError, WeishauptWebifError) as err:
                    _LOGGER.debug(
                        "WSB WEM menu %s/%s unavailable (%s)",
                        first,
                        second,
                        type(err).__name__,
                    )
        return values


class WsbWebifSensor(CoordinatorEntity[WsbWebifCoordinator], SensorEntity):
    """A confirmed WEM field in its existing Weishaupt device group."""

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
        if field.choices:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = [key for _, key in field.choices]
        else:
            self._attr_device_class = field.device_class
            self._attr_state_class = field.state_class
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
    def native_value(self) -> int | float | str | None:
        """Return the value from the current poll."""
        return (self.coordinator.data or {}).get(self.field.key)
