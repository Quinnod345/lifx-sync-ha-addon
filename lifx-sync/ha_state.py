#!/usr/bin/env python3
"""Query Home Assistant entity states to get last-known light color/brightness.

The Supervisor token is injected automatically by HAOS into every add-on as
SUPERVISOR_TOKEN — no user configuration needed. The Supervisor API is always
reachable at http://supervisor/core/api inside a running add-on.

Usage
-----
    from ha_state import fetch_group_hsbk

    # Returns a dict mapping each light's MAC address to an HSBK tuple,
    # using last-known HA state where available, falling back to DEFAULT_HSBK.
    hsbk_map = fetch_group_hsbk(lights_data, label_filter)

HSBK format
-----------
    (hue: 0-65535, saturation: 0-65535, brightness: 0-65535, kelvin: 2500-9000)
    This maps directly to the LIFX LAN protocol LightSetState payload.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.request
import json
from typing import Any

logger = logging.getLogger(__name__)

# Fallback color when HA is unavailable or entity not found.
# Warm white, full brightness.
DEFAULT_HSBK: tuple[int, int, int, int] = (0, 0, 65535, 2700)

# HA Supervisor API base URL — only reachable inside an add-on container.
_SUPERVISOR_URL = "http://supervisor/core/api"


def _supervisor_token() -> str | None:
    return os.environ.get("SUPERVISOR_TOKEN")


def _entity_id_for_label(label: str) -> str:
    """Convert a label (or prefix) to a HA entity_id.

    "Downlight"  → light.downlight
    "Bar"        → light.bar
    "LIFX Color 67201B" → light.lifx_color_67201b
    """
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return f"light.{slug}"


def _fetch_state(entity_id: str, token: str) -> dict[str, Any] | None:
    """Fetch a single entity state from the HA API. Returns None on any error."""
    url = f"{_SUPERVISOR_URL}/states/{entity_id}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        logger.debug("Could not fetch HA state for %s: %s", entity_id, exc)
        return None


def _ha_attrs_to_hsbk(attrs: dict[str, Any]) -> tuple[int, int, int, int]:
    """Convert HA light attributes to LIFX HSBK tuple.

    HA attributes:
        hs_color: [hue_degrees (0-360), saturation_percent (0-100)]
        brightness: 0-255
        color_temp: mireds (153-500) — only used if hs_color absent
        color_temp_kelvin: kelvin — preferred over mireds when present

    LIFX HSBK:
        hue: 0-65535, saturation: 0-65535, brightness: 0-65535, kelvin: 2500-9000
    """
    # Hue: HA degrees (0-360) → LIFX (0-65535)
    hs = attrs.get("hs_color")
    if hs:
        hue = round((hs[0] / 360.0) * 65535)
        sat = round((hs[1] / 100.0) * 65535)
    else:
        hue = 0
        sat = 0

    # Brightness: HA 0-255 → LIFX 0-65535
    ha_brightness = attrs.get("brightness")
    brightness = round((ha_brightness / 255.0) * 65535) if ha_brightness is not None else 65535

    # Kelvin: prefer color_temp_kelvin, fall back to mireds conversion
    kelvin = attrs.get("color_temp_kelvin")
    if kelvin is None:
        mireds = attrs.get("color_temp")
        if mireds:
            kelvin = round(1_000_000 / mireds)
    kelvin = max(2500, min(9000, int(kelvin))) if kelvin else 2700

    return (hue, sat, brightness, kelvin)


def _group_prefix(label: str) -> str:
    """Strip trailing number/hex token to get the group prefix.

    "Bar 1" → "Bar", "Downlight" → "Downlight", "LIFX Color 67201B" → "LIFX Color"
    """
    stripped = re.sub(r"\s+\S*\d\S*$", "", label).strip()
    return stripped if stripped else label


def fetch_group_hsbk(
    lights_data: list[dict[str, Any]],
    label_filter: str | None,
) -> dict[str, tuple[int, int, int, int]]:
    """Return a MAC → HSBK map for the given lights, using HA state where available.

    Args:
        lights_data:  list of {"mac", "ip", "label"} dicts for lights to sync
        label_filter: the label/prefix used to filter this group (e.g. "Bar", "Downlight")
                      or None for "all lights"

    Returns:
        dict mapping each light MAC to an HSBK tuple.
        Falls back to DEFAULT_HSBK for any light whose state cannot be fetched.
    """
    token = _supervisor_token()
    result: dict[str, tuple[int, int, int, int]] = {}

    if not token:
        logger.debug("No SUPERVISOR_TOKEN — using default HSBK for all lights.")
        return {light["mac"]: DEFAULT_HSBK for light in lights_data}

    # Determine which entity_id to query.
    # If a label_filter is set, use its group prefix as the entity.
    # For "all lights" (no filter), we'll try per-label fallback below.
    group_entity: str | None = None
    if label_filter:
        group_entity = _entity_id_for_label(_group_prefix(label_filter))

    # Fetch the group-level entity state once (covers filtered groups).
    group_hsbk: tuple[int, int, int, int] | None = None
    if group_entity:
        state = _fetch_state(group_entity, token)
        if state and state.get("state") not in (None, "unavailable", "unknown"):
            attrs = state.get("attributes", {})
            group_hsbk = _ha_attrs_to_hsbk(attrs)
            logger.debug("HA state for %s → HSBK %s", group_entity, group_hsbk)

    # For each light, try individual entity first, then group, then default.
    for light in lights_data:
        mac = light["mac"]
        label = light.get("label", "")
        individual_entity = _entity_id_for_label(label)

        hsbk: tuple[int, int, int, int] | None = None

        # Try the individual bulb entity if it differs from the group entity.
        if individual_entity != group_entity:
            state = _fetch_state(individual_entity, token)
            if state and state.get("state") not in (None, "unavailable", "unknown"):
                hsbk = _ha_attrs_to_hsbk(state.get("attributes", {}))

        # Fall back to group entity state.
        if hsbk is None and group_hsbk is not None:
            hsbk = group_hsbk

        # Last resort: default warm white.
        if hsbk is None:
            hsbk = DEFAULT_HSBK

        result[mac] = hsbk

    return result
