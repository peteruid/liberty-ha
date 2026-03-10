"""Button platform for Liberty integration."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Liberty buttons from a config entry."""
    async_add_entities([LibertyCleanupButton(hass, entry)])


class LibertyCleanupButton(ButtonEntity):
    """Button to remove stale Liberty devices from the registry."""

    _attr_has_entity_name = True
    _attr_name = "Clean Up Stale Devices"
    _attr_icon = "mdi:broom"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the cleanup button."""
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"liberty_bridge_cleanup"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "bridge")},
            name="Liberty Bridge",
            manufacturer="Liberty",
            model="MQTT Bridge",
        )

    async def async_press(self) -> None:
        """Handle button press — remove orphaned devices."""
        entities = self.hass.data.get(DOMAIN, {}).get("entities", {})

        if not entities:
            _LOGGER.warning(
                "No active rooms discovered — is the Liberty app running? "
                "Skipping cleanup to avoid removing all devices"
            )
            return

        registry = dr.async_get(self.hass)
        removed = 0

        for device in list(registry.devices.values()):
            if not any(ident[0] == DOMAIN for ident in device.identifiers):
                continue

            room_ids = [
                ident[1] for ident in device.identifiers if ident[0] == DOMAIN
            ]

            # Skip the bridge device itself
            if "bridge" in room_ids:
                continue

            # Remove if none of this device's room IDs have active entities
            if not any(rid in entities for rid in room_ids):
                _LOGGER.info(
                    "Removing stale device: %s (%s)", device.name, room_ids
                )
                registry.async_remove_device(device.id)
                removed += 1

        _LOGGER.info("Cleanup complete — removed %d stale device(s)", removed)
