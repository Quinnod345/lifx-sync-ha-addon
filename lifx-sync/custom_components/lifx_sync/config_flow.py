"""Config flow and options flow for the LIFX Sync integration."""
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
    """Try to reach /api/lights. Returns the lights list on success."""
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


async def _run_discovery(hass: HomeAssistant, host: str, port: int) -> int:
    """POST /api/discover and return the number of lights found."""
    session = async_get_clientsession(hass)
    url = f"http://{host}:{port}/api/discover"
    try:
        async with session.post(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return int(data.get("count", 0))
    except Exception as exc:
        raise CannotConnect from exc


class LIFXSyncConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial UI setup flow for LIFX Sync."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
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

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return LIFXSyncOptionsFlow(config_entry)


class LIFXSyncOptionsFlow(config_entries.OptionsFlow):
    """Options flow — shown when the user clicks Configure on the integration card.

    Lets the user re-discover lights (calls /api/discover on the add-on)
    and optionally change the host/port without removing and re-adding.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show the options form."""
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}

        host: str = self._entry.data[CONF_HOST]
        port: int = self._entry.data[CONF_PORT]

        if user_input is not None:
            new_host = user_input[CONF_HOST].strip()
            new_port = user_input[CONF_PORT]
            rediscover = user_input.get("rediscover", False)

            # Validate connectivity with the (possibly updated) host/port.
            try:
                if rediscover:
                    count = await _run_discovery(self.hass, new_host, new_port)
                    description_placeholders["discovered"] = str(count)
                else:
                    await _validate_connection(self.hass, new_host, new_port)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error in LIFX Sync options flow")
                errors["base"] = "unknown"
            else:
                # Persist updated host/port into the config entry data.
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data={CONF_HOST: new_host, CONF_PORT: new_port},
                )
                # Reload so light entities are rebuilt with the fresh cache.
                await self.hass.config_entries.async_reload(self._entry.entry_id)
                return self.async_create_entry(title="", data={})

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=host): str,
                vol.Required(CONF_PORT, default=port): int,
                vol.Optional("rediscover", default=False): bool,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders=description_placeholders,
        )


class CannotConnect(Exception):
    """Raised when the add-on is unreachable."""


class NoLights(Exception):
    """Raised when the add-on has no lights cached yet."""
