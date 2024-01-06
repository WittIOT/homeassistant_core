"""Adds config flow for Time & Date integration."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.schema_config_entry_flow import (
    SchemaCommonFlowHandler,
    SchemaConfigFlowHandler,
    SchemaFlowError,
    SchemaFlowFormStep,
)
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.setup import async_prepare_setup_platform

from .const import CONF_DISPLAY_OPTIONS, DOMAIN, OPTION_TYPES
from .sensor import TimeDateSensor

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DISPLAY_OPTIONS): SelectSelector(
            SelectSelectorConfig(
                options=[option for option in OPTION_TYPES if option != "beat"],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="display_options",
            )
        ),
    }
)


async def validate_input(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Validate rest setup."""
    hass = handler.parent_handler.hass
    if hass.config.time_zone is None:
        raise SchemaFlowError("timezone_not_exist")
    return user_input


CONFIG_FLOW = {
    "user": SchemaFlowFormStep(
        schema=USER_SCHEMA,
        preview=DOMAIN,
        validate_user_input=validate_input,
    )
}


class TimeDateConfigFlowHandler(SchemaConfigFlowHandler, domain=DOMAIN):
    """Handle a config flow for Time & Date."""

    config_flow = CONFIG_FLOW

    def async_config_entry_title(self, options: Mapping[str, Any]) -> str:
        """Return config entry title."""
        return f"Time & Date {options[CONF_DISPLAY_OPTIONS]}"

    def async_config_flow_finished(self, options: Mapping[str, Any]) -> None:
        """Abort if instance already exist."""
        self._async_abort_entries_match(dict(options))

    @staticmethod
    async def async_setup_preview(hass: HomeAssistant) -> None:
        """Set up preview WS API."""
        websocket_api.async_register_command(hass, ws_start_preview)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "time_date/start_preview",
        vol.Required("flow_id"): str,
        vol.Required("flow_type"): vol.Any("config_flow"),
        vol.Required("user_input"): dict,
    }
)
@websocket_api.async_response
async def ws_start_preview(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Generate a preview."""
    validated = USER_SCHEMA(msg["user_input"])

    # Create an EntityPlatform, needed for name translations
    platform = await async_prepare_setup_platform(hass, {}, SENSOR_DOMAIN, DOMAIN)
    entity_platform = EntityPlatform(
        hass=hass,
        logger=_LOGGER,
        domain=SENSOR_DOMAIN,
        platform_name=DOMAIN,
        platform=platform,
        scan_interval=timedelta(seconds=3600),
        entity_namespace=None,
    )
    await entity_platform.async_load_translations()

    preview_states: dict[str, dict[str, str | Mapping[str, Any]]] = {}

    @callback
    def async_preview_updated(
        key: str, state: str, attributes: Mapping[str, Any]
    ) -> None:
        """Forward config entry state events to websocket."""
        preview_states[key] = {"attributes": attributes, "state": state}
        connection.send_message(
            websocket_api.event_message(
                msg["id"], {"items": list(preview_states.values())}
            )
        )

    subscriptions: list[CALLBACK_TYPE] = []

    @callback
    def async_unsubscripe_subscriptions() -> None:
        while subscriptions:
            subscriptions.pop()()

    preview_entities = {
        option_type: TimeDateSensor(option_type)
        for option_type in validated[CONF_DISPLAY_OPTIONS]
    }

    for preview_entity in preview_entities.values():
        preview_entity.hass = hass
        preview_entity.platform = entity_platform

    if msg["flow_type"] == "options_flow":
        flow_status = hass.config_entries.options.async_get(msg["flow_id"])
        config_entry_id = flow_status["handler"]
        config_entry = hass.config_entries.async_get_entry(config_entry_id)
        if not config_entry:
            raise HomeAssistantError
        entity_registry = er.async_get(hass)
        entries = er.async_entries_for_config_entry(entity_registry, config_entry_id)
        for option_type, preview_entity in preview_entities.items():
            expected_unique_id = option_type
            for entry in entries:
                if entry.unique_id == expected_unique_id:
                    preview_entity.registry_entry = entry
                    break

    connection.send_result(msg["id"])
    for preview_entity in preview_entities.values():
        subscriptions.append(preview_entity.async_start_preview(async_preview_updated))

    connection.subscriptions[msg["id"]] = async_unsubscripe_subscriptions
