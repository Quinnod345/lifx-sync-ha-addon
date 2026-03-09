#!/usr/bin/env python3
"""Discover LIFX lights on the LAN and cache them to lights.json.

Bypasses lifxlan's discover_devices() because it calls is_switch() outside its
own try/except, crashing on any device that times out on GetVersion. Instead we
use broadcast_with_resp() directly and build Light objects ourselves.

Reliability strategy
--------------------
A single broadcast pass with a 1-second timeout drops any bulb that is slow to
wake up (common after a power cycle or deep Wi-Fi sleep). This script uses:

  1. Multiple broadcast passes (SCAN_PASSES) so slow bulbs that miss pass 1
     get a second and third chance.
  2. A longer per-pass timeout (SCAN_TIMEOUT_SECS) to catch bulbs that take
     2–3 seconds to respond.
  3. MAC-keyed deduplication so a bulb seen in any pass is kept.
  4. Merge with the existing lights.json — if a bulb was seen before but is
     offline right now it stays in the file with its last-known label and IP.
     Entries are only removed if you pass --flush-missing explicitly.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from lifxlan import LifxLAN
from lifxlan.errors import WorkflowException
from lifxlan.light import Light
from lifxlan.msgtypes import GetService, StateService


DEFAULT_OUTPUT = Path(__file__).with_name("lights.json")

# How many independent broadcast+listen passes to run.
# Each pass re-broadcasts GetService and listens for SCAN_TIMEOUT_SECS.
# Slow bulbs that miss pass 1 are usually caught by pass 2 or 3.
SCAN_PASSES = 3

# Seconds to listen after each broadcast. lifxlan default is 0.3 s which
# is too short for bulbs waking from deep Wi-Fi sleep.
SCAN_TIMEOUT_SECS = 3.0

# Retries when fetching a bulb's label after it responds to GetService.
LABEL_RETRIES = 4
LABEL_RETRY_DELAY = 0.4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover ALL LIFX lights on the LAN and update the cache."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Path to the lights cache JSON file (default: lights.json).",
    )
    parser.add_argument(
        "--flush-missing",
        action="store_true",
        help=(
            "Remove lights that were not found in this scan. "
            "By default, previously discovered lights are kept even if offline."
        ),
    )
    return parser.parse_args()


def _get_label(light: Light) -> str | None:
    """Fetch a bulb's label with retries. Returns None if it never responds."""
    for attempt in range(1, LABEL_RETRIES + 1):
        try:
            return light.get_label()
        except WorkflowException:
            if attempt < LABEL_RETRIES:
                time.sleep(LABEL_RETRY_DELAY)
    return None


def run_discovery(lan: LifxLAN) -> dict[str, dict[str, str]]:
    """Run SCAN_PASSES broadcast passes and return a MAC-keyed dict of results.

    Each pass re-broadcasts GetService and collects responses for
    SCAN_TIMEOUT_SECS. Results from all passes are merged — a bulb only needs
    to respond in one pass to be included.
    """
    found: dict[str, dict[str, str]] = {}  # mac → {label, ip, mac}

    for pass_num in range(1, SCAN_PASSES + 1):
        print(f"  Pass {pass_num}/{SCAN_PASSES}…", end=" ", flush=True)

        try:
            responses = lan.broadcast_with_resp(
                GetService,
                StateService,
                timeout_secs=SCAN_TIMEOUT_SECS,
            )
        except Exception as exc:
            print(f"broadcast error: {exc}")
            continue

        new_this_pass = 0
        for r in responses:
            mac = r.target_addr
            if mac in found:
                continue  # already have this one from an earlier pass

            light = Light(r.target_addr, r.ip_addr, r.service, r.port, lan.source_id, False)
            label = _get_label(light)
            if label is None:
                print(f"\n    Warning: no label for {mac} ({r.ip_addr}), skipping.")
                continue

            found[mac] = {"label": label, "ip": r.ip_addr, "mac": mac}
            new_this_pass += 1

        print(f"found {len(responses)} response(s), {new_this_pass} new.")

    return found


def load_existing(path: Path) -> dict[str, dict[str, str]]:
    """Load previously cached lights keyed by MAC. Returns empty dict if missing."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return {}
        return {entry["mac"]: entry for entry in data if "mac" in entry}
    except Exception:
        return {}


def main() -> int:
    args = parse_args()

    existing = load_existing(args.output)
    print(f"Existing cache: {len(existing)} light(s).")
    print(f"Scanning network ({SCAN_PASSES} passes × {SCAN_TIMEOUT_SECS}s)…")

    lan = LifxLAN()
    discovered = run_discovery(lan)

    if args.flush_missing:
        # Only keep what was found in this scan.
        merged = discovered
    else:
        # Merge: start from existing cache, overlay anything found now.
        # This means offline-but-known lights are preserved with their last IP/label.
        merged = {**existing, **discovered}

    lights = sorted(merged.values(), key=lambda e: (e["label"].lower(), e["ip"]))

    args.output.write_text(json.dumps(lights, indent=2) + "\n", encoding="utf-8")

    added   = len(discovered) - sum(1 for m in discovered if m in existing)
    updated = sum(1 for m in discovered if m in existing and discovered[m] != existing[m])
    kept    = sum(1 for m in existing if m not in discovered)

    print(f"\nResults: {len(lights)} total — "
          f"{added} new, {updated} updated, {kept} kept from previous scan.")

    for light in lights:
        flag = ""
        if light["mac"] not in discovered:
            flag = "  [offline — kept from cache]"
        elif light["mac"] not in existing:
            flag = "  [new]"
        print(f'  - {light["label"]}: {light["ip"]} ({light["mac"]}){flag}')

    print(f"\nSaved to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
