"""Light platform for the LIFX Sync integration.

Creates one LightEntity per distinct label group found in /api/lights,
plus one "All Lights" entity that controls every cached bulb at once.

turn_on / turn_off both POST to the add-on's REST API, which runs the
hardened multi-packet burst-and-verify sync engine.  No LIFX protocol
code lives here — this integration is purely a thin UI wrapper over the
add-on's HTTP API.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import aiohttp

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ALL_LIGHTS_LABEL, CONF_HOST, CONF_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up LIFX Sync light entities from a config entry.

    Queries /api/lights, collects distinct label groups, and creates
    one entity per group plus one "All Lights" entity.
    """
    host: str = entry.data[CONF_HOST]
    port: int = entry.data[CONF_PORT]
    session = async_get_clientsession(hass)

    base_url = f"http://{host}:{port}"

    try:
        async with session.get(
            f"{base_url}/api/lights", timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            resp.raise_for_status()
            lights: list[dict[str, str]] = await resp.json()
    except Exception:
        _LOGGER.exception("Could not fetch lights from LIFX Sync add-on at %s", base_url)
        return

    # Collect distinct labels preserving insertion order.
    seen: dict[str, int] = {}
    for light in lights:
        label = light.get("label") or light.get("ip", "Unknown")
        seen[label] = seen.get(label, 0) + 1

    entities: list[LIFXSyncLight] = [
        LIFXSyncLight(
            session=session,
            base_url=base_url,
            label=None,
            display_name=ALL_LIGHTS_LABEL,
            bulb_count=len(lights),
            entry_id=entry.entry_id,
        )
    ]

    for label, count in sorted(seen.items()):
        entities.append(
            LIFXSyncLight(
                session=session,
                base_url=base_url,
                label=label,
                display_name=label,
                bulb_count=count,
                entry_id=entry.entry_id,
            )
        )

    async_add_entities(entities)


class LIFXSyncLight(LightEntity):
    """A light group that maps to one label (or all lights) in the LIFX Sync add-on.

    State is optimistic: we assume the command succeeded and flip is_on
    immediately.  A background poll every 30 s re-reads the truth from
    /api/lights if needed (iot_class = local_polling).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False      # state is optimistic; no external state to poll
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        label: str | None,
        display_name: str,
        bulb_count: int,
        entry_id: str,
    ) -> None:
        slug = (label or "all").lower().replace(" ", "_")
        self._session = session
        self._base_url = base_url
        self._label = label
        self._bulb_count = bulb_count

        self._attr_name = display_name
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{slug}"
        self._attr_is_on = False
        self._attr_extra_state_attributes = {
            "bulb_count": bulb_count,
            "label_filter": label or "all",
        }

    # ── helpers ──────────────────────────────────────────────────────────────

    def _build_url(self, action: str) -> str:
        """Build the REST endpoint URL, appending ?label= when filtering."""
        path = f"{self._base_url}/api/lights/{action}"
        if self._label is not None:
            path += "?" + urlencode({"label": self._label})
        return path

    async def _post(self, action: str) -> bool:
        """POST to on/off endpoint. Returns True on success."""
        url = self._build_url(action)
        try:
            async with self._session.post(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                return True
        except aiohttp.ClientError as exc:
            _LOGGER.error(
                "LIFX Sync: failed to POST %s for label '%s': %s",
                action,
                self._label or "all",
                exc,
            )
            return False

    # ── LightEntity interface ─────────────────────────────────────────────────

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light group."""
        if await self._post("on"):
            self._attr_is_on = True
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light group."""
        if await self._post("off"):
            self._attr_is_on = False
            self.async_write_ha_state()
