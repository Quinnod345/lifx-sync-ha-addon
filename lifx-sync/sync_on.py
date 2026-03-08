#!/usr/bin/env python3
"""Turn cached LIFX lights on."""

from __future__ import annotations

import argparse
from pathlib import Path

from sync_core import default_lights_file, load_lights, run_sync


POWER_ON = 65535


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Turn all, or a filtered subset of, cached LIFX lights on."
    )
    parser.add_argument("--lights-file", type=Path, default=default_lights_file())
    parser.add_argument(
        "--label",
        dest="labels",
        metavar="LABEL",
        action="append",
        help="Only target lights with this label. Repeat to include multiple labels.",
    )
    parser.add_argument("--timing", action="store_true", help="Print timing summary.")
    parser.add_argument(
        "--verbose", action="store_true", help="Print per-bulb verify attempts."
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lights = load_lights(args.lights_file, args.labels)

    if args.labels:
        print(f"Targeting {len(lights)} light(s) for labels: {args.labels}")

    all_ok, _ = run_sync(lights, POWER_ON, timing=args.timing, verbose=args.verbose)

    if all_ok:
        print(f"All {len(lights)} light(s) confirmed ON.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
