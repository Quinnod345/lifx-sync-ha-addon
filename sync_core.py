#!/usr/bin/env python3
"""Hardened sync engine shared by sync_on.py and sync_off.py.

Strategy
--------
Goal: every bulb fires as close to simultaneously as physically possible,
with guaranteed delivery confirmed per-bulb.

Phase 1 — Pre-warm (parallel threads, unsynchronised)
    Each thread constructs the raw UDP packet bytes for its bulb and fires
    one rapid set_power() to wake the bulb's radio. No work happens after
    the barrier — pre-building packets here means zero CPU between barrier
    release and first byte on the wire.

Phase 2 — Raw UDP blast (single tight loop, barrier-gated)
    All threads wait at the barrier.  The main thread is also waiting.
    On release, a SINGLE dedicated sender thread immediately loops over
    all pre-built packets and fires them from one socket as fast as the
    OS allows — no inter-packet gaps, no per-bulb threading overhead.
    The sender sends BLAST_ROUNDS full passes to saturate any single-
    packet loss without adding latency between bulbs.

Phase 3 — Verify + confirmed retry (parallel threads)
    Each worker thread reads back power state and retries with a
    confirmed (ack-required) send if it doesn't match. Up to
    VERIFY_RETRIES cycles with a short settle between each.

Why raw UDP instead of lifxlan set_power()
    lifxlan's set_power() opens and closes a socket per call, acquires
    Python locks, and does bookkeeping between every send.  For maximum
    simultaneity we pre-build all the packet bytes, open one socket, and
    write all of them in a single tight loop with nothing in between.
"""

from __future__ import annotations

import json
import os
import random
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from lifxlan import Light
from lifxlan.errors import WorkflowException


# ── tunables ──────────────────────────────────────────────────────────────────

# How many full passes to blast to every bulb in the synchronised burst.
# Each pass sends one packet to every bulb.  3 passes = 3 packets per bulb
# delivered as fast as the OS socket buffer allows, with zero inter-bulb gap.
BLAST_ROUNDS = 5

# Seconds to wait after the blast before the first verify read.
# Bulbs need a moment to process their last received packet.
BLAST_SETTLE = 0.15

# Max verify+retry cycles per bulb.
VERIFY_RETRIES = 5

# Seconds between verify cycles.
VERIFY_RETRY_DELAY = 0.25

# UDP broadcast port used by the LIFX LAN protocol.
LIFX_PORT = 56700

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


# ── LIFX raw packet builder ───────────────────────────────────────────────────
# Builds a minimal LightSetPower (type 117) packet addressed to a specific MAC.
# Ref: https://lan.developer.lifx.com/docs/packet-contents

def _mac_bytes(mac_str: str) -> bytes:
    """Convert "d0:73:d5:a1:15:fb" → 6-byte little-endian bytes + 2 zero pad."""
    parts = [int(x, 16) for x in mac_str.split(":")]
    return bytes(parts) + b"\x00\x00"  # LIFX target is 8 bytes


def _build_set_power_packet(mac_str: str, power: int, source_id: int) -> bytes:
    """Build a raw LIFX LightSetPower (type=117) UDP packet.

    Frame:
        uint16 size, uint16 protocol|addressable|tagged|origin,
        uint32 source
    Frame address:
        uint8[8] target (mac + 2 zero), uint8[6] reserved,
        uint8 res_required|ack_required, uint8 sequence
    Protocol header:
        uint64 reserved, uint16 type, uint16 reserved
    Payload:
        uint16 level (0 or 65535), uint32 duration (0 = instant)
    """
    size = 42  # 36 header + 6 payload
    # protocol=1024, addressable=1, tagged=0, origin=0  → 0x1400
    protocol_flags = 0x1400
    target = _mac_bytes(mac_str)
    reserved6 = b"\x00" * 6
    res_ack = 0x00           # rapid: no ack, no response requested
    sequence = 0
    reserved8 = b"\x00" * 8
    msg_type = 117            # LightSetPower
    reserved2 = b"\x00\x00"
    duration = 0              # instant transition

    header = struct.pack(
        "<HHI"          # size, protocol_flags, source_id
        "8s6sBB"        # target, reserved6, res_ack, sequence
        "8sHH",         # reserved8, msg_type, reserved2
        size, protocol_flags, source_id,
        target, reserved6, res_ack, sequence,
        reserved8, msg_type, 0,
    )
    payload = struct.pack("<HI", power, duration)
    return header + payload


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
    # Match exact label OR prefix — "Bar" matches "Bar 1", "Bar 2", etc.
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
    packet: bytes,                 # pre-built raw UDP packet for this bulb
) -> None:
    label = light_info.get("label", light_info["ip"])
    ip    = light_info["ip"]
    result = BulbResult(label=label, ip=ip)

    def emit(status: str) -> None:
        if progress_cb:
            progress_cb(ip, label, status)

    try:
        # Phase 1 — pre-warm via lifxlan (handles socket setup per bulb)
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

        # Signal pre-warm done, wait for all peers.
        pre_warm_barrier.wait()

        # Phase 2 — raw UDP blast handled by the central sender, just record time.
        blast_barrier.wait()
        result.sent_at = time.perf_counter()

        if abort.is_set():
            result.aborted = True
            with results_lock: results.append(result)
            return

        # Phase 3 — verify + confirmed retry
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

    # Pre-build every raw UDP packet and pair with (ip, packet) before any
    # threads start — zero work between barrier release and first sendto().
    packets: list[tuple[str, bytes]] = [
        (light["ip"], _build_set_power_packet(light["mac"], target_power, source_id))
        for light in lights_data
    ]

    # Two barriers:
    #   pre_warm_barrier — releases when all workers finish Phase 1
    #   blast_barrier    — releases when the sender is ready to fire
    n = len(lights_data)
    pre_warm_barrier = threading.Barrier(n + 1)  # n workers + main
    blast_barrier    = threading.Barrier(n + 1)  # n workers + sender

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
        for light, (_, pkt) in zip(lights_data, packets)
    ]

    for t in threads:
        t.start()

    # Wait for all pre-warms to complete, then open the blast socket.
    pre_warm_barrier.wait()

    # Open one UDP socket for the entire blast — reusing one socket eliminates
    # per-send socket overhead and keeps the kernel send queue hot.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)  # 1 MB send buffer
    sock.setblocking(False)
    try:
        sock.bind(("", 0))
    except OSError:
        sock.setblocking(True)

    # Release all workers simultaneously, then immediately blast.
    blast_barrier.wait()
    dispatch_start = time.perf_counter()

    # BLAST_ROUNDS full passes: packet for bulb 0, bulb 1, ... bulb N-1, repeat.
    # All sends happen in a single tight loop with no sleep/yield between them.
    for _ in range(BLAST_ROUNDS):
        for ip, pkt in packets:
            try:
                sock.sendto(pkt, (ip, LIFX_PORT))
            except BlockingIOError:
                # Send buffer momentarily full — block briefly and retry once.
                sock.setblocking(True)
                sock.sendto(pkt, (ip, LIFX_PORT))
                sock.setblocking(False)

    sock.close()

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
