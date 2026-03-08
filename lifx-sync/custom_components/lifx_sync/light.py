"""Light platform for the LIFX Sync integration.

Creates one LightEntity per group found in /api/lights plus one "All Lights"
entity that controls every cached bulb at once.

Grouping rules
--------------
Labels that share the same prefix word(s) before a trailing number are merged
into one entity.  Examples:
  - "Bar 1", "Bar 2", "Bar 3"       → light.bar   (label filter = "Bar")
  - "Deck 1", "Deck 2", "Deck 3"    → light.deck
  - "Downlight" (no number suffix)  → light.downlight (exact match)
  - "LIFX Color 67201B"             → light.lifx_color_67201b (no siblings → exact)

The label filter sent to the add-on is the shared prefix for grouped labels,
or the full exact label for singletons.  The add-on's case-insensitive
substring match means "Bar" matches "Bar 1", "Bar 2", etc.

turn_on / turn_off POST to the add-on's REST API which runs the hardened
burst-and-verify sync engine.  No LIFX protocol code lives here.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
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

# Matches a trailing space + number/hex token, e.g. " 1", " 2", " 67201B"
_TRAILING_NUMBER = re.compile(r"\s+\S*\d\S*$")


def _group_prefix(label: str) -> str:
    """Return the grouping prefix for a label.

    "Bar 1" → "Bar", "Deck 2" → "Deck", "Downlight" → "Downlight",
    "LIFX Color 67201B" → "LIFX Color"
    """
    stripped = _TRAILING_NUMBER.sub("", label).strip()
    return stripped if stripped else label


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up LIFX Sync light entities from a config entry."""
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

    # Map prefix → set of full labels that share it.
    prefix_to_labels: dict[str, set[str]] = defaultdict(set)
    for light in lights:
        full_label = light.get("label") or light.get("ip", "Unknown")
        prefix_to_labels[_group_prefix(full_label)].add(full_label)

    # For each prefix:
    #   - multiple full labels → grouped entity, filter = prefix
    #   - single full label whose prefix == itself → singleton, filter = full label
    #   - single full label whose prefix != itself → also grouped, filter = prefix
    #     (handles e.g. a lone "Bar 1" that should still send ?label=Bar)
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

    for prefix, full_labels in sorted(prefix_to_labels.items()):
        is_exact_singleton = len(full_labels) == 1 and next(iter(full_labels)) == prefix
        filter_label = prefix if not is_exact_singleton else next(iter(full_labels))
        bulb_count = sum(
            1 for light in lights
            if (light.get("label") or light.get("ip", "")) in full_labels
        )
        entities.append(
            LIFXSyncLight(
                session=session,
                base_url=base_url,
                label=filter_label,
                display_name=prefix,
                bulb_count=bulb_count,
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
