"""Entity classes used in this integration."""

from typing import TYPE_CHECKING, override

from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..configentry import MyConfigEntry, entity_identity
from ..const import CONF, CONST
from .coordinator import WeishauptCoordinator
from .descriptions.description import (
    EntityDescription,
    NumberDescription,
    SelectDescription,
    SensorDescription,
)

if TYPE_CHECKING:
    import logging

    _LOGGER: logging.Logger = __import__("logging").getLogger(__name__)


class WeishauptEntity(CoordinatorEntity[WeishauptCoordinator]):
    """Base entity for Weishaupt devices."""

    description: EntityDescription

    def __init__(
        self,
        coordinator: WeishauptCoordinator,
        description: EntityDescription,
        config_entry: MyConfigEntry,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)

        self.description = description
        self._mac = entity_identity(config_entry)
        self._weishaupt_config_entry = config_entry

        self._attr_has_entity_name = True
        self._attr_unique_id = (
            f"{self._mac}_{description.report_name}_{description.key}"
        )
        self._attr_translation_key = f"{description.report_name}_{description.key}"

    @property
    @override
    def available(self) -> bool:
        """Return whether the entity is available."""
        return (
            super().available
            and (
                getattr(self.description, "update_report", None)
                or self.description.report_name
            ) in self.coordinator.data.updated
        )

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        report_name = self.description.report_name
        entry = self._weishaupt_config_entry
        webif = (
            entry.runtime_data.wsb_coordinator
            if entry.data.get(CONF.WSB_READ_ONLY)
            else None
        )

        info = DeviceInfo(
            identifiers={
                (CONST.DOMAIN, f"{self._mac}_{report_name}"),
            },
            translation_key=f"dev_{report_name}",
            manufacturer="Weishaupt",
        )
        if webif is not None and webif.controller_firmware is not None:
            info["sw_version"] = webif.controller_firmware
        return info


class WeishauptSensor(WeishauptEntity, SensorEntity):
    """Representation of a Weishaupt sensor."""

    description: SensorDescription

    def __init__(
        self,
        coordinator: WeishauptCoordinator,
        description: SensorDescription,
        config_entry: MyConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description, config_entry)

        if description.translation_key is not None:
            self._attr_translation_key = description.translation_key
        self._attr_device_class = description.params.device_class
        if description.params.options:
            self._attr_options = list(description.params.options)
        self._attr_state_class = description.params.state_class
        self._attr_native_unit_of_measurement = (
            description.params.native_unit_of_measurement
        )
        self._attr_suggested_display_precision = (
            description.params.suggested_display_precision
        )

    @property
    @override
    def native_value(self) -> float | str | None:
        """Return the sensor value."""
        value: float | str | None

        if self.description.calculated_value_fn is not None:
            value = self.description.calculated_value_fn(
                self.coordinator,
                self._weishaupt_config_entry.runtime_data.powermap,
            )
        else:
            value = self.description.value_fn(self.coordinator.device)

        if value is None:
            return None

        if self.description.params.is_enum:
            return f"{self.description.report_name}_{self.description.key}_{value}"

        return value


class WeishauptNumber(WeishauptEntity, NumberEntity):
    """Representation of a Weishaupt number."""

    description: NumberDescription

    def __init__(
        self,
        coordinator: WeishauptCoordinator,
        description: NumberDescription,
        config_entry: MyConfigEntry,
    ) -> None:
        """Initialize the number."""
        super().__init__(coordinator, description, config_entry)

        self._attr_device_class = description.params.device_class
        self._attr_native_unit_of_measurement = (
            description.params.native_unit_of_measurement
        )
        self._attr_native_min_value = description.params.native_min_value
        self._attr_native_max_value = description.params.native_max_value
        self._attr_native_step = description.params.native_step
        self._attr_mode = description.params.mode

    @property
    @override
    def native_value(self) -> float | None:
        """Return the current value."""
        return self.description.value_fn(self.coordinator.device)

    @property
    @override
    def available(self) -> bool:
        """Require a valid readback for settings in the WSB profile."""
        return super().available and (
            self.description.update_report is None or self.native_value is not None
        )

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Set the value."""
        await self.coordinator.async_write_setting(self.description.set_value_fn, value)


class WeishauptSelect(WeishauptEntity, SelectEntity):
    """Representation of a Weishaupt select."""

    description: SelectDescription

    def __init__(
        self,
        coordinator: WeishauptCoordinator,
        description: SelectDescription,
        config_entry: MyConfigEntry,
    ) -> None:
        """Initialize the select."""
        super().__init__(coordinator, description, config_entry)

        self._enum = description.enum

        self._attr_options = [member.name for member in self._enum]

    @property
    @override
    def current_option(self) -> str | None:
        """Return the current option."""
        value = self.description.value_fn(self.coordinator.device)

        if value is None:
            return None

        try:
            return self._enum(value).name
        except ValueError:
            # A running hot-water push can report a minute that is not a preset.
            return None

    @override
    async def async_select_option(self, option: str) -> None:
        """Set the selected option."""
        value = self._enum[option].value

        await self.coordinator.async_write_setting(self.description.set_value_fn, value)
