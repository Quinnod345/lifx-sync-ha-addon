"""Config flow for the LIFX Sync integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_HOST, CONF_PORT, DEFAULT_HOST, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    }
)


async def _validate_connection(
    hass: HomeAssistant, host: str, port: int
) -> list[dict[str, str]]:
    """Try to reach /api/lights. Returns the lights list on success.

    Raises CannotConnect if the server is unreachable.
    Raises NoLights if the server responds but has no cached lights.
    """
    session = async_get_clientsession(hass)
    url = f"http://{host}:{port}/api/lights"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            resp.raise_for_status()
            lights: list[dict[str, str]] = await resp.json()
    except Exception as exc:
        raise CannotConnect from exc

    if not lights:
        raise NoLights

    return lights


class LIFXSyncConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the UI setup flow for LIFX Sync."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show the connection form and validate on submit."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]

            try:
                await _validate_connection(self.hass, host, port)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except NoLights:
                errors["base"] = "no_lights"
            except Exception:
                _LOGGER.exception("Unexpected error during LIFX Sync config flow")
                errors["base"] = "unknown"
            else:
                # Prevent duplicate entries (single_config_entry is also set in manifest).
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"LIFX Sync ({host}:{port})",
                    data={CONF_HOST: host, CONF_PORT: port},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_SCHEMA,
            errors=errors,
        )


class CannotConnect(Exception):
    """Raised when the add-on is unreachable."""


class NoLights(Exception):
    """Raised when the add-on has no lights cached yet."""
