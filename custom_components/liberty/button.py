"""Button platform for Liberty integration."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
        """Handle button press — remove orphaned and withdrawn devices.

        Rooms the app has withdrawn (empty config) are kept as unavailable
        devices on purpose, so their ids survive a power cycle or an app
        restart. This button is the explicit act that actually deletes them,
        along with any device that has no entity at all.
        """
        data = self.hass.data.get(DOMAIN, {})
        entities = data.get("entities", {})
        remove_devices_not_in = data.get("remove_devices_not_in")
        if remove_devices_not_in is None:
            _LOGGER.warning("Media player platform not ready; try again shortly")
            return

        live = {rid for rid, e in entities.items() if not e.config_withdrawn}
        if not live:
            _LOGGER.warning(
                "No live rooms — is the Liberty app running? "
                "Skipping cleanup to avoid removing all devices"
            )
            return

        removed = remove_devices_not_in(live)
        _LOGGER.info("Cleanup complete — removed %d stale device(s)", len(removed))
