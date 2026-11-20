"""All entities from the different modules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorStateClass

from ...configentry import MyConfigEntry
from ...const import CONF
from .calculated import CALCULATERD_ENTITIES
from .description import SensorDescription
from .domestic_hot_water import DOMESTIC_HOT_WATER_ENTITIES
from .heat_pump import HEAT_PUMP_ENTITIES
from .heating_circuit import (
    HEATING_CIRCUIT_ENTITIES,
    HEATING_CIRCUIT_ENTITIES2,
    HEATING_CIRCUIT_ENTITIES3,
    HEATING_CIRCUIT_ENTITIES4,
    HEATING_CIRCUIT_ENTITIES5,
)
from .io import IO_ENTITIES
from .second_heat_source import SECOND_HEAT_SOURCE_ENTITIES
from .stats import STATISTICS_ENTITIES
from .system import SYSTEM_ENTITIES

if TYPE_CHECKING:
    from ..weishaupt_modbus_client.model.device import Weishaupt

WSB_UNVERIFIED_MODBUS_KEYS = {
    "electric_heater_1_operating_hours",
    "electric_heater_2_operating_hours",
    "electric_heater_2_switch_cycles",
    "sensor_output_h1_4",
    "sensor_output_h1_5",
}

# The stable integration recorded these unitless numeric values with an empty
# unit string. Recorder treats an omitted unit as a different series contract.
WSB_LEGACY_UNITLESS_STATISTICS = {
    "adr31106",
    "adr36801",
    "electric_heater_1_switch_cycles",
    "daily_cop",
    "yesterday_cop",
    "monthly_cop",
    "yearly_cop",
}

WSB_BINARY_IO_KEYS = {
    "sensor_sg_ready_1",
    "sensor_sg_ready_2",
    "sensor_input_output_h1_2",
    "sensor_output_h1_3",
}

WSB_LEGACY_HEATER_STATUS = {
    "electric_heater_1_status",
    "electric_heater_2_status",
}


def _heater_status(
    value_fn: Callable[[Weishaupt], float | str | None], device: Weishaupt
) -> str | None:
    return {0: "w2_status_aus", 1: "w2_status_ein"}.get(value_fn(device))


def _binary_input(
    value_fn: Callable[[Weishaupt], float | str | None], device: Weishaupt
) -> float | None:
    value = value_fn(device)
    return value if isinstance(value, (int, float)) and value in (0, 1) else None


def _wsb_sensor(description: SensorDescription) -> SensorDescription:
    """Keep the established WSB Recorder contract and heater state values."""
    if description.key in WSB_LEGACY_UNITLESS_STATISTICS | WSB_BINARY_IO_KEYS:
        return replace(
            description,
            params=replace(
                description.params,
                state_class=SensorStateClass.MEASUREMENT,
                native_unit_of_measurement="",
                is_enum=False,
            ),
            value_fn=(
                partial(_binary_input, description.value_fn)
                if description.key in WSB_BINARY_IO_KEYS
                else description.value_fn
            ),
        )
    if description.key in WSB_LEGACY_HEATER_STATUS:
        return replace(
            description,
            params=replace(description.params, is_enum=False),
            value_fn=partial(_heater_status, description.value_fn),
        )
    return description


def get_entities(entry: MyConfigEntry) -> tuple:
    """Get all entities for the given config entry."""

    readings = (
        SYSTEM_ENTITIES
        + HEAT_PUMP_ENTITIES
        + HEATING_CIRCUIT_ENTITIES
        + DOMESTIC_HOT_WATER_ENTITIES
        + SECOND_HEAT_SOURCE_ENTITIES
        + STATISTICS_ENTITIES
        + CALCULATERD_ENTITIES
        + IO_ENTITIES
    )
    if entry.data.get(CONF.HK2, False) is True:
        readings += HEATING_CIRCUIT_ENTITIES2
    if entry.data.get(CONF.HK3, False) is True:
        readings += HEATING_CIRCUIT_ENTITIES3
    if entry.data.get(CONF.HK4, False) is True:
        readings += HEATING_CIRCUIT_ENTITIES4
    if entry.data.get(CONF.HK5, False) is True:
        readings += HEATING_CIRCUIT_ENTITIES5
    if entry.data.get(CONF.WSB_READ_ONLY):
        return tuple(
            _wsb_sensor(description)
            for description in readings
            if isinstance(description, SensorDescription)
            and description.key not in WSB_UNVERIFIED_MODBUS_KEYS
        )
    return readings
