"""Diagnostics support for Haus-Bus integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    gateway = entry.runtime_data.gateway

    devices_info: dict[str, Any] = {}
    for device_id, device in gateway.devices.items():
        channels = gateway.channels.get(device_id, {})
        devices_info[device_id] = {
            "device_id": device.device_id,
            "hass_device_entry_id": device.hass_device_entry_id,
            "special_type": device.special_type,
            "channel_count": len(channels),
            "channels": [
                {
                    "class": entity.__class__.__name__,
                    "unique_id": entity._attr_unique_id,
                    "name": entity._attr_name,
                }
                for entity in channels.values()
            ],
        }

    return {
        "config_entry": {
            "entry_id": entry.entry_id,
            "data": dict(entry.data),
        },
        "gateway": {
            "device_count": len(gateway.devices),
            "registered_channels": len(gateway.registered_channels),
            "event_entities": len(gateway.events),
            "devices": devices_info,
        },
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a specific device."""
    gateway = entry.runtime_data.gateway

    device_id: str | None = None
    for domain, did in device.identifiers:
        if domain == DOMAIN:
            device_id = did
            break

    if device_id is None or device_id not in gateway.devices:
        return {"error": "device not found"}

    hausbus_device = gateway.devices[device_id]
    channels = gateway.channels.get(device_id, {})

    channel_states: list[dict[str, Any]] = []
    for (class_id, instance_id), entity in channels.items():
        state = hass.states.get(entity.entity_id) if entity.entity_id else None
        channel_states.append(
            {
                "class_id": class_id,
                "instance_id": instance_id,
                "entity_class": entity.__class__.__name__,
                "unique_id": entity._attr_unique_id,
                "name": entity._attr_name,
                "state": state.state if state else "unavailable",
                "extra_attributes": dict(entity._attr_extra_state_attributes),
            }
        )

    return {
        "device_id": hausbus_device.device_id,
        "hass_device_entry_id": hausbus_device.hass_device_entry_id,
        "special_type": hausbus_device.special_type,
        "channels": channel_states,
    }
