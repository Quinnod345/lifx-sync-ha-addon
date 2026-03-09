#!/usr/bin/env python3
"""Hardened sync engine shared by sync_on.py and sync_off.py.

Strategy
--------
Goal: every bulb fires as close to simultaneously as physically possible,
with guaranteed delivery confirmed per-bulb.

Why ON lags but OFF is perfect
-------------------------------
Power-OFF is instantaneous in bulb firmware — it just cuts the LED driver.
Power-ON triggers a flash read: each bulb independently reads its last
color/brightness from onboard flash, then ramps up. That takes 10–200 ms
per bulb and varies, causing stagger even with a simultaneous UDP blast.

The fix: use LightSetState (type 101) for power-on instead of LightSetPower
(type 117). LightSetState carries both the power level AND the color in a
single packet. The bulb applies them atomically from the packet payload —
no flash read needed — so turn-on fires as fast as turn-off.

Phases
------
Phase 1 — Pre-warm (parallel threads, unsynchronised)
    Each thread fires one rapid set_power() to wake the bulb's UDP stack and
    clear any in-flight transition state. Light objects are constructed here
    so no work remains between barrier release and the blast.

Phase 2 — Raw UDP blast (single tight loop, barrier-gated)
    All workers wait at a barrier. On release, a single UDP socket fires
    BLAST_ROUNDS full passes across every bulb IP in one tight loop with
    zero inter-bulb gap. The OS kernel queues all packets simultaneously.
    For power-on, each packet is LightSetState (color+power atomically).
    For power-off, each packet is LightSetPower (minimal payload).

Phase 3 — Verify + confirmed retry (parallel threads)
    Each worker reads back power state and retries with a confirmed
    (ack-required) send if it doesn't match target. Up to VERIFY_RETRIES
    cycles with a short settle between each.
"""

from __future__ import annotations

import json
import os
import random
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lifxlan import Light
from lifxlan.errors import WorkflowException


# ── tunables ──────────────────────────────────────────────────────────────────

# Full passes of the raw UDP blast per bulb.
BLAST_ROUNDS = 5

# Seconds to wait after the blast before the first verify read.
BLAST_SETTLE = 0.15

# Max verify+retry cycles per bulb.
VERIFY_RETRIES = 5

# Seconds between verify cycles.
VERIFY_RETRY_DELAY = 0.25

# UDP port used by the LIFX LAN protocol.
LIFX_PORT = 56700

# Color embedded in the power-on LightSetState packet.
# HSBK: hue=0, saturation=0 (white), brightness=65535 (full), kelvin=2700 (warm)
# The bulb applies this atomically with the power-on, no flash read needed.
ON_HSBK = (0, 0, 65535, 2700)

# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BulbResult:
    label: str
    ip: str
    ok: bool = False
    sent_at: float | None = None
    verify_attempts: int = 0
    error: str | None = None
    aborted: bool = False


# ── LIFX raw packet builders ──────────────────────────────────────────────────

def _mac_bytes(mac_str: str) -> bytes:
    """Convert "d0:73:d5:a1:15:fb" → 8-byte LIFX target field."""
    return bytes(int(x, 16) for x in mac_str.split(":")) + b"\x00\x00"


def _header(size: int, msg_type: int, mac_str: str, source_id: int) -> bytes:
    """Build the 36-byte LIFX frame + frame-address + protocol header."""
    protocol_flags = 0x1400  # protocol=1024, addressable=1, tagged=0, origin=0
    target = _mac_bytes(mac_str)
    return struct.pack(
        "<HHI8s6sBB8sHH",
        size,
        protocol_flags,
        source_id,
        target,
        b"\x00" * 6,  # reserved
        0x00,          # res_required | ack_required = 0 (rapid)
        0,             # sequence
        b"\x00" * 8,  # reserved
        msg_type,
        0,             # reserved
    )


def _build_set_power_packet(mac_str: str, power: int, source_id: int) -> bytes:
    """LightSetPower (type 117): 36-byte header + 6-byte payload. Used for OFF."""
    hdr = _header(42, 117, mac_str, source_id)
    payload = struct.pack("<HI", power, 0)  # level, duration=0 (instant)
    return hdr + payload


def _build_set_state_packet(
    mac_str: str, hsbk: tuple[int, int, int, int], power: int, source_id: int
) -> bytes:
    """LightSetState (type 101): sets color+power atomically in one packet.

    By carrying both color and power level in the same message, the bulb
    applies them from the packet payload directly — no flash read needed.
    This is why power-on with LightSetState fires as fast as power-off.

    Payload (52 bytes total, 16 payload):
        uint8[2] reserved, uint16 hue, uint16 sat, uint16 brightness,
        uint16 kelvin, uint16 reserved, uint16 power, uint8[32] label (empty),
        uint64 reserved
    """
    hdr = _header(92, 101, mac_str, source_id)
    h, s, b, k = hsbk
    payload = struct.pack(
        "<2sHHHHHHH32sQ",
        b"\x00\x00",   # reserved
        h, s, b, k,    # hue, sat, brightness, kelvin
        0,             # reserved (uint16)
        power,         # power level (uint16)
        0,             # reserved (uint16) — pad to align label
        b"\x00" * 32,  # label (empty = don't change)
        0,             # reserved (uint64)
    )
    return hdr + payload


# ── helpers ───────────────────────────────────────────────────────────────────

def _blast(packets: list[tuple[str, bytes]]) -> None:
    """Open one UDP socket and fire BLAST_ROUNDS passes as fast as possible."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    sock.setblocking(False)
    try:
        sock.bind(("", 0))
    except OSError:
        sock.setblocking(True)
    try:
        for _ in range(BLAST_ROUNDS):
            for ip, pkt in packets:
                try:
                    sock.sendto(pkt, (ip, LIFX_PORT))
                except BlockingIOError:
                    sock.setblocking(True)
                    sock.sendto(pkt, (ip, LIFX_PORT))
                    sock.setblocking(False)
    finally:
        sock.close()


def load_lights(path: Path, labels: list[str] | None) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Run discover.py first to build the cache."
        )
    all_lights: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(all_lights, list) or not all_lights:
        raise ValueError(f"{path} does not contain any cached lights.")
    if any(not {"mac", "ip"}.issubset(l) for l in all_lights):
        raise ValueError(f"{path} contains entries missing mac or ip.")
    if not labels:
        return all_lights
    normalised = [lbl.lower() for lbl in labels]
    filtered = [
        l for l in all_lights
        if any(
            l.get("label", "").lower() == term
            or l.get("label", "").lower().startswith(term + " ")
            for term in normalised
        )
    ]
    if not filtered:
        available = sorted({l.get("label", "") for l in all_lights})
        raise ValueError(f"No lights matched {labels}. Available: {available}")
    return filtered


def default_lights_file() -> Path:
    return Path(
        os.environ.get("LIGHTS_FILE", str(Path(__file__).with_name("lights.json")))
    )


# ── worker ────────────────────────────────────────────────────────────────────

def _worker(
    light_info: dict[str, Any],
    target_power: int,
    pre_warm_barrier: threading.Barrier,
    blast_barrier: threading.Barrier,
    results: list[BulbResult],
    results_lock: threading.Lock,
    abort: threading.Event,
    progress_cb: Callable[[str, str, str], None] | None,
    packet: bytes,
) -> None:
    label = light_info.get("label", light_info["ip"])
    ip    = light_info["ip"]
    result = BulbResult(label=label, ip=ip)

    def emit(status: str) -> None:
        if progress_cb:
            progress_cb(ip, label, status)

    try:
        if abort.is_set():
            result.aborted = True
            with results_lock: results.append(result)
            return

        emit("warming")
        light = Light(light_info["mac"], ip)
        try:
            light.set_power(target_power, rapid=True)
        except WorkflowException:
            pass

        pre_warm_barrier.wait()

        blast_barrier.wait()
        result.sent_at = time.perf_counter()

        if abort.is_set():
            result.aborted = True
            with results_lock: results.append(result)
            return

        time.sleep(BLAST_SETTLE)

        confirmed = False
        for attempt in range(1, VERIFY_RETRIES + 1):
            if abort.is_set():
                result.aborted = True
                with results_lock: results.append(result)
                return

            result.verify_attempts = attempt
            emit(f"verifying ({attempt}/{VERIFY_RETRIES})")

            try:
                if light.get_power() == target_power:
                    confirmed = True
                    break
            except WorkflowException:
                pass

            try:
                light.set_power(target_power, rapid=False)
            except WorkflowException:
                pass

            time.sleep(VERIFY_RETRY_DELAY)

        result.ok = confirmed
        emit("ok" if confirmed else "failed")
        if not confirmed:
            result.error = f"state mismatch after {VERIFY_RETRIES} verify attempts"

    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
        emit("error")

    with results_lock:
        results.append(result)


# ── run_sync ──────────────────────────────────────────────────────────────────

def run_sync(
    lights_data: list[dict[str, Any]],
    target_power: int,
    *,
    timing: bool = False,
    verbose: bool = False,
    abort: threading.Event | None = None,
    progress_cb: Callable[[str, str, str], None] | None = None,
) -> tuple[bool, list[BulbResult]]:
    """Dispatch set_power(target_power) to all lights and verify delivery."""
    if abort is None:
        abort = threading.Event()

    source_id = random.randrange(2, 1 << 32)
    power_on = target_power == 65535

    # For power-on, use LightSetState (type 101) which carries color+power
    # atomically in one packet — no flash read on the bulb side, so all bulbs
    # fire their LED driver at the same time as they would for power-off.
    # For power-off, use the minimal LightSetPower (type 117) packet.
    if power_on:
        power_packets: list[tuple[str, bytes]] = [
            (light["ip"], _build_set_state_packet(light["mac"], ON_HSBK, target_power, source_id))
            for light in lights_data
        ]
    else:
        power_packets = [
            (light["ip"], _build_set_power_packet(light["mac"], target_power, source_id))
            for light in lights_data
        ]

    n = len(lights_data)
    pre_warm_barrier = threading.Barrier(n + 1)
    blast_barrier    = threading.Barrier(n + 1)

    results: list[BulbResult] = []
    results_lock = threading.Lock()

    threads = [
        threading.Thread(
            target=_worker,
            args=(
                light, target_power,
                pre_warm_barrier, blast_barrier,
                results, results_lock,
                abort, progress_cb,
                pkt,
            ),
            daemon=True,
        )
        for light, (_, pkt) in zip(lights_data, power_packets)
    ]

    for t in threads:
        t.start()

    pre_warm_barrier.wait()

    blast_barrier.wait()
    dispatch_start = time.perf_counter()

    _blast(power_packets)

    for t in threads:
        t.join()

    total_ms = (time.perf_counter() - dispatch_start) * 1000
    aborted   = [r for r in results if r.aborted]
    successes = [r for r in results if r.ok]
    failures  = [r for r in results if not r.ok and not r.aborted]

    if timing:
        sent = [r for r in successes if r.sent_at is not None]
        if sent:
            t_min = min(r.sent_at for r in sent)  # type: ignore[type-var]
            for r in sorted(sent, key=lambda x: x.sent_at):  # type: ignore[arg-type]
                offset_ms = (r.sent_at - t_min) * 1000  # type: ignore[operator]
                extra = f"  ({r.verify_attempts} verify)" if verbose else ""
                print(f"  {r.label:<32}  +{offset_ms:6.2f} ms{extra}")
            spread = (max(r.sent_at for r in sent) - t_min) * 1000  # type: ignore[type-var]
            print(f"\n  Burst spread:              {spread:.2f} ms")
        print(f"  Total (incl. verify):      {total_ms:.0f} ms")
        print(
            f"  Confirmed: {len(successes)}/{len(results)}  "
            f"Failed: {len(failures)}  Aborted: {len(aborted)}"
        )

    for r in failures:
        print(f"  x {r.label} ({r.ip}): {r.error}")

    all_ok = len(failures) == 0 and len(aborted) == 0
    return all_ok, results
