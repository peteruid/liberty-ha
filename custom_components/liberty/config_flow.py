"""Config flow for Liberty integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_SPOTIFY_ENTITY, DOMAIN


class LibertyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Liberty."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Liberty", data={})

        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> LibertyOptionsFlow:
        """Return the options flow handler."""
        return LibertyOptionsFlow(config_entry)


class LibertyOptionsFlow(OptionsFlow):
    """Handle Liberty options."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the Spotify entity option."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        # Find all media_player entities with "spotify" in the entity_id
        all_mp = self.hass.states.async_entity_ids("media_player")
        spotify_entities = sorted(eid for eid in all_mp if "spotify" in eid)

        # Build options: empty string = disabled, then each spotify entity
        options = {"": "None (disabled)"}
        for eid in spotify_entities:
            state = self.hass.states.get(eid)
            friendly = state.attributes.get("friendly_name", eid) if state else eid
            options[eid] = f"{friendly} ({eid})"

        current = self.config_entry.options.get(CONF_SPOTIFY_ENTITY, "")

        schema = vol.Schema(
            {
                vol.Optional(CONF_SPOTIFY_ENTITY, default=current): vol.In(options),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
