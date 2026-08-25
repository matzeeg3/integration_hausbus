"""Config flow for Haus-Bus integration."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from pyhausbus.BusDataMessage import BusDataMessage
from pyhausbus.BusHandler import BusHandler
from pyhausbus.de.hausbus.homeassistant.proxy.controller.data.ModuleId import ModuleId
from pyhausbus.HausBusUtils import HOMESERVER_DEVICE_ID
from pyhausbus.HomeServer import HomeServer
from pyhausbus.IBusDataListener import IBusDataListener
from pyhausbus.ObjectId import ObjectId

from .binary_sensor import HausbusBinarySensor
from .const import (
    CONF_CHANNEL_ID,
    CONF_CONNECTION_TYPE,
    CONF_DEVICE_ID,
    CONNECTION_TYPE_AUTO,
    CONNECTION_TYPE_FIXED_IP,
    DOMAIN,
)
from .cover import HausbusCover
from .entity import HausbusEntity
from .light import HausbusDimmerLight, HausbusLedLight, HausbusRGBDimmerLight
from .switch import HausbusSwitch

_LOGGER = logging.getLogger(__name__)

DEVICE_SEARCH_TIMEOUT = 5

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_CONNECTION_TYPE, default=CONNECTION_TYPE_AUTO
        ): SelectSelector(
            SelectSelectorConfig(
                options=[CONNECTION_TYPE_AUTO, CONNECTION_TYPE_FIXED_IP],
                mode=SelectSelectorMode.LIST,
                translation_key=CONF_CONNECTION_TYPE,
            )
        )
    }
)

STEP_FIXED_IP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
    }
)


class ConfigFlow(config_entries.ConfigFlow, IBusDataListener, domain=DOMAIN):  # type: ignore[misc]
    """Handle a config flow for hausbus."""

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._found_device = False
        self._search_task: asyncio.Task | None = None
        self._connection_data: dict[str, Any] = {}
        self.home_server = HomeServer()
        self.home_server.addBusEventListener(self)

    @staticmethod
    def async_get_options_flow(
        _config_entry: config_entries.ConfigEntry,
    ) -> HausbusOptionsFlowHandler:
        """Create the options flow used to configure Haus-Bus devices."""
        return HausbusOptionsFlowHandler()

    def remove_bus_event_listeners(self) -> None:
        """Cleanup after finishing the config flow."""
        self.home_server.removeBusEventListener(self)

    def async_remove(self) -> None:
        """Trigger cleanup of bus event listeners after config flow."""
        self.remove_bus_event_listeners()
        return super().async_remove()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step — choose connection type."""
        if user_input is not None:
            connection_type = user_input[CONF_CONNECTION_TYPE]
            self._connection_data[CONF_CONNECTION_TYPE] = connection_type
            if connection_type == CONNECTION_TYPE_FIXED_IP:
                return await self.async_step_fixed_ip()
            return await self.async_step_wait_for_device()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors={}
        )

    async def async_step_fixed_ip(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the fixed IP step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                ipaddress.ip_address(host)
            except ValueError:
                errors[CONF_HOST] = "invalid_host"
            else:
                self._connection_data[CONF_HOST] = host
                return await self.async_step_wait_for_device()

        return self.async_show_form(
            step_id="fixed_ip",
            data_schema=STEP_FIXED_IP_SCHEMA,
            errors=errors,
        )

    async def async_step_wait_for_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Wait for a hausbus device to be found."""
        if not self._search_task:
            self._search_task = self.hass.async_create_task(
                self._async_wait_for_device()
            )

        if not self._search_task.done():
            return self.async_show_progress(
                step_id="wait_for_device",
                progress_action="wait_for_device",
                progress_task=self._search_task,
            )

        try:
            await self._search_task
        except TimeoutError:
            return self.async_show_progress_done(next_step_id="search_timeout")
        finally:
            self._search_task = None

        return self.async_show_progress_done(next_step_id="search_complete")

    async def async_step_search_timeout(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Inform the user that no device has been found."""
        if user_input is not None:
            return await self.async_step_wait_for_device()

        return self.async_show_form(step_id="search_timeout")

    async def async_step_search_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Create a configuration entry for the hausbus devices."""
        return self.async_create_entry(title="Haus-Bus", data=self._connection_data)

    async def _async_wait_for_device(self) -> None:
        """Start searching for devices and wait until at least one device was found or timeout is reached."""
        if self._connection_data.get(CONF_CONNECTION_TYPE) == CONNECTION_TYPE_FIXED_IP:
            host = self._connection_data.get(CONF_HOST, "")
            if host:
                BusHandler.getInstance().broadcastIp = host
        self.hass.async_add_executor_job(self.home_server.searchDevices)
        await asyncio.wait_for(self._check_device_found(), DEVICE_SEARCH_TIMEOUT)

    async def _check_device_found(self) -> bool:
        """Check if a device was found periodically."""
        while not self._found_device:
            await asyncio.sleep(0.1)
        return True

    def busDataReceived(self, busDataMessage: BusDataMessage) -> None:
        """Handle Haus-Bus messages."""
        object_id = ObjectId(busDataMessage.getSenderObjectId())
        data = busDataMessage.getData()

        if object_id.getDeviceId() == HOMESERVER_DEVICE_ID:
            return

        if isinstance(data, ModuleId):
            self._found_device = True


class _ChannelTypeSpec:
    """Describes how to build/apply the options form for one Haus-Bus channel type."""

    def __init__(
        self,
        type_label: str,
        build_schema: Callable[[HausbusEntity], vol.Schema],
        apply: Callable[[HausbusEntity, dict[str, Any]], Any],
    ) -> None:
        """Set up the channel type spec."""
        self.type_label = type_label
        self.build_schema = build_schema
        self.apply = apply


def _attrs(entity: HausbusEntity) -> dict[str, Any]:
    return entity.extra_state_attributes or {}


def _channel_label(entity: HausbusEntity) -> str:
    return entity.name or entity._attr_name or entity.entity_id


def _percent(field: str, default: int) -> tuple[vol.Marker, NumberSelector]:
    """Number selector rendered as a 0-100% slider."""
    return (
        vol.Required(field, default=default),
        NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=100,
                step=1,
                mode=NumberSelectorMode.SLIDER,
                unit_of_measurement="%",
            )
        ),
    )


def _number(
    field: str, default: int, *, min_value: int = 0
) -> tuple[vol.Marker, NumberSelector]:
    """Number selector rendered as a bounded input box with steppers."""
    return (
        vol.Required(field, default=default),
        NumberSelector(
            NumberSelectorConfig(min=min_value, mode=NumberSelectorMode.BOX)
        ),
    )


def _cover_schema(entity: HausbusEntity) -> vol.Schema:
    attrs = _attrs(entity)
    return vol.Schema(
        dict(
            [
                _number("close_time", attrs.get("close_time", 0)),
                _number("open_time", attrs.get("open_time", 0)),
                (
                    vol.Required(
                        "invert_direction",
                        default=attrs.get("invert_direction", False),
                    ),
                    BooleanSelector(),
                ),
            ]
        )
    )


async def _cover_apply(entity: HausbusEntity, user_input: dict[str, Any]) -> None:
    await entity.async_cover_set_configuration(**user_input)


def _dimmer_schema(entity: HausbusEntity) -> vol.Schema:
    attrs = _attrs(entity)
    return vol.Schema(
        dict(
            [
                (
                    vol.Required("mode", default=attrs.get("mode", "switch_only")),
                    SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                "dim_trailing_edge",
                                "dim_leading_edge",
                                "switch_only",
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="dimmer_mode",
                        )
                    ),
                ),
                _number("dimming_time", attrs.get("dimming_time", 0)),
                _number("ramp_time", attrs.get("ramp_time", 0)),
                _percent(
                    "dimming_start_brightness",
                    attrs.get("dimming_start_brightness", 0),
                ),
                _percent(
                    "dimming_end_brightness",
                    attrs.get("dimming_end_brightness", 100),
                ),
            ]
        )
    )


async def _dimmer_apply(entity: HausbusEntity, user_input: dict[str, Any]) -> None:
    await entity.async_dimmer_set_configuration(**user_input)


def _rgb_dimmer_schema(entity: HausbusEntity) -> vol.Schema:
    attrs = _attrs(entity)
    return vol.Schema(dict([_number("dimming_time", attrs.get("dimming_time", 0))]))


async def _rgb_dimmer_apply(entity: HausbusEntity, user_input: dict[str, Any]) -> None:
    await entity.async_rgb_set_configuration(**user_input)


def _led_schema(entity: HausbusEntity) -> vol.Schema:
    attrs = _attrs(entity)
    min_brightness = attrs.get("min_brightness")
    if min_brightness is None and entity._configuration:
        min_brightness = entity._configuration.getMinBrightness()
    return vol.Schema(
        dict(
            [
                _number("time_base", attrs.get("time_base", 0)),
                _percent("min_brightness", min_brightness or 0),
            ]
        )
    )


async def _led_apply(entity: HausbusEntity, user_input: dict[str, Any]) -> None:
    await entity.async_led_set_min_brightness(user_input["min_brightness"])
    await entity.async_led_set_configuration(user_input["time_base"])


def _switch_schema(entity: HausbusEntity) -> vol.Schema:
    attrs = _attrs(entity)
    return vol.Schema(
        dict(
            [
                _number("max_on_time", attrs.get("max_on_time", 0)),
                _number("off_delay_time", attrs.get("off_delay_time", 0)),
                _number("time_base", attrs.get("time_base", 0)),
            ]
        )
    )


async def _switch_apply(entity: HausbusEntity, user_input: dict[str, Any]) -> None:
    await entity.async_switch_set_configuration(**user_input)


def _taster_schema(entity: HausbusEntity) -> vol.Schema:
    attrs = _attrs(entity)

    def _bool(field: str, default: bool) -> tuple[vol.Marker, BooleanSelector]:
        return (vol.Required(field, default=default), BooleanSelector())

    return vol.Schema(
        dict(
            [
                _number("hold_timeout", attrs.get("hold_timeout", 0)),
                _number("double_click_timeout", attrs.get("double_click_timeout", 0)),
                _bool(
                    "event_button_pressed_active",
                    attrs.get("event_button_pressed_active", True),
                ),
                _bool(
                    "event_button_released_active",
                    attrs.get("event_button_released_active", True),
                ),
                _bool(
                    "event_button_hold_start_active",
                    attrs.get("event_button_hold_start_active", False),
                ),
                _bool(
                    "event_button_hold_end_active",
                    attrs.get("event_button_hold_end_active", False),
                ),
                _bool(
                    "event_button_clicked_active",
                    attrs.get("event_button_clicked_active", True),
                ),
                _bool(
                    "event_button_double_clicked_active",
                    attrs.get("event_button_double_clicked_active", False),
                ),
                _bool(
                    "led_feedback_active",
                    attrs.get("led_feedback_active", False),
                ),
                _bool("inverted", attrs.get("inverted", False)),
                _number("debounce_time", attrs.get("debounce_time", 40)),
            ]
        )
    )


async def _taster_apply(entity: HausbusEntity, user_input: dict[str, Any]) -> None:
    await entity.async_push_button_set_configuration(**user_input)


_CHANNEL_TYPES: dict[type[HausbusEntity], _ChannelTypeSpec] = {
    HausbusCover: _ChannelTypeSpec("Rollladen", _cover_schema, _cover_apply),
    HausbusDimmerLight: _ChannelTypeSpec("Dimmer", _dimmer_schema, _dimmer_apply),
    HausbusRGBDimmerLight: _ChannelTypeSpec(
        "RGB Dimmer", _rgb_dimmer_schema, _rgb_dimmer_apply
    ),
    HausbusLedLight: _ChannelTypeSpec("LED", _led_schema, _led_apply),
    HausbusSwitch: _ChannelTypeSpec("Schalter", _switch_schema, _switch_apply),
    HausbusBinarySensor: _ChannelTypeSpec("Taster", _taster_schema, _taster_apply),
}


class HausbusOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle configuration of individual Haus-Bus device channels."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._device_id: str | None = None
        self._channel_map: dict[str, HausbusEntity] = {}
        self._entity: HausbusEntity | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user pick a Haus-Bus device to configure."""
        gateway = self.config_entry.runtime_data.gateway

        devices = {
            device_id: device.name
            for device_id, device in gateway.devices.items()
            if any(
                type(entity) in _CHANNEL_TYPES
                for entity in gateway.channels.get(device_id, {}).values()
            )
        }
        if not devices:
            return self.async_abort(reason="no_devices")

        if user_input is not None:
            self._device_id = user_input[CONF_DEVICE_ID]
            return await self.async_step_device()

        sorted_devices = sorted(devices.items(), key=lambda item: item[1].lower())

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=device_id, label=name)
                                for device_id, name in sorted_devices
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user pick a configurable channel of the selected device."""
        gateway = self.config_entry.runtime_data.gateway
        channels = gateway.channels.get(self._device_id, {})

        self._channel_map = {
            entity.unique_id: entity
            for entity in channels.values()
            if type(entity) in _CHANNEL_TYPES
        }
        if not self._channel_map:
            return self.async_abort(reason="no_configurable_channels")

        if user_input is not None:
            self._entity = self._channel_map[user_input[CONF_CHANNEL_ID]]
            return await self.async_step_channel()

        options = sorted(
            (
                SelectOptionDict(
                    value=unique_id,
                    label=f"{_channel_label(entity)} "
                    f"({_CHANNEL_TYPES[type(entity)].type_label})",
                )
                for unique_id, entity in self._channel_map.items()
            ),
            key=lambda option: option["label"].lower(),
        )

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CHANNEL_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_channel(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show and apply the configuration form for the selected channel."""
        entity = self._entity
        assert entity is not None
        spec = _CHANNEL_TYPES[type(entity)]
        errors: dict[str, str] = {}

        if user_input is not None:
            # NumberSelector always returns floats; the pyhausbus proxies
            # need plain ints to build the bus message.
            normalized_input = {
                key: int(value) if isinstance(value, float) else value
                for key, value in user_input.items()
            }
            try:
                await spec.apply(entity, normalized_input)
            except Exception:
                _LOGGER.exception(
                    "Failed to apply configuration for %s", entity.entity_id
                )
                errors["base"] = "apply_failed"
            else:
                return self.async_create_entry(title="", data={})

        if not await entity.ensure_configuration():
            return self.async_abort(reason="configuration_timeout")

        return self.async_show_form(
            step_id="channel",
            data_schema=spec.build_schema(entity),
            description_placeholders={"channel_name": _channel_label(entity)},
            errors=errors,
        )
