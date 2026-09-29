"""The Liberty integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import DOMAIN

PLATFORMS = [Platform.MEDIA_PLAYER, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Liberty from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["entry"] = entry
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.pop(DOMAIN, None)
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Allow a Liberty device to be deleted from the HA UI.

    Withdrawn rooms deliberately stay as unavailable devices so their
    device_id and entity_id survive (see media_player.handle_config); this
    is how a genuinely retired speaker gets removed. Drop our entity object
    too, so a later config for the same id creates a fresh entity instead of
    writing state to one HA has already torn down.
    """
    entities = hass.data.get(DOMAIN, {}).get("entities", {})
    for domain, room_id in device_entry.identifiers:
        if domain == DOMAIN:
            entities.pop(room_id, None)
    return True
