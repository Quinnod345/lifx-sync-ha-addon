#!/usr/bin/env python3
"""Discover LIFX lights on the LAN and cache them to lights.json.

Bypasses lifxlan's discover_devices() because it calls is_switch() outside its
own try/except, crashing on any device that times out on GetVersion. Instead we
use broadcast_with_resp() directly (the same underlying call) and build Light
objects ourselves with full per-device error handling.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from lifxlan import LifxLAN
from lifxlan.device import Device
from lifxlan.errors import WorkflowException
from lifxlan.light import Light
from lifxlan.msgtypes import GetService, StateService


DEFAULT_OUTPUT = Path(__file__).with_name("lights.json")
RETRIES = 3
RETRY_DELAY = 0.5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover all LIFX lights on the network and cache their MAC/IP details."
    )
    parser.add_argument(
        "--expected-count",
        type=int,
        default=None,
        help=(
            "Stop after finding this many lights. Omit to scan until the timeout "
            "expires and discover every responding light on the network."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Where to write the cached light list.",
    )
    return parser.parse_args()


def get_label_with_retry(device: Device) -> str | None:
    for attempt in range(1, RETRIES + 1):
        try:
            return device.get_label()
        except WorkflowException:
            if attempt < RETRIES:
                time.sleep(RETRY_DELAY)
    return None


def discover_lights(lan: LifxLAN) -> list[dict[str, str]]:
    responses = lan.broadcast_with_resp(GetService, StateService)
    lights: list[dict[str, str]] = []
    skipped = 0

    for response in responses:
        light = Light(
            response.target_addr,
            response.ip_addr,
            response.service,
            response.port,
            lan.source_id,
            lan.verbose,
        )
        label = get_label_with_retry(light)
        if label is None:
            print(
                f"  Warning: could not get label for {response.target_addr} "
                f"({response.ip_addr}), skipping."
            )
            skipped += 1
            continue
        lights.append(
            {"label": label, "ip": response.ip_addr, "mac": response.target_addr}
        )

    if skipped:
        print(f"  {skipped} device(s) skipped due to communication errors.")

    return lights


def main() -> int:
    args = parse_args()
    print("Scanning network...")
    lan = LifxLAN(args.expected_count)

    lights = discover_lights(lan)
    lights.sort(key=lambda item: (item["label"].lower(), item["ip"]))

    args.output.write_text(json.dumps(lights, indent=2) + "\n", encoding="utf-8")

    print(f"\nCached {len(lights)} light(s):")
    for light in lights:
        print(f'  - {light["label"]}: {light["ip"]} ({light["mac"]})')
    print(f"\nSaved to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
