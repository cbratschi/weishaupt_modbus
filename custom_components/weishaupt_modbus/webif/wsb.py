"""Read optional WSB values that do not have a verified Modbus mapping."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
import logging
import math
import re

from bs4 import BeautifulSoup
from weishaupt_webif_api import WebifConnection
from weishaupt_webif_api.exceptions import (
    McuResourceError,
    SessionExpiredError,
    WeishauptWebifError,
)

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
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfFrequency,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

_LOGGER = logging.getLogger(__name__)
_ROOT = "/settings_export.html"
WSB_REQUEST_DELAY_SECONDS = 1.0
_REQUEST_TIMEOUT = 10
_MAX_EXPORT_REQUESTS = 7
_EXPORT_LINK = re.compile(r"/settings_export\.html\?stack=[0-9a-fA-F,]+")
_CONTROLLER_FIRMWARE = re.compile(r"\bWWP-CPU COM V(\d+\.\d+) Rev\.\s*(\d+)\b")
_FLOW_VALUE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m\s*[3³]\s*/\s*h")
_TEMPERATURE_VALUE = re.compile(r"([+−-]?\d+(?:[.,]\d+)?)\s*°\s*C")
_FREQUENCY_VALUE = re.compile(r"(\d+(?:[.,]\d+)?)\s*Hz")
_UNSIGNED_VALUE = re.compile(r"[0-9]{1,12}")
_REPORT_FAILURES = (OSError, ValueError, WeishauptWebifError)


@dataclass(frozen=True)
class WsbField:
    """One named WEM value and its entity identity."""

    report: str
    key: str
    label: str
    unit: str | None = None
    choices: tuple[tuple[str, str], ...] = ()
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT
    aliases: tuple[str, ...] = ()
    text_pattern: str | None = None


def _temperature(report: str, key: str, label: str, *aliases: str) -> WsbField:
    return WsbField(
        report,
        key,
        label,
        UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        aliases=aliases,
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
    WsbField(
        "heat_pump",
        "diverter_valve_position",
        "Stellung Umschaltventil",
        choices=(("Heizkreis", "heating_circuit"), ("Warmwasser", "hot_water")),
    ),
    WsbField(
        "heat_pump",
        "compressor_frequency_target",
        "Soll Frequenz Verdichter",
        UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
    ),
    WsbField(
        "heat_pump",
        "compressor_frequency_actual",
        "Ist Frequenz Verdichter",
        UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
    ),
    WsbField(
        "heat_pump",
        "compressor_operating_hours",
        "Betriebsstd. Verdichter",
        UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    _temperature("heat_pump", "reported_target_temperature", "Solltemperatur"),
    _temperature(
        "heat_pump",
        "outdoor_heat_exchanger_inlet_temperature",
        "Wärmetauscher AG Eintritt",
        "Wärmetauscher AG Eintrit",
    ),
    _temperature(
        "heat_pump", "outdoor_heat_exchanger_middle_temperature", "Wärmetauscher AG Mitte"
    ),
    _temperature(
        "heat_pump", "compressor_discharge_temperature", "Druckgas", "Druckgastemperatur"
    ),
    _temperature("heat_pump", "indoor_heat_exchanger_temperature", "Wärmetauscher Innen"),
    _temperature("heat_pump", "indoor_refrigerant_temperature", "Kältemittel Innen"),
    WsbField(
        "heat_pump",
        "system_unit_version",
        "Version WWP-SG",
        state_class=None,
        text_pattern=r"V\d+(?:\.\d+)+(?:\s+(?:R|Rev\.\s*)\d+)?",
    ),
    WsbField(
        "heat_pump",
        "control_board_version",
        "Version WWP-CPU",
        state_class=None,
        text_pattern=r"V\d+(?:\.\d+)+(?:\s+(?:R|Rev\.\s*)\d+)?",
    ),
    WsbField(
        "heat_pump",
        "outdoor_unit_variant",
        "Außengerät Variante",
        state_class=None,
        aliases=("Aussengerät Variante",),
        text_pattern=r"WSB \d{1,2}(?:[- ][A-Za-z0-9]+)*",
    ),
)
_HEATING_CIRCUIT_INFO = (
    _temperature("heating_circuit", "outdoor_temperature_average", "AT Mittelwert"),
    _temperature("heating_circuit", "outdoor_temperature_long_term", "AT Langzeitwert"),
    _temperature("heating_circuit", "reported_supply_temperature", "Vorlauftemperatur"),
)
WSB_FIELDS = _PUMP_SETTINGS + _HEATER_COUNTERS + _HEAT_PUMP_INFO + _HEATING_CIRCUIT_INFO


def _menu(page: BeautifulSoup, label: str, *, parent: str | None = None) -> str:
    links = [
        str(link["href"])
        for link in page.find_all("a", href=_EXPORT_LINK)
        if _EXPORT_LINK.fullmatch(str(link["href"]))
        and " ".join(link.stripped_strings) == label
        and (parent is None or str(link["href"]).startswith(parent + ","))
    ]
    if not links:
        raise ValueError(f"Missing WEM menu {label}")
    # The navigation repeats top-level labels on child pages. Scope a child
    # lookup to its own stack before preferring the last matching link.
    return links[-1]


def _pump_values(page: BeautifulSoup) -> dict[str, int | str]:
    labels = [" ".join(anchor.stripped_strings) for anchor in page.find_all("a")]
    values: dict[str, int | str] = {}
    for field in _PUMP_SETTINGS:
        choices = dict(field.choices)
        prefix = field.label + " "
        matches = [
            label.removeprefix(prefix) for label in labels if label.startswith(prefix)
        ]
        if len(matches) == 1 and matches[0] in choices:
            values[field.key] = choices[matches[0]]
    return values


def _info_values(
    page: BeautifulSoup, fields: tuple[WsbField, ...]
) -> dict[str, int | float | str]:
    rows: dict[str, list[str]] = {}
    for row in page.select("div.nav-link.browseobj"):
        if heading := row.find("h5"):
            label = heading.get_text(" ", strip=True)
            raw = row.get_text(" ", strip=True).removeprefix(label).strip()
            rows.setdefault(label, []).append(raw)

    values: dict[str, int | float | str] = {}
    for field in fields:
        matches = [
            value
            for label in (field.label, *field.aliases)
            for value in rows.get(label, ())
        ]
        if len(matches) == 1 and (value := _parse_info_value(field, matches[0])) is not None:
            values[field.key] = value
    return values


def _parse_info_value(field: WsbField, raw: str) -> int | float | str | None:
    if field.choices:
        return dict(field.choices).get(raw)
    if field.text_pattern is not None:
        return raw if re.fullmatch(field.text_pattern, raw) else None
    if field.unit == PERCENTAGE:
        if (match := re.fullmatch(r"(\d{1,3})\s*%", raw)) and int(match[1]) <= 100:
            return int(match[1])
        return None
    if field.unit == UnitOfTime.HOURS:
        match = re.fullmatch(r"([0-9]{1,12})\s*h", raw)
        return int(match[1]) if match else None
    if field.unit is None:
        return int(raw) if _UNSIGNED_VALUE.fullmatch(raw) else None

    if field.unit == UnitOfFrequency.HERTZ and raw == "Aus":
        return 0
    pattern = {
        UnitOfTemperature.CELSIUS: _TEMPERATURE_VALUE,
        UnitOfFrequency.HERTZ: _FREQUENCY_VALUE,
        UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR: _FLOW_VALUE,
    }[field.unit]
    if match := pattern.fullmatch(raw):
        value = float(match[1].replace(",", ".").replace("−", "-"))
        if math.isfinite(value) and (
            field.unit != UnitOfTemperature.CELSIUS or -50 <= value <= 200
        ):
            return value
    return None


def _hour_values(page: BeautifulSoup) -> dict[str, int | float | str]:
    return _info_values(page, _HEATER_COUNTERS)


def _heat_pump_values(page: BeautifulSoup) -> dict[str, int | float | str]:
    return _info_values(page, _HEAT_PUMP_INFO)


def _heating_circuit_values(page: BeautifulSoup) -> dict[str, int | float | str]:
    return _info_values(page, _HEATING_CIRCUIT_INFO)


_REPORT_PAGES = (
    ("Wärmepumpe", (("Pumpe", _pump_values),)),
    (
        "Info",
        (
            ("2. WEZ", _hour_values),
            ("Wärmepumpe", _heat_pump_values),
            ("Heizkreis", _heating_circuit_values),
        ),
    ),
)
_REPORT_KEYS = frozenset(
    (parent, child) for parent, pages in _REPORT_PAGES for child, _ in pages
)


class _ExportBudgetExhausted(ValueError):
    """No export requests remain in this poll."""


class _WemAuthenticationRequired(WeishauptWebifError):
    """The export endpoint did not return an authenticated page."""


@dataclass(frozen=True)
class _ReportRoute:
    """A leaf URL that has returned recognized data on this controller."""

    path: str
    fields: frozenset[str]


class WsbWebifCoordinator(DataUpdateCoordinator[dict[str, int | float | str]]):
    """Keep WEM availability independent of the Modbus coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: WebifConnection | None,
        mcu_lock: asyncio.Lock,
        *,
        config_entry: MyConfigEntry | None = None,
    ) -> None:
        """Set up WEM polling."""
        super().__init__(
            hass,
            _LOGGER,
            name="Weishaupt WSB WEM",
            config_entry=config_entry,
            update_interval=timedelta(minutes=5),
        )
        self.api = api
        self.mcu_lock = mcu_lock
        self.controller_firmware: str | None = None
        self._routes: dict[tuple[str, str], _ReportRoute] = {}
        self._last_partial_poll: str | None = None

    def _update_controller_firmware(self, firmware: str) -> None:
        if firmware == self.controller_firmware:
            return
        if self.controller_firmware is not None:
            self._routes.clear()
        self.controller_firmware = firmware
        if self.config_entry is not None:
            registry = dr.async_get(self.hass)
            for device in dr.async_entries_for_config_entry(
                registry, self.config_entry.entry_id
            ):
                registry.async_update_device(device.id, sw_version=firmware)

    async def async_close(self) -> None:
        """Stop WEM requests before closing the shared HTTP session."""
        api, self.api = self.api, None
        await self.async_shutdown()
        if api is not None:
            async with self.mcu_lock:
                await api.close()

    async def _get_page(self, path: str) -> BeautifulSoup:
        # The pinned client handles pacing and reauthentication. The profile
        # follows only same-device export menu links and never submits settings.
        async with self.mcu_lock, asyncio.timeout(_REQUEST_TIMEOUT):
            if self.api is None:
                raise ValueError("WEM is disabled")
            response = await self.api._request("GET", path)  # noqa: SLF001
        if response.status_code in (401, 403):
            raise _WemAuthenticationRequired("WEM authentication required")
        if response.status_code in (429, 503):
            raise McuResourceError("WEM export endpoint is busy")
        if response.status_code != 200:
            raise ValueError("WEM export unavailable")
        page = BeautifulSoup(response.text, "html.parser")
        if page.select_one('input[name="pass"]') or (
            page.title and page.title.get_text(strip=True).casefold() == "login"
        ):
            raise _WemAuthenticationRequired("WEM export returned a login page")
        return page

    async def _async_update_data(self) -> dict[str, int | float | str]:
        if self.api is None:
            return {}

        requests = 0
        firmware: str | None = None
        expected = {key: route.fields for key, route in self._routes.items()}

        async def get_page(path: str) -> BeautifulSoup:
            nonlocal requests, firmware
            if requests >= _MAX_EXPORT_REQUESTS:
                raise _ExportBudgetExhausted
            requests += 1
            page = await self._get_page(path)
            if match := _CONTROLLER_FIRMWARE.search(page.get_text(" ", strip=True)):
                firmware = f"WWP-CPU COM V{match[1]} R{match[2]}"
                if path == _ROOT:
                    self._update_controller_firmware(firmware)
            return page

        values: dict[str, int | float | str] = {}
        interruption = None
        try:
            # A warm poll can spend all spare calls on incomplete leaves.
            # Verified exports also carry the firmware header.
            root = (
                None if self._routes.keys() >= _REPORT_KEYS else await get_page(_ROOT)
            )
            await self._read_reports(get_page, root, values)
        except _REPORT_FAILURES as err:
            # Further requests after a transport failure can wait through the
            # client's cooldown while holding the controller lock.
            if isinstance(err, (_WemAuthenticationRequired, SessionExpiredError)):
                self._routes.clear()
            interruption = type(err).__name__
            if not values:
                raise UpdateFailed(f"WEM export unavailable ({interruption})") from None

        if firmware is not None:
            self._update_controller_firmware(firmware)
        if not values:
            raise UpdateFailed("No supported WSB fields in WEM export")

        incomplete = [
            f"{parent}/{child} ({len(fields - values.keys())} missing)"
            for (parent, child), fields in sorted(expected.items())
            if fields - values.keys()
        ]
        if interruption:
            incomplete.append(interruption)
        detail = ", ".join(incomplete) or None
        if detail is not None and detail != self._last_partial_poll:
            _LOGGER.warning(
                "Incomplete WSB WEM poll after %d export calls. %s", requests, detail
            )
        self._last_partial_poll = detail
        return values

    async def _read_reports(
        self,
        get_page: Callable[[str], Awaitable[BeautifulSoup]],
        root: BeautifulSoup | None,
        values: dict[str, int | float | str],
    ) -> None:
        """Read known pages and retry incomplete reports within the poll budget."""
        retry: list[
            tuple[
                tuple[str, str],
                _ReportRoute,
                Callable[[BeautifulSoup], dict[str, int | float | str]],
            ]
        ] = []
        for first, pages in _REPORT_PAGES:
            section = None
            if root is not None and any(
                (first, second) not in self._routes for second, _ in pages
            ):
                try:
                    parent = _menu(root, first)
                    section = await get_page(parent), parent
                except ValueError as err:
                    _LOGGER.debug(
                        "WSB WEM menu %s unavailable (%s)", first, type(err).__name__
                    )
            for second, parse in pages:
                key = (first, second)
                cached = self._routes.get(key)
                try:
                    if cached is not None:
                        path = cached.path
                    elif section is not None:
                        page, parent = section
                        path = _menu(page, second, parent=parent)
                    else:
                        continue
                    current = parse(await get_page(path))
                except ValueError as err:
                    self._routes.pop(key, None)
                    _LOGGER.debug(
                        "WSB WEM menu %s/%s unavailable (%s)",
                        first,
                        second,
                        type(err).__name__,
                    )
                    continue
                if current:
                    fields = frozenset(current)
                    self._routes[key] = _ReportRoute(
                        path, cached.fields | fields if cached else fields
                    )
                    values.update(current)
                if cached is not None and not cached.fields <= current.keys():
                    _LOGGER.debug(
                        "WSB WEM page %s/%s omitted %d previously reported fields",
                        first,
                        second,
                        len(cached.fields - current.keys()),
                    )
                    if not current:
                        self._routes.pop(key, None)
                    retry.append((key, cached, parse))

        # Discovery takes priority. Retry each incomplete cached report once.
        for key, cached, parse in retry:
            try:
                current = parse(await get_page(cached.path))
            except _ExportBudgetExhausted:
                break
            except ValueError as err:
                _LOGGER.debug(
                    "WSB WEM page retry %s/%s failed (%s)", *key, type(err).__name__
                )
                continue
            if current:
                values.update(current)
                self._routes[key] = _ReportRoute(
                    cached.path, cached.fields | frozenset(current)
                )


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
        if field.text_pattern is not None:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
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
        return (self.coordinator.data or {}).get(self.field.key) if self.available else None
