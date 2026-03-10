"""Media player platform for Liberty speakers."""
from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import AVAILABILITY_TOPIC, DOMAIN, TOPIC_PREFIX

_LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Liberty media players from a config entry."""
    entities: dict[str, LibertyMediaPlayer] = {}
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["entities"] = entities

    @callback
    def handle_config(msg: mqtt.ReceiveMessage) -> None:
        """Handle room config messages for discovery."""
        parts = msg.topic.split("/")
        if len(parts) != 3:
            return
        room_id = parts[1]

        # Ignore bridge config topic
        if room_id == "bridge":
            return

        # Empty payload = room removed
        if not msg.payload:
            if room_id in entities:
                _LOGGER.info("Room removed: %s", room_id)
                hass.async_create_task(entities[room_id].async_remove())
                del entities[room_id]
                # Remove the device from the registry so it doesn't linger
                registry = dr.async_get(hass)
                device = registry.async_get_device(identifiers={(DOMAIN, room_id)})
                if device:
                    registry.async_remove_device(device.id)
            return

        try:
            config = json.loads(msg.payload)
        except (json.JSONDecodeError, TypeError):
            _LOGGER.warning("Invalid config payload for room %s", room_id)
            return

        if room_id not in entities:
            _LOGGER.info(
                "Discovered room: %s (%s)", config.get("name", room_id), room_id
            )
            entity = LibertyMediaPlayer(hass, room_id, config)
            entities[room_id] = entity
            async_add_entities([entity])
        else:
            # Update existing entity config (e.g. name change)
            entities[room_id].update_config(config)

    # Subscribe to room config topics for auto-discovery
    unsub = await mqtt.async_subscribe(
        hass, f"{TOPIC_PREFIX}/+/config", handle_config, qos=1
    )
    entry.async_on_unload(unsub)



class LibertyMediaPlayer(MediaPlayerEntity):
    """Representation of a Liberty speaker room as a media player."""

    _attr_has_entity_name = True
    _attr_name = None  # Use device name as entity name
    _attr_icon = "mdi:speaker-wireless"
    _attr_supported_features = SUPPORTED_FEATURES

    def __init__(
        self, hass: HomeAssistant, room_id: str, config: dict[str, Any]
    ) -> None:
        """Initialize the media player."""
        self.hass = hass
        self._room_id = room_id
        self._room_name = config.get("name", room_id)
        self._manufacturer = config.get("manufacturer", "Bowers & Wilkins")
        self._model = config.get("model", "Speaker")
        self._sw_version = config.get("sw_version")

        self._attr_icon = (
            "mdi:speaker-multiple"
            if config.get("is_virtual")
            else "mdi:speaker-wireless"
        )
        self._attr_unique_id = f"liberty_{room_id}_media_player"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, room_id)},
            name=self._room_name,
            manufacturer=self._manufacturer,
            model=self._model,
            sw_version=self._sw_version,
        )

        # State
        self._state: MediaPlayerState = MediaPlayerState.IDLE
        self._volume: float | None = None  # 0.0 .. 1.0
        self._muted: bool = False
        self._available: bool = False

        # Media info
        self._media_title: str | None = None
        self._media_artist: str | None = None
        self._media_album: str | None = None
        self._media_source: str | None = None
        self._media_duration: int | None = None
        self._media_position: int | None = None
        self._audio_format: str | None = None

        self._unsubs: list = []

    # -- Properties --

    @property
    def available(self) -> bool:
        """Return True if the entity is available."""
        return self._available

    @property
    def state(self) -> MediaPlayerState:
        """Return the state of the player."""
        return self._state

    @property
    def volume_level(self) -> float | None:
        """Volume level of the media player (0..1)."""
        return self._volume

    @property
    def is_volume_muted(self) -> bool:
        """Return True if volume is muted."""
        return self._muted

    @property
    def media_title(self) -> str | None:
        """Title of current playing media."""
        return self._media_title

    @property
    def media_artist(self) -> str | None:
        """Artist of current playing media."""
        return self._media_artist

    @property
    def media_album_name(self) -> str | None:
        """Album name of current playing media."""
        return self._media_album

    @property
    def source(self) -> str | None:
        """Name of the current input source."""
        return self._media_source

    @property
    def media_duration(self) -> int | None:
        """Duration of current playing media in seconds."""
        return self._media_duration

    @property
    def media_position(self) -> int | None:
        """Position of current playing media in seconds."""
        return self._media_position

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs = {}
        if self._audio_format:
            attrs["audio_format"] = self._audio_format
        return attrs

    # -- MQTT Subscriptions --

    async def async_added_to_hass(self) -> None:
        """Subscribe to MQTT topics when entity is added."""
        prefix = f"{TOPIC_PREFIX}/{self._room_id}"

        self._unsubs.append(
            await mqtt.async_subscribe(
                self.hass, f"{prefix}/state", self._handle_state, qos=0
            )
        )
        self._unsubs.append(
            await mqtt.async_subscribe(
                self.hass, f"{prefix}/volume", self._handle_volume, qos=0
            )
        )
        self._unsubs.append(
            await mqtt.async_subscribe(
                self.hass, f"{prefix}/mute", self._handle_mute, qos=0
            )
        )
        self._unsubs.append(
            await mqtt.async_subscribe(
                self.hass, AVAILABILITY_TOPIC, self._handle_availability, qos=1
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from MQTT topics when entity is removed."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    @callback
    def _handle_state(self, msg: mqtt.ReceiveMessage) -> None:
        """Handle state topic updates."""
        try:
            data = json.loads(msg.payload)
        except (json.JSONDecodeError, TypeError):
            return

        state_str = data.get("state", "idle")
        if state_str == "playing":
            self._state = MediaPlayerState.PLAYING
        elif state_str == "paused":
            self._state = MediaPlayerState.PAUSED
        else:
            self._state = MediaPlayerState.IDLE

        self._media_title = data.get("title")
        self._media_artist = data.get("artist")
        self._media_album = data.get("album")
        self._media_source = data.get("source")
        self._media_duration = data.get("duration")
        self._media_position = data.get("elapsed")
        self._audio_format = data.get("audio_format")

        self.async_write_ha_state()

    @callback
    def _handle_volume(self, msg: mqtt.ReceiveMessage) -> None:
        """Handle volume topic updates."""
        try:
            raw = int(float(msg.payload))
            self._volume = max(0.0, min(1.0, raw / 100.0))
        except (ValueError, TypeError):
            return
        self.async_write_ha_state()

    @callback
    def _handle_mute(self, msg: mqtt.ReceiveMessage) -> None:
        """Handle mute topic updates."""
        self._muted = msg.payload.strip().upper() == "ON"
        self.async_write_ha_state()

    @callback
    def _handle_availability(self, msg: mqtt.ReceiveMessage) -> None:
        """Handle bridge availability updates."""
        self._available = msg.payload.strip().lower() == "online"
        self.async_write_ha_state()

    # -- Commands --

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level (0..1)."""
        int_volume = int(round(volume * 100))
        await mqtt.async_publish(
            self.hass,
            f"{TOPIC_PREFIX}/{self._room_id}/volume/set",
            str(int_volume),
            qos=1,
        )

    async def async_volume_up(self) -> None:
        """Turn volume up."""
        await mqtt.async_publish(
            self.hass,
            f"{TOPIC_PREFIX}/{self._room_id}/volume_up",
            "PRESS",
            qos=1,
        )

    async def async_volume_down(self) -> None:
        """Turn volume down."""
        await mqtt.async_publish(
            self.hass,
            f"{TOPIC_PREFIX}/{self._room_id}/volume_down",
            "PRESS",
            qos=1,
        )

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute the volume."""
        await mqtt.async_publish(
            self.hass,
            f"{TOPIC_PREFIX}/{self._room_id}/mute/set",
            "ON" if mute else "OFF",
            qos=1,
        )

    async def async_media_play(self) -> None:
        """Send play command (only if not already playing)."""
        if self._state != MediaPlayerState.PLAYING:
            await mqtt.async_publish(
                self.hass,
                f"{TOPIC_PREFIX}/{self._room_id}/play_pause",
                "PRESS",
                qos=1,
            )

    async def async_media_pause(self) -> None:
        """Send pause command (only if currently playing)."""
        if self._state == MediaPlayerState.PLAYING:
            await mqtt.async_publish(
                self.hass,
                f"{TOPIC_PREFIX}/{self._room_id}/play_pause",
                "PRESS",
                qos=1,
            )

    async def async_media_play_pause(self) -> None:
        """Toggle play/pause."""
        await mqtt.async_publish(
            self.hass,
            f"{TOPIC_PREFIX}/{self._room_id}/play_pause",
            "PRESS",
            qos=1,
        )

    async def async_media_next_track(self) -> None:
        """Send next track command."""
        await mqtt.async_publish(
            self.hass,
            f"{TOPIC_PREFIX}/{self._room_id}/next_track",
            "PRESS",
            qos=1,
        )

    async def async_media_previous_track(self) -> None:
        """Send previous track command."""
        await mqtt.async_publish(
            self.hass,
            f"{TOPIC_PREFIX}/{self._room_id}/previous_track",
            "PRESS",
            qos=1,
        )

    # -- Config updates --

    @callback
    def update_config(self, config: dict[str, Any]) -> None:
        """Update entity from new config payload."""
        name = config.get("name")
        if name and name != self._room_name:
            self._room_name = name
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, self._room_id)},
                name=self._room_name,
                manufacturer=config.get("manufacturer", self._manufacturer),
                model=config.get("model", self._model),
                sw_version=config.get("sw_version", self._sw_version),
            )
            self.async_write_ha_state()
