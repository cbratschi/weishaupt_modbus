"""Verify the WSB sensor profile against the supported register formats."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup
from modbus_connection.exceptions import ModbusExceptionError
import pytest

from custom_components.weishaupt_modbus.const import CONF
from custom_components.weishaupt_modbus.modbus.descriptions.all import get_entities
from custom_components.weishaupt_modbus.modbus.descriptions.description import (
    NumberDescription,
    SelectDescription,
    SensorDescription,
)
from custom_components.weishaupt_modbus.modbus.weishaupt_modbus_client.model.device import (
    Weishaupt,
)
from custom_components.weishaupt_modbus.webif.wsb import (
    WSB_FIELDS,
    _heat_pump_values,
    _hour_values,
    _pump_values,
)
from homeassistant.exceptions import ServiceValidationError


class ReadOnlyUnit:
    """Supply selected WSB words through the public polling interface."""

    def __init__(self) -> None:
        """Initialise the fixture with documented examples and sentinel values."""
        self.words = {
            31101: 1,
            31104: 350,
            32101: 520,
            32102: 487,
            34104: 1,
            34105: 0,
            35103: 0x800A,
            35104: 0x800B,
            36104: 40000,
            36304: 65535,
            40001: 1,
            40002: 40000,
            41103: 0,
            41105: 215,
            41106: 185,
            41107: 180,
            41108: 110,
            41109: 180,
            41110: 350,
            41111: 0x8000,
            41112: 200,
            42102: 0,
            42103: 520,
            42104: 350,
        }
        self.reject_hot_water_settings = False

    async def read_input_registers(self, address: int, count: int) -> list[int]:
        """Return a contiguous block of up to five input words."""
        assert 1 <= count <= 5
        return [self.words.get(index, 0) for index in range(address, address + count)]

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        """Allow holding reads and simulate a failure in the hot-water block."""
        if self.reject_hot_water_settings and address == 42101:
            raise ModbusExceptionError.from_code(2, "unsupported setting")
        return await self.read_input_registers(address, count)


def _profile():
    return get_entities(SimpleNamespace(data={CONF.WSB_READ_ONLY: True}))


def test_wsb_reads_verified_settings_and_keeps_measurements_independent() -> None:
    """A failed holding-register read must leave the hot water measurement live."""
    unit = ReadOnlyUnit()
    reports = (
        "system",
        "heating_circuit",
        "domestic_hot_water",
        "second_heat_source",
        "statistics",
        "io",
        "wsb_system_settings",
        "wsb_heating_circuit_settings",
        "wsb_hot_water_settings",
    )
    device = Weishaupt(unit, reports, wsb_read_only=True)
    sensors = {
        f"{item.report_name}_{item.key}": item
        for item in _profile()
        if isinstance(item, SensorDescription)
    }

    def read(key):
        return sensors[key].value_fn(device)

    async def exercise() -> None:
        first = await device.async_update()
        assert first.updated == set(reports)
        assert not first.failed
        assert read("system_pv_setpoint") == 40000
        assert read("system_operating_mode") == "system_operating_mode_heating"
        assert read("heating_circuit_room_target_temperature") is None
        assert read("heating_circuit_comfort_room_target_temperature") == 21.5
        assert read("heating_circuit_heating_curve") == 1.10
        assert read("heating_circuit_constant_heating_temperature") == 35.0
        assert read("heating_circuit_constant_heating_lowering_temperature") is None
        assert read("heating_circuit_constant_cooling_temperature") == 20.0
        assert read("domestic_hot_water_push_minutes") == 0
        assert read("domestic_hot_water_normal_temperature") == 52.0
        assert read("domestic_hot_water_lowering_temperature") == 35.0
        assert read("second_heat_source_electric_heater_1_status") == "w2_status_ein"
        assert read("second_heat_source_electric_heater_2_status") == "w2_status_aus"
        assert read("io_sensor_input_output_h1_2") == 0
        assert read("io_sensor_output_h1_3") == 1
        assert device.statistics_input.total_energy_year == 40000
        assert device.statistics_input.hot_water_energy_year == 65535

        unit.reject_hot_water_settings = True
        unit.words[32102] = 491
        unit.words[35103] = 0x8000
        unit.words[35104] = 0  # A numeric sensor code is not a digital state.
        second = await device.async_update()
        assert second.failed.keys() == {"wsb_hot_water_settings"}
        assert (
            sensors["domestic_hot_water_normal_temperature"].update_report
            not in second.updated
        )
        assert "domestic_hot_water" in second.updated
        assert read("domestic_hot_water_temperature") == 49.1
        assert read("io_sensor_input_output_h1_2") is None
        assert read("io_sensor_output_h1_3") is None

        unit.reject_hot_water_settings = False
        unit.words[42103] = 1
        assert "wsb_hot_water_settings" in (await device.async_update()).updated
        assert read("domestic_hot_water_normal_temperature") is None

    asyncio.run(exercise())


def test_wsb_comfort_control_writes_only_the_verified_register() -> None:
    """Write and restore the verified register without touching adjacent settings."""

    class WritableUnit(ReadOnlyUnit):
        def __init__(self) -> None:
            super().__init__()
            self.writes: list[tuple[int, int]] = []

        async def write_register(self, address: int, value: int) -> None:
            self.writes.append((address, value))
            self.words[address] = value

    profile = get_entities(
        SimpleNamespace(data={CONF.WSB_READ_ONLY: True, CONF.HK2: True})
    )
    (control,) = (
        item
        for item in profile
        if isinstance(item, NumberDescription)
        and item.report_name == "heating_circuit"
        and item.key == "comfort_room_target_temperature"
    )
    assert isinstance(control, NumberDescription)
    assert control.report_name == "heating_circuit"
    assert control.key == "comfort_room_target_temperature"
    assert control.update_report == "wsb_heating_circuit_settings"
    (sensor,) = (
        item
        for item in profile
        if isinstance(item, SensorDescription)
        and item.report_name == control.report_name
        and item.key == control.key
    )

    async def exercise() -> None:
        unit = WritableUnit()
        device = Weishaupt(unit, ("wsb_heating_circuit_settings",), wsb_read_only=True)
        before = dict(unit.words)
        await device.async_update()
        assert control.value_fn(device) == sensor.value_fn(device) == 21.5
        for target in (19.5, 21.25, 28.5, float("nan")):
            with pytest.raises(ServiceValidationError):
                await control.set_value_fn(device, target)
        unit.words[41106] = 215
        await device.async_update()
        with pytest.raises(ServiceValidationError):
            await control.set_value_fn(device, 21.0)
        unit.words[41106] = 1
        await device.async_update()
        with pytest.raises(ServiceValidationError):
            await control.set_value_fn(device, 22.0)
        assert not unit.writes

        unit.words[41106] = before[41106]
        await device.async_update()
        await control.set_value_fn(device, 21.0)
        await device.async_update()
        assert control.value_fn(device) == sensor.value_fn(device) == 21.0
        await control.set_value_fn(device, 21.5)
        await device.async_update()
        assert control.value_fn(device) == sensor.value_fn(device) == 21.5
        assert unit.words == before
        assert unit.writes == [(41105, 210), (41105, 215)]

    asyncio.run(exercise())


def test_wsb_exposes_and_writes_only_the_documented_controls() -> None:
    """Verify register, encoding and poll report for the complete WSB write set."""

    class WritableUnit(ReadOnlyUnit):
        def __init__(self) -> None:
            super().__init__()
            self.writes = []

        async def write_register(self, address: int, value: int) -> None:
            self.writes.append((address, value))
            self.words[address] = value

    expected = {
        ("system", "operating_mode"): (40001, 3, 3),
        ("system", "pv_setpoint"): (40002, 1234, 1234),
        ("heating_circuit", "operation_mode"): (41103, 2, 2),
        ("heating_circuit", "comfort_room_target_temperature"): (41105, 22.0, 220),
        ("heating_circuit", "normal_room_target_temperature"): (41106, 19.0, 190),
        ("heating_circuit", "lowering_room_target_temperature"): (41107, 17.5, 175),
        ("heating_circuit", "constant_heating_temperature"): (41110, 36.5, 365),
        ("heating_circuit", "constant_heating_lowering_temperature"): (
            41111,
            27.0,
            270,
        ),
        ("heating_circuit", "constant_cooling_temperature"): (41112, 21.0, 210),
        ("domestic_hot_water", "push"): (42102, 10, 10),
        ("domestic_hot_water", "normal_temperature"): (42103, 51.5, 515),
        ("domestic_hot_water", "lowering_temperature"): (42104, 35.0, 350),
    }
    reports = {
        "system": "wsb_system_settings",
        "heating_circuit": "wsb_heating_circuit_settings",
        "domestic_hot_water": "wsb_hot_water_settings",
    }
    profile = get_entities(
        SimpleNamespace(data={CONF.WSB_READ_ONLY: True, CONF.HK2: True})
    )
    controls = {
        (item.report_name, item.key): item
        for item in profile
        if isinstance(item, (NumberDescription, SelectDescription))
    }
    assert controls.keys() == expected.keys()
    assert all(
        item.update_report == reports[group] for (group, _), item in controls.items()
    )

    async def exercise() -> None:
        unit = WritableUnit()
        device = Weishaupt(unit, tuple(reports.values()), wsb_read_only=True)
        await device.async_update()
        fixed_lowering = controls[
            "heating_circuit", "constant_heating_lowering_temperature"
        ]
        assert fixed_lowering.value_fn(device) is None
        for key, invalid in (
            (("heating_circuit", "constant_heating_lowering_temperature"), 27.0),
            (("heating_circuit", "normal_room_target_temperature"), 22.0),
            (("heating_circuit", "lowering_room_target_temperature"), 19.0),
            (("domestic_hot_water", "lowering_temperature"), 55.0),
            (("system", "pv_setpoint"), 125.5),
        ):
            with pytest.raises(ServiceValidationError):
                await controls[key].set_value_fn(device, invalid)
        assert unit.writes == []
        unit.words[41111] = 250
        await device.async_update()
        for key, (address, value, word) in expected.items():
            await controls[key].set_value_fn(device, value)
            await device.async_update()
            assert controls[key].value_fn(device) == value
            assert unit.writes[-1] == (address, word)
        assert unit.writes == [
            (address, word) for address, _, word in expected.values()
        ]

    asyncio.run(exercise())


def test_wsb_wem_values_ignore_missing_ambiguous_and_invalid_rows() -> None:
    """Route only unambiguous settings and counters with the expected formats."""
    pump = BeautifulSoup(
        "<a>Regelungsart Heizen <small>Konstantbetrieb</small></a>"
        "<a>Regelungsart WW Volumenstrom</a>"
        "<a>Regelungsart Kühlen Konstantbetrieb</a><a>Regelungsart Kühlen Volumenstrom</a>"
        "<a>Leistung Heizen 55 %</a><a>Leistung Warmwasser 253 %</a>"
        "<a>Funktion HK-Pumpe</a><a>Freigabe bei EVU-Sperre Aus</a>",
        "html.parser",
    )
    assert _pump_values(pump) == {
        "heating_pump_power_setpoint": 55,
        "pump_regulation_heating": "constant_speed",
        "pump_regulation_hot_water": "flow_rate",
        "pump_function": "heating_circuit_pump",
        "pump_utility_lock_release": "off",
    }
    heater = BeautifulSoup(
        '<div class="nav-link browseobj"><h5>Betriebsstunden E1</h5>37 h</div>'
        '<div class="nav-link browseobj"><h5>Schaltspiele E1</h5>386</div>'
        '<div class="nav-link browseobj"><h5>Betriebsstunden E2</h5>unbekannt</div>',
        "html.parser",
    )
    assert _hour_values(heater) == {
        "electric_heater_1_operating_hours": 37,
        "electric_heater_1_switch_cycles": 386,
    }


def test_wsb_reads_actual_flow_pump_speed_and_cycle_counters() -> None:
    """Read the live info rows separately from configured pump setpoints."""
    page = BeautifulSoup(
        '<div class="nav-link browseobj"><h5>Volumenstrom</h5>1.2m3/h</div>'
        '<div class="nav-link browseobj"><h5>Drehzahl Pumpe M1</h5>57 %</div>'
        '<div class="nav-link browseobj"><h5>Schaltspiele Verdichter</h5>1234</div>'
        '<div class="nav-link browseobj"><h5>Schaltspiele Abtauen</h5>89</div>'
        '<div class="nav-link browseobj"><h5>Leistung Heizen</h5>70 %</div>',
        "html.parser",
    )
    assert _heat_pump_values(page) == {
        "actual_flow_rate": 1.2,
        "actual_pump_speed": 57,
        "compressor_starts": 1234,
        "defrost_cycles": 89,
    }


def test_wsb_heat_pump_info_keeps_zero_and_rejects_bad_rows() -> None:
    """Keep genuine zeroes and omit duplicate, malformed and unavailable values."""
    page = BeautifulSoup(
        '<div class="nav-link browseobj"><h5>Volumenstrom</h5>0,0 m³/h</div>'
        '<div class="nav-link browseobj"><h5>Drehzahl Pumpe M1</h5>0%</div>'
        '<div class="nav-link browseobj"><h5>Schaltspiele Verdichter</h5>0</div>'
        '<div class="nav-link browseobj"><h5>Schaltspiele Abtauen</h5>0</div>',
        "html.parser",
    )
    assert _heat_pump_values(page) == {
        "actual_flow_rate": 0.0,
        "actual_pump_speed": 0,
        "compressor_starts": 0,
        "defrost_cycles": 0,
    }

    page = BeautifulSoup(
        '<div class="nav-link browseobj"><h5>Volumenstrom</h5>-1.2 m3/h</div>'
        '<div class="nav-link browseobj"><h5>Drehzahl Pumpe M1</h5>253 %</div>'
        '<div class="nav-link browseobj"><h5>Schaltspiele Verdichter</h5>1234</div>'
        '<div class="nav-link browseobj"><h5>Schaltspiele Verdichter</h5>1235</div>'
        '<div class="nav-link browseobj"><h5>Schaltspiele Abtauen</h5>--</div>',
        "html.parser",
    )
    assert _heat_pump_values(page) == {}


def test_wsb_sensors_have_names_and_options_in_all_locales() -> None:
    """Verify that all exposed entities and new enum states have display strings."""
    directory = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "weishaupt_modbus"
        / "translations"
    )
    options = {
        f"{item.report_name}_{item.key}": item.params.options
        for item in _profile()
        if isinstance(item, SensorDescription)
    }
    options.update(
        {
            f"{field.report}_{field.key}": [key for _, key in field.choices]
            for field in WSB_FIELDS
        }
    )
    for locale in ("de", "en", "nl"):
        file = directory / f"{locale}.json"
        entities = json.loads(file.read_text(encoding="utf-8"))["entity"]
        for item in _profile():
            key = f"{item.report_name}_{item.key}"
            if isinstance(item, NumberDescription):
                assert entities["number"][key]["name"], (locale, key)
            if isinstance(item, SelectDescription):
                label = entities["select"][key]
                assert label["name"]
                assert {option.name for option in item.enum} <= label["state"].keys()
        labels = entities["sensor"]
        for key, keys in options.items():
            assert labels.get(key, {}).get("name"), (locale, key)
            assert set(keys) <= labels[key].get("state", {}).keys(), (locale, key)
