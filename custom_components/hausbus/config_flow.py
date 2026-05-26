"""Config flow for Haus-Bus integration."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any

from pyhausbus.BusDataMessage import BusDataMessage
from pyhausbus.BusHandler import BusHandler
from pyhausbus.de.hausbus.homeassistant.proxy.controller.data.ModuleId import ModuleId
from pyhausbus.HausBusUtils import HOMESERVER_DEVICE_ID
from pyhausbus.HomeServer import HomeServer
from pyhausbus.IBusDataListener import IBusDataListener
from pyhausbus.ObjectId import ObjectId
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_CONNECTION_TYPE,
    CONNECTION_TYPE_AUTO,
    CONNECTION_TYPE_FIXED_IP,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

DEVICE_SEARCH_TIMEOUT = 5

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONNECTION_TYPE, default=CONNECTION_TYPE_AUTO): SelectSelector(
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


class ConfigFlow(IBusDataListener, config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[misc]
    """Handle a config flow for hausbus."""

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._found_device = False
        self._search_task: asyncio.Task | None = None
        self._connection_data: dict[str, Any] = {}
        self.home_server = HomeServer()
        self.home_server.addBusEventListener(self)

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

    async def async_step_wait_for_device(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Wait for a hausbus device to be found."""
        if not self._search_task:
            self._search_task = self.hass.async_create_task(self._async_wait_for_device())

        if not self._search_task.done():
            return self.async_show_progress(step_id="wait_for_device", progress_action="wait_for_device", progress_task=self._search_task)

        try:
            await self._search_task
        except TimeoutError:
            return self.async_show_progress_done(next_step_id="search_timeout")
        finally:
            self._search_task = None

        return self.async_show_progress_done(next_step_id="search_complete")

    async def async_step_search_timeout(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Inform the user that no device has been found."""
        if user_input is not None:
            return await self.async_step_wait_for_device()

        return self.async_show_form(step_id="search_timeout")

    async def async_step_search_complete(self, user_input: dict[str, Any] | None = None) -> FlowResult:
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
