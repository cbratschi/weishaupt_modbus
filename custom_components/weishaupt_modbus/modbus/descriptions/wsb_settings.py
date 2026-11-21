"""Expose verified WSB configuration registers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfTime
from homeassistant.exceptions import ServiceValidationError

from .description import (
    EntityDescription,
    NumberDescription,
    SelectDescription,
    SensorDescription,
)
from .params import EMPTY, POWER, ROOM_TEMP_COMFORT, TEMPERATURE

if TYPE_CHECKING:
    from custom_components.weishaupt_modbus.modbus.weishaupt_modbus_client.model.device import (
        Weishaupt,
    )


_REPORTS = {
    "system": "wsb_system_settings",
    "heating_circuit": "wsb_heating_circuit_settings",
    "domestic_hot_water": "wsb_hot_water_settings",
}
_MODES = {"system": "operating_mode", "heating_circuit": "operation_mode"}
_TEMPERATURES = {
    "heating_circuit": {
        "comfort_room_target_temperature",
        "normal_room_target_temperature",
        "lowering_room_target_temperature",
        "summer_winter_switch_temperature",
        "constant_heating_temperature",
        "constant_heating_lowering_temperature",
        "constant_cooling_temperature",
    },
    "domestic_hot_water": {"normal_temperature", "lowering_temperature"},
}
_LEGACY_CONSTANTS = {
    "constant_heating_temperature",
    "constant_heating_lowering_temperature",
    "constant_cooling_temperature",
}


def _temperature(
    value_fn: Callable[[Weishaupt], float | None], device: Weishaupt
) -> float | None:
    """Ignore non-request markers and values outside FormatSollwert."""
    value = value_fn(device)
    return value if value is not None and 5 <= value <= 500 else None


def _mode(
    value_fn: Callable[[Weishaupt], int | None],
    modes: dict[int, str],
    device: Weishaupt,
) -> str | None:
    value = value_fn(device)
    return modes.get(value) if value is not None else None


def _push_minutes(device: Weishaupt) -> int | None:
    value = device.domestic_hot_water_config.push
    return value if value is not None and (value == 0 or 5 <= value <= 240) else None


def _heating_curve(device: Weishaupt) -> float | None:
    value = device.heating_circuit_config.heating_curve
    return (
        value if value is not None and (value == 0 or 0.05 <= value <= 1.50) else None
    )


async def _set_comfort_temperature(device: Weishaupt, value: float) -> None:
    """Validate the WSB heating circuit's allowed Comfort setting."""
    circuit = device.heating_circuit_config
    normal = circuit.normal_room_target_temperature
    maximum = ROOM_TEMP_COMFORT.native_max_value
    if normal is None or not 16 <= normal <= maximum:
        raise ServiceValidationError("The Normal room target must be readable.")
    minimum = max(ROOM_TEMP_COMFORT.native_min_value, normal)
    if not minimum <= value <= maximum or value % ROOM_TEMP_COMFORT.native_step:
        raise ServiceValidationError(
            f"Choose a Comfort room target from {minimum:g} to {maximum:g} °C "
            "in steps of 0.5 °C."
        )
    await circuit.write("comfort_room_target_temperature", value)


def wsb_settings(
    readings: tuple[EntityDescription, ...],
) -> tuple[SensorDescription | NumberDescription, ...]:
    """Keep the setting sensors and expose the verified Comfort control."""
    settings: list[SensorDescription | NumberDescription] = []
    for item in readings:
        report = _REPORTS.get(item.report_name)
        if report is None:
            continue
        if (
            isinstance(item, SelectDescription)
            and _MODES.get(item.report_name) == item.key
        ):
            modes = {member.value: member.name for member in item.enum}
            params = replace(
                EMPTY,
                device_class=SensorDeviceClass.ENUM,
                options=tuple(modes.values()),
            )
            reader = partial(_mode, item.value_fn, modes)
        elif isinstance(item, NumberDescription):
            if item.key in _TEMPERATURES.get(item.report_name, ()):
                params = TEMPERATURE
                if item.key not in _LEGACY_CONSTANTS:
                    params = replace(params, state_class=None)
                reader = partial(_temperature, item.value_fn)
                if (
                    item.report_name == "heating_circuit"
                    and item.key == "comfort_room_target_temperature"
                ):
                    settings.append(
                        replace(
                            item,
                            update_report=report,
                            value_fn=reader,
                            set_value_fn=_set_comfort_temperature,
                        )
                    )
            elif item.report_name == "heating_circuit" and item.key == "heating_curve":
                params = replace(EMPTY, suggested_display_precision=2)
                reader = _heating_curve
            elif item.report_name == "system" and item.key == "pv_setpoint":
                params = replace(POWER, state_class=None)
                reader = item.value_fn
            else:
                continue
        else:
            continue
        settings.append(
            SensorDescription(
                key=item.key,
                report_name=item.report_name,
                update_report=report,
                params=params,
                value_fn=reader,
            )
        )
    settings.append(
        SensorDescription(
            key="push_minutes",
            report_name="domestic_hot_water",
            update_report=_REPORTS["domestic_hot_water"],
            params=replace(
                EMPTY,
                device_class=SensorDeviceClass.DURATION,
                native_unit_of_measurement=UnitOfTime.MINUTES,
            ),
            value_fn=_push_minutes,
        )
    )
    return tuple(settings)
