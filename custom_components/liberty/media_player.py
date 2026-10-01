"""Media player platform for Liberty speakers."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from datetime import datetime

from homeassistant.components import mqtt
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import AVAILABILITY_TOPIC, CONF_SPOTIFY_ENTITY, DOMAIN, TOPIC_PREFIX

_LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.SHUFFLE_SET
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
    def remove_room(room_id: str) -> None:
        """Delete a room's entity and device. Explicit removal only."""
        entity = entities.pop(room_id, None)
        if entity is not None:
            hass.async_create_task(entity.async_remove())
        registry = dr.async_get(hass)
        device = registry.async_get_device(identifiers={(DOMAIN, room_id)})
        if device:
            registry.async_remove_device(device.id)

    hass.data[DOMAIN]["remove_room"] = remove_room

    @callback
    def remove_devices_not_in(keep: set[str]) -> list[str]:
        """Delete every Liberty device whose room ids all fall outside `keep`.

        HA owns the device registry, so this is the one place staleness is
        decided. The app's clean-up action and the Clean Up Stale Devices
        button both route here. Returns the names removed.
        """
        registry = dr.async_get(hass)
        removed: list[str] = []
        for device in list(registry.devices.values()):
            room_ids = [ident[1] for ident in device.identifiers if ident[0] == DOMAIN]
            if not room_ids or "bridge" in room_ids:
                continue
            if any(rid in keep for rid in room_ids):
                continue
            _LOGGER.info("Removing stale device: %s (%s)", device.name, room_ids)
            removed.append(device.name or room_ids[0])
            for rid in room_ids:
                remove_room(rid)
        return removed

    hass.data[DOMAIN]["remove_devices_not_in"] = remove_devices_not_in

    @callback
    def handle_cleanup(msg: mqtt.ReceiveMessage) -> None:
        """App-initiated clean-up.

        The app sends the ids of the rooms it currently has; we remove every
        device not among them and report the count back on
        liberty/bridge/cleanup_result. The app can't make this call itself:
        a withdrawn room's config is gone from the broker, but its device is
        still here, so only the registry knows what's stale.
        """
        try:
            payload = json.loads(msg.payload)
            keep = set(payload.get("rooms", []))
        except (json.JSONDecodeError, TypeError, AttributeError):
            _LOGGER.warning("Invalid cleanup payload: %s", msg.payload)
            return
        if not keep:
            _LOGGER.warning(
                "Cleanup request listed no rooms — refusing to remove everything"
            )
            removed: list[str] = []
        else:
            removed = remove_devices_not_in(keep)
        hass.async_create_task(
            mqtt.async_publish(
                hass,
                f"{TOPIC_PREFIX}/bridge/cleanup_result",
                json.dumps({"removed": len(removed), "names": removed}),
                qos=1,
                retain=False,
            )
        )

    @callback
    def handle_config(msg: mqtt.ReceiveMessage) -> None:
        """Handle room config messages for discovery.

        Identity has to survive transients. Each entity's unique_id and its
        device's identifiers derive from the room id, which is stable, so as
        long as the device is never deleted HA keeps the same device_id and
        entity_id across app restarts, HA restarts and speakers being
        powered off. Deleting the device on an empty config — what this used
        to do — threw that away: the next config created a new device_id
        (silently orphaning every device-based automation) and, racing the
        asynchronous entity removal, could land the entity on a "_2" id.

        So an empty config now *withdraws* a room: the entity goes
        unavailable and the device stays. Actual deletion takes an explicit
        {"removed": true} from the app's clean-up action, the Clean Up Stale
        Devices button, or deleting the device in the HA UI.
        """
        parts = msg.topic.split("/")
        if len(parts) != 3:
            return
        room_id = parts[1]

        # Ignore bridge config topic
        if room_id == "bridge":
            return

        # Empty payload = room withdrawn (not deleted)
        if not msg.payload:
            if room_id in entities:
                _LOGGER.info("Room withdrawn: %s — marking unavailable", room_id)
                entities[room_id].set_config_withdrawn(True)
            return

        try:
            config = json.loads(msg.payload)
        except (json.JSONDecodeError, TypeError):
            _LOGGER.warning("Invalid config payload for room %s", room_id)
            return

        # Explicit deletion marker from the app's clean-up action. Consume
        # the retained marker afterwards so it doesn't replay on every HA
        # restart against a room that no longer exists.
        if config.get("removed"):
            _LOGGER.info("Room removed: %s", room_id)
            remove_room(room_id)
            hass.async_create_task(
                mqtt.async_publish(hass, msg.topic, "", qos=1, retain=True)
            )
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

    # App-initiated clean-up requests
    unsub_cleanup = await mqtt.async_subscribe(
        hass, f"{TOPIC_PREFIX}/bridge/cleanup", handle_cleanup, qos=1
    )
    entry.async_on_unload(unsub_cleanup)



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
        # Set when the app withdrew this room's config (empty retained
        # payload). The device and entity survive; we just go unavailable
        # until a config comes back.
        self._config_withdrawn: bool = False

        # Media info
        self._media_title: str | None = None
        self._media_artist: str | None = None
        self._media_album: str | None = None
        self._media_source: str | None = None
        self._media_duration: int | None = None
        self._media_position: int | None = None
        self._media_position_updated_at: datetime | None = None
        self._audio_format: str | None = None

        self._unsubs: list = []

    # -- Properties --

    @property
    def available(self) -> bool:
        """Return True if the entity is available."""
        return self._available and not self._config_withdrawn

    @property
    def config_withdrawn(self) -> bool:
        """True while the app has withdrawn this room's config."""
        return self._config_withdrawn

    @callback
    def set_config_withdrawn(self, withdrawn: bool) -> None:
        """Mark the room's config withdrawn/restored without touching identity."""
        if self._config_withdrawn == withdrawn:
            return
        self._config_withdrawn = withdrawn
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

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
    def media_position_updated_at(self) -> datetime | None:
        """When the position was last updated.

        Returns the timestamp captured when we last received a position, so HA
        interpolates the progress bar forward on its own. Returning utcnow()
        here would reset the anchor on every read and freeze the bar between
        updates — which is why the bridge used to have to republish position
        every second.
        """
        return self._media_position_updated_at

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
        # Anchor the position to "now" each time we receive a state update. The
        # bridge only publishes on real changes (play/pause, track, seek), so HA
        # interpolates the progress bar between updates from this timestamp.
        new_position = data.get("elapsed")
        if new_position != self._media_position or self._media_position_updated_at is None:
            self._media_position_updated_at = dt_util.utcnow()
        self._media_position = new_position
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

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs: Any
    ) -> None:
        """Play media via the configured Spotify entity.

        For spotify: URIs, selects this room as the Spotify Connect target,
        then starts playback through the Spotify integration.
        """
        if not media_id.startswith("spotify:"):
            _LOGGER.warning(
                "Liberty only supports spotify: URIs for play_media, got: %s",
                media_id,
            )
            return

        entry = self.hass.data.get(DOMAIN, {}).get("entry")
        if entry is None:
            _LOGGER.error("Liberty config entry not found")
            return

        spotify_entity_id = entry.options.get(CONF_SPOTIFY_ENTITY, "")
        if not spotify_entity_id:
            _LOGGER.warning(
                "No Spotify entity configured. Go to Settings > Integrations "
                "> Liberty > Configure to select your Spotify media player."
            )
            return

        spotify_state = self.hass.states.get(spotify_entity_id)
        if spotify_state is None:
            _LOGGER.error(
                "Configured Spotify entity %s not found", spotify_entity_id
            )
            return
        if spotify_state.state == "unavailable":
            _LOGGER.error(
                "Configured Spotify entity %s is unavailable", spotify_entity_id
            )
            return

        # Select this room as the Spotify Connect target
        await self.hass.services.async_call(
            "media_player",
            "select_source",
            {"entity_id": spotify_entity_id, "source": self._room_name},
            blocking=True,
        )

        # Wait for Spotify to switch playback target
        await asyncio.sleep(2)

        # Start playback on the Spotify entity
        await self.hass.services.async_call(
            "media_player",
            "play_media",
            {
                "entity_id": spotify_entity_id,
                "media_content_type": media_type,
                "media_content_id": media_id,
            },
            blocking=True,
        )

    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Set shuffle mode via the configured Spotify entity."""
        entry = self.hass.data.get(DOMAIN, {}).get("entry")
        if entry is None:
            return

        spotify_entity_id = entry.options.get(CONF_SPOTIFY_ENTITY, "")
        if not spotify_entity_id:
            _LOGGER.warning(
                "No Spotify entity configured. Go to Settings > Integrations "
                "> Liberty > Configure to select your Spotify media player."
            )
            return

        await self.hass.services.async_call(
            "media_player",
            "shuffle_set",
            {"entity_id": spotify_entity_id, "shuffle": shuffle},
            blocking=True,
        )

    # -- Config updates --

    @callback
    def update_config(self, config: dict[str, Any]) -> None:
        """Update entity from new config payload."""
        # A config arriving at all means the room is back.
        self.set_config_withdrawn(False)
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
