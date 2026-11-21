"""Expose verified WSB configuration registers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from functools import partial
from operator import attrgetter
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
from .params import EMPTY, POWER, SENSOR_PERCENTAGE, TEMPERATURE

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
_PUMP_SETPOINTS = (
    "heating_pump_power_setpoint",
    "hot_water_pump_power_setpoint",
    "cooling_pump_power_setpoint",
)
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

# Only the writes listed in Weishaupt's WWP Modbus specification for the WSB.
_WRITABLE = {
    "system": {"operating_mode", "pv_setpoint"},
    "heating_circuit": {
        "operation_mode",
        "comfort_room_target_temperature",
        "normal_room_target_temperature",
        "lowering_room_target_temperature",
        *_LEGACY_CONSTANTS,
    },
    "domestic_hot_water": {"push", "normal_temperature", "lowering_temperature"},
}

# The user-facing WSB ranges differ from the generic number descriptions.
# In particular, the factory Absenk hot-water target is 35 °C.
_NUMBER_LIMITS = {
    "normal_room_target_temperature": (16, 28),
    "lowering_room_target_temperature": (16, 28),
    "constant_heating_temperature": (7, 65),
    "constant_heating_lowering_temperature": (7, 65),
    "constant_cooling_temperature": (7, 30),
    "normal_temperature": (20, 60),
    "lowering_temperature": (10, 60),
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


_RELATED_LIMITS = {
    "comfort_room_target_temperature": (("normal_room_target_temperature",), ()),
    "normal_room_target_temperature": (
        ("lowering_room_target_temperature",),
        ("comfort_room_target_temperature",),
    ),
    "lowering_room_target_temperature": ((), ("normal_room_target_temperature",)),
    "normal_temperature": (("lowering_temperature",), ()),
    "lowering_temperature": ((), ("normal_temperature",)),
}


async def _set_wsb_number(
    item: NumberDescription, device: Weishaupt, value: float
) -> None:
    """Check the WSB bounds and any dependent targets before a write."""
    params = item.params
    minimum, maximum = params.native_min_value, params.native_max_value
    if item.key in _LEGACY_CONSTANTS and _temperature(item.value_fn, device) is None:
        raise ServiceValidationError("The current fixed temperature is unavailable.")
    if item.key in _RELATED_LIMITS:
        component = (
            device.heating_circuit_config
            if item.report_name == "heating_circuit"
            else device.domestic_hot_water_config
        )
        lower_fields, upper_fields = _RELATED_LIMITS[item.key]
        lower = [getattr(component, field) for field in lower_fields]
        upper = [getattr(component, field) for field in upper_fields]
        if any(bound is None or not 5 <= bound <= 80 for bound in lower + upper):
            raise ServiceValidationError(
                "The related temperature targets must be readable."
            )
        minimum = max([minimum, *lower])
        maximum = min([maximum, *upper])
    step = params.native_step
    if not minimum <= value <= maximum or (value - params.native_min_value) % step:
        raise ServiceValidationError(
            f"Choose a value from {minimum:g} to {maximum:g} in steps of {step:g}."
        )
    await item.set_value_fn(device, value)


def _number_control(
    item: NumberDescription,
    report: str,
    reader: Callable[[Weishaupt], float | None],
) -> NumberDescription:
    params = item.params
    if item.key in _NUMBER_LIMITS:
        minimum, maximum = _NUMBER_LIMITS[item.key]
        params = replace(params, native_min_value=minimum, native_max_value=maximum)
    elif item.key == "pv_setpoint":
        params = replace(params, native_step=1)
    configured = replace(item, params=params, update_report=report, value_fn=reader)
    return replace(configured, set_value_fn=partial(_set_wsb_number, configured))


def wsb_settings(
    readings: tuple[EntityDescription, ...],
) -> tuple[EntityDescription, ...]:
    """Keep the setting sensors and expose documented WSB controls."""
    settings: list[EntityDescription] = []
    for item in readings:
        report = _REPORTS.get(item.report_name)
        if report is None:
            continue
        writable = item.key in _WRITABLE[item.report_name]
        if isinstance(item, SelectDescription) and writable:
            settings.append(replace(item, update_report=report))
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
            elif item.report_name == "heating_circuit" and item.key == "heating_curve":
                params = replace(EMPTY, suggested_display_precision=2)
                reader = _heating_curve
            elif item.report_name == "system" and item.key == "pv_setpoint":
                params = replace(POWER, state_class=None)
                reader = item.value_fn
            else:
                continue
            if writable:
                settings.append(_number_control(item, report, reader))
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
    settings.extend(
        SensorDescription(
            key=key,
            report_name="heat_pump",
            update_report="wsb_pump_settings",
            params=SENSOR_PERCENTAGE,
            value_fn=attrgetter(f"wsb_pump_settings.{key}"),
        )
        for key in _PUMP_SETPOINTS
    )
    return tuple(settings)
