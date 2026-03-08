#!/usr/bin/env python3
"""Hardened sync engine shared by sync_on.py and sync_off.py."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lifxlan import Light
from lifxlan.errors import WorkflowException


BURST_COUNT = 4
BURST_GAP = 0.02
BURST_SETTLE = 0.2

VERIFY_RETRIES = 5
VERIFY_RETRY_DELAY = 0.3


@dataclass
class BulbResult:
    label: str
    ip: str
    ok: bool = False
    sent_at: float | None = None
    verify_attempts: int = 0
    error: str | None = None
    aborted: bool = False


def default_lights_file() -> Path:
    return Path(
        os.environ.get("LIGHTS_FILE", str(Path(__file__).with_name("lights.json")))
    )


def load_lights(path: Path, labels: list[str] | None) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Run discover.py first to build the cache."
        )
    all_lights: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(all_lights, list) or not all_lights:
        raise ValueError(f"{path} does not contain any cached lights.")
    if any(not {"mac", "ip"}.issubset(light) for light in all_lights):
        raise ValueError(f"{path} contains entries missing mac or ip.")
    if not labels:
        return all_lights
    # Match exact label OR prefix — "Bar" matches "Bar 1", "Bar 2", etc.
    normalized = [label.lower() for label in labels]
    filtered = [
        light for light in all_lights
        if any(
            light.get("label", "").lower() == term
            or light.get("label", "").lower().startswith(term + " ")
            for term in normalized
        )
    ]
    if not filtered:
        available = sorted({light.get("label", "") for light in all_lights})
        raise ValueError(f"No lights matched {labels}. Available: {available}")
    return filtered


def _worker(
    light_info: dict[str, Any],
    target_power: int,
    barrier: threading.Barrier,
    results: list[BulbResult],
    results_lock: threading.Lock,
    abort: threading.Event,
    progress_cb: Callable[[str, str, str], None] | None,
) -> None:
    label = light_info.get("label", light_info["ip"])
    ip = light_info["ip"]
    result = BulbResult(label=label, ip=ip)

    def emit(status: str) -> None:
        if progress_cb:
            progress_cb(ip, label, status)

    try:
        light = Light(light_info["mac"], ip)

        if abort.is_set():
            result.aborted = True
            with results_lock:
                results.append(result)
            return

        emit("warming")
        try:
            light.set_power(target_power, rapid=True)
        except WorkflowException:
            pass

        barrier.wait()

        if abort.is_set():
            result.aborted = True
            with results_lock:
                results.append(result)
            return

        emit("sending")
        result.sent_at = time.perf_counter()

        for i in range(BURST_COUNT):
            if abort.is_set():
                result.aborted = True
                with results_lock:
                    results.append(result)
                return
            try:
                light.set_power(target_power, rapid=True)
            except WorkflowException:
                pass
            if i < BURST_COUNT - 1:
                time.sleep(BURST_GAP)

        time.sleep(BURST_SETTLE)

        confirmed = False
        for attempt in range(1, VERIFY_RETRIES + 1):
            if abort.is_set():
                result.aborted = True
                with results_lock:
                    results.append(result)
                return

            result.verify_attempts = attempt
            emit(f"verifying ({attempt}/{VERIFY_RETRIES})")

            try:
                actual = light.get_power()
                if actual == target_power:
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
        if confirmed:
            emit("ok")
        else:
            result.error = f"state mismatch after {VERIFY_RETRIES} verify attempts"
            emit("failed")

    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
        emit("error")

    with results_lock:
        results.append(result)


def run_sync(
    lights_data: list[dict[str, Any]],
    target_power: int,
    *,
    timing: bool = False,
    verbose: bool = False,
    abort: threading.Event | None = None,
    progress_cb: Callable[[str, str, str], None] | None = None,
) -> tuple[bool, list[BulbResult]]:
    if abort is None:
        abort = threading.Event()

    barrier = threading.Barrier(len(lights_data) + 1)
    results: list[BulbResult] = []
    results_lock = threading.Lock()

    threads = [
        threading.Thread(
            target=_worker,
            args=(light, target_power, barrier, results, results_lock, abort, progress_cb),
            daemon=True,
        )
        for light in lights_data
    ]

    for thread in threads:
        thread.start()

    barrier.wait()
    dispatch_start = time.perf_counter()

    for thread in threads:
        thread.join()

    total_ms = (time.perf_counter() - dispatch_start) * 1000
    aborted = [result for result in results if result.aborted]
    successes = [result for result in results if result.ok]
    failures = [result for result in results if not result.ok and not result.aborted]

    if timing:
        sent = [result for result in successes if result.sent_at is not None]
        if sent:
            t_min = min(result.sent_at for result in sent)  # type: ignore[type-var]
            for result in sorted(sent, key=lambda item: item.sent_at):  # type: ignore[arg-type]
                offset_ms = (result.sent_at - t_min) * 1000  # type: ignore[operator]
                extra = f"  ({result.verify_attempts} verify)" if verbose else ""
                print(f"  {result.label:<32}  +{offset_ms:6.2f} ms{extra}")
            spread = (max(result.sent_at for result in sent) - t_min) * 1000  # type: ignore[type-var]
            print(f"\n  Burst spread:              {spread:.2f} ms")
        print(f"  Total (incl. verify):      {total_ms:.0f} ms")
        print(
            f"  Confirmed: {len(successes)}/{len(results)}  "
            f"Failed: {len(failures)}  Aborted: {len(aborted)}"
        )

    for result in failures:
        print(f"  x {result.label} ({result.ip}): {result.error}")

    all_ok = len(failures) == 0 and len(aborted) == 0
    return all_ok, results
