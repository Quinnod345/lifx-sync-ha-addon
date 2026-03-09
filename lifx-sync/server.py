#!/usr/bin/env python3
"""Web UI + REST API for the Home Assistant add-on."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from lifxlan import LifxLAN

from discover import run_discovery
from ha_state import fetch_group_hsbk
from sync_core import default_lights_file, load_lights, run_sync


BASE_DIR = Path(__file__).resolve().parent
LIGHTS_FILE = default_lights_file()
HOST = os.environ.get("LIFX_SYNC_HOST", "0.0.0.0")
PORT = int(os.environ.get("LIFX_SYNC_PORT", "5050"))
POWER_ON = 65535
POWER_OFF = 0


@dataclass
class SyncJob:
    action: str
    label: str | None
    abort: threading.Event | None = None

    def __post_init__(self) -> None:
        if self.abort is None:
            self.abort = threading.Event()


_job_queue: queue.Queue[SyncJob] = queue.Queue()
_current_job: SyncJob | None = None
_current_lock = threading.Lock()
_run_lock = threading.Lock()

_sse_clients: list[tuple[queue.Queue[str], threading.Event]] = []
_sse_clients_lock = threading.Lock()


def write_lights_cache(lights: list[dict[str, str]]) -> None:
    LIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LIGHTS_FILE.write_text(json.dumps(lights, indent=2) + "\n", encoding="utf-8")


def discover_and_cache() -> list[dict[str, str]]:
    lan = LifxLAN()
    # Load existing cache so offline lights are preserved across re-discovers.
    existing: dict[str, dict[str, str]] = {}
    if LIGHTS_FILE.exists():
        try:
            data = json.loads(LIGHTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = {e["mac"]: e for e in data if "mac" in e}
        except Exception:
            pass

    discovered = run_discovery(lan)
    merged = {**existing, **discovered}
    lights = sorted(merged.values(), key=lambda e: (e["label"].lower(), e["ip"]))
    write_lights_cache(lights)
    return lights


def push_sse(event: str, data: Any) -> None:
    message = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    stale: list[tuple[queue.Queue[str], threading.Event]] = []
    with _sse_clients_lock:
        for client_queue, closed in _sse_clients:
            if closed.is_set():
                stale.append((client_queue, closed))
                continue
            try:
                client_queue.put_nowait(message)
            except queue.Full:
                pass
        for item in stale:
            if item in _sse_clients:
                _sse_clients.remove(item)


def bulb_progress(ip: str, label: str, status: str) -> None:
    push_sse("bulb", {"ip": ip, "label": label, "status": status})


def current_cache() -> list[dict[str, Any]]:
    if not LIGHTS_FILE.exists():
        return []
    return json.loads(LIGHTS_FILE.read_text(encoding="utf-8"))


def worker_loop() -> None:
    global _current_job
    while True:
        job = _job_queue.get()
        with _current_lock:
            _current_job = job

        try:
            lights_data = load_lights(LIGHTS_FILE, [job.label] if job.label else None)
        except Exception as exc:
            push_sse(
                "done",
                {
                    "ok": False,
                    "action": job.action,
                    "error": str(exc),
                    "confirmed": 0,
                    "total": 0,
                    "failed": [],
                    "aborted": False,
                },
            )
            with _current_lock:
                _current_job = None
            _job_queue.task_done()
            continue

        target_power = POWER_ON if job.action == "on" else POWER_OFF
        push_sse(
            "start",
            {"action": job.action, "label": job.label, "total": len(lights_data)},
        )

        # For power-on, fetch each bulb's last-known color/brightness from HA
        # so lights restore to their previous state rather than defaulting to
        # warm white. Falls back gracefully if HA is unreachable.
        hsbk_map = None
        if target_power == POWER_ON:
            try:
                hsbk_map = fetch_group_hsbk(lights_data, job.label)
            except Exception:
                pass  # non-fatal — run_sync falls back to ON_HSBK

        with _run_lock:
            all_ok, results = run_sync(
                lights_data,
                target_power,
                abort=job.abort,
                progress_cb=bulb_progress,
                hsbk_map=hsbk_map,
            )

        confirmed = sum(1 for result in results if result.ok)
        failed = [
            {"ip": result.ip, "label": result.label, "error": result.error}
            for result in results
            if not result.ok and not result.aborted
        ]
        aborted = any(result.aborted for result in results)

        push_sse(
            "done",
            {
                "ok": all_ok,
                "action": job.action,
                "confirmed": confirmed,
                "total": len(lights_data),
                "failed": failed,
                "aborted": aborted,
            },
        )

        with _current_lock:
            _current_job = None
        _job_queue.task_done()


def enqueue(action: str, label: str | None) -> None:
    with _current_lock:
        if _current_job is not None:
            _current_job.abort.set()
    _job_queue.put(SyncJob(action=action, label=label))


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LIFX Sync</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@700;800&display=swap');

  :root {
    --bg: #0d0d0f;
    --surface: #18181c;
    --border: #2a2a30;
    --text: #e8e8f0;
    --muted: #727286;
    --on: #ff9500;
    --off: #49495c;
    --ok: #34d399;
    --warn: #fbbf24;
    --err: #f87171;
    --work: #60a5fa;
  }

  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Mono', monospace;
    padding: 40px 20px 72px;
  }
  .shell { max-width: 760px; margin: 0 auto; }
  h1 {
    margin: 0;
    font-family: 'Syne', sans-serif;
    font-size: clamp(2rem, 6vw, 3rem);
    background: linear-gradient(135deg, #fff 30%, #ffb347 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  .sub {
    margin: 8px 0 0;
    color: var(--muted);
    font-size: 0.85rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .toolbar, .status, .card {
    margin-top: 20px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px;
  }
  .toolbar, .status { padding: 16px 18px; }
  .toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }
  .toolbar-copy {
    color: var(--muted);
    font-size: 0.78rem;
  }
  .toolbar-actions { display: flex; gap: 10px; }
  .groups { display: grid; gap: 16px; margin-top: 20px; }
  .card { padding: 18px; }
  .card-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }
  .title {
    font-family: 'Syne', sans-serif;
    font-size: 1.05rem;
  }
  .count {
    color: var(--muted);
    font-size: 0.75rem;
    margin-top: 4px;
  }
  .actions { display: flex; gap: 8px; }
  button {
    border: none;
    border-radius: 10px;
    padding: 10px 18px;
    font-family: inherit;
    cursor: pointer;
    transition: opacity 0.2s, transform 0.1s;
  }
  button:active { transform: scale(0.98); }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  .btn-on { background: var(--on); color: #000; }
  .btn-off { background: var(--off); color: var(--text); }
  .btn-alt { background: #252532; color: var(--text); }
  .pills {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 14px;
  }
  .pill {
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 0.68rem;
    border: 1px solid var(--border);
    color: var(--muted);
    background: #20202a;
  }
  .pill.warming { color: var(--warn); border-color: #5a4300; background: #2a2000; }
  .pill.sending { color: var(--work); border-color: #1d4e89; background: #142338; }
  .pill.verifying { color: var(--warn); border-color: #5a4300; background: #2a2000; }
  .pill.ok { color: var(--ok); border-color: #1a5c3a; background: #0d2a1a; }
  .pill.failed, .pill.error { color: var(--err); border-color: #5c1a1a; background: #2a0d0d; }
  .status { display: grid; gap: 10px; }
  .status-line { display: flex; justify-content: space-between; gap: 12px; }
  .status-text { color: var(--muted); }
  .bar {
    height: 6px;
    background: #23232d;
    border-radius: 999px;
    overflow: hidden;
  }
  .bar-fill {
    height: 100%;
    width: 0%;
    background: var(--on);
    transition: width 0.25s ease;
  }
  .toast-wrap {
    position: fixed;
    right: 24px;
    bottom: 24px;
    display: grid;
    gap: 10px;
  }
  .toast {
    padding: 10px 14px;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: var(--surface);
    min-width: 260px;
  }
</style>
</head>
<body>
<div class="shell">
  <h1>LIFX Sync</h1>
  <p class="sub" id="subtitle">Loading lights...</p>

  <div class="toolbar">
    <div class="toolbar-copy">Home Assistant add-on UI for synced LIFX LAN control.</div>
    <div class="toolbar-actions">
      <button class="btn-alt" id="discover-btn">Re-discover</button>
    </div>
  </div>

  <div class="status">
    <div class="status-line">
      <div class="status-text" id="status-text">Idle</div>
      <div class="status-text" id="status-count"></div>
    </div>
    <div class="bar"><div class="bar-fill" id="status-fill"></div></div>
  </div>

  <div class="groups" id="groups"></div>
</div>

<div class="toast-wrap" id="toasts"></div>

<script>
const groupsEl = document.getElementById('groups');
const subtitleEl = document.getElementById('subtitle');
const statusTextEl = document.getElementById('status-text');
const statusCountEl = document.getElementById('status-count');
const statusFillEl = document.getElementById('status-fill');
const discoverBtn = document.getElementById('discover-btn');

const pillMap = {};
const buttonMap = [];
let currentTotal = 0;
let currentConfirmed = 0;

function toast(text) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = text;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function setButtonsDisabled(disabled) {
  for (const btn of buttonMap) btn.disabled = disabled;
  discoverBtn.disabled = disabled;
}

function setStatus(text, confirmed = null, total = null) {
  statusTextEl.textContent = text;
  if (confirmed !== null && total !== null) {
    statusCountEl.textContent = `${confirmed}/${total}`;
    statusFillEl.style.width = total > 0 ? `${Math.round((confirmed / total) * 100)}%` : '0%';
  } else {
    statusCountEl.textContent = '';
    statusFillEl.style.width = '0%';
  }
}

function setPillState(ip, state) {
  const pill = pillMap[ip];
  if (!pill) return;
  pill.className = `pill ${state === 'idle' ? '' : state}`.trim();
  if (state === 'ok') {
    pill.textContent = `${pill.dataset.base} ✓`;
  } else if (state === 'failed' || state === 'error') {
    pill.textContent = `${pill.dataset.base} ✗`;
  } else if (state.startsWith('verifying')) {
    pill.textContent = `${pill.dataset.base} ↻`;
  } else {
    pill.textContent = pill.dataset.base;
  }
}

async function loadLights() {
  const response = await fetch('/api/lights');
  const lights = await response.json();
  groupsEl.innerHTML = '';
  Object.keys(pillMap).forEach(key => delete pillMap[key]);
  buttonMap.length = 0;

  subtitleEl.textContent = `${lights.length} light${lights.length === 1 ? '' : 's'} cached`;

  const grouped = {};
  for (const light of lights) {
    const key = light.label || light.ip;
    if (!grouped[key]) grouped[key] = [];
    grouped[key].push(light);
  }

  groupsEl.appendChild(buildCard('All lights', lights, null));
  for (const key of Object.keys(grouped).sort()) {
    groupsEl.appendChild(buildCard(key, grouped[key], key));
  }
}

function buildCard(title, lights, label) {
  const card = document.createElement('div');
  card.className = 'card';

  const top = document.createElement('div');
  top.className = 'card-top';

  const copy = document.createElement('div');
  copy.innerHTML = `<div class="title">${title}</div><div class="count">${lights.length} bulb${lights.length === 1 ? '' : 's'}</div>`;

  const actions = document.createElement('div');
  actions.className = 'actions';

  const onBtn = document.createElement('button');
  onBtn.className = 'btn-on';
  onBtn.textContent = 'ON';
  onBtn.onclick = () => sendCommand('on', label);

  const offBtn = document.createElement('button');
  offBtn.className = 'btn-off';
  offBtn.textContent = 'OFF';
  offBtn.onclick = () => sendCommand('off', label);

  actions.appendChild(onBtn);
  actions.appendChild(offBtn);
  buttonMap.push(onBtn, offBtn);

  top.appendChild(copy);
  top.appendChild(actions);
  card.appendChild(top);

  const pills = document.createElement('div');
  pills.className = 'pills';
  for (const light of lights) {
    const pill = document.createElement('div');
    pill.className = 'pill';
    pill.dataset.base = light.ip;
    pill.textContent = light.ip;
    pillMap[light.ip] = pill;
    pills.appendChild(pill);
  }
  card.appendChild(pills);

  return card;
}

async function sendCommand(action, label) {
  const url = label ? `/api/lights/${action}?label=${encodeURIComponent(label)}` : `/api/lights/${action}`;
  await fetch(url, { method: 'POST' });
}

async function rediscover() {
  setButtonsDisabled(true);
  setStatus('Re-discovering lights...');
  try {
    const response = await fetch('/api/discover', { method: 'POST' });
    const data = await response.json();
    await loadLights();
    toast(`Discovered ${data.count} lights`);
    setStatus('Idle');
  } catch (error) {
    toast('Discovery failed');
    setStatus('Discovery failed');
  } finally {
    setButtonsDisabled(false);
  }
}

function connectEvents() {
  const stream = new EventSource('/api/stream');

  stream.addEventListener('start', (event) => {
    const data = JSON.parse(event.data);
    currentTotal = data.total;
    currentConfirmed = 0;
    setButtonsDisabled(true);
    for (const ip of Object.keys(pillMap)) setPillState(ip, 'idle');
    setStatus(`Syncing ${data.action.toUpperCase()}...`, currentConfirmed, currentTotal);
  });

  stream.addEventListener('bulb', (event) => {
    const data = JSON.parse(event.data);
    setPillState(data.ip, data.status);
    if (data.status === 'ok') {
      currentConfirmed += 1;
      setStatus(statusTextEl.textContent, currentConfirmed, currentTotal);
    }
  });

  stream.addEventListener('done', (event) => {
    const data = JSON.parse(event.data);
    setButtonsDisabled(false);
    if (data.aborted) {
      setStatus('Previous command aborted');
      return;
    }
    if (data.ok) {
      setStatus(`Completed ${data.action.toUpperCase()}`, data.confirmed, data.total);
      toast(`${data.confirmed}/${data.total} lights confirmed`);
    } else {
      setStatus(`Completed with failures`, data.confirmed, data.total);
      toast(`${data.failed.length} lights failed`);
      for (const item of data.failed) setPillState(item.ip, 'failed');
    }
  });

  stream.addEventListener('discover', (event) => {
    const data = JSON.parse(event.data);
    toast(`Discovered ${data.count} lights`);
  });
}

discoverBtn.onclick = rediscover;
loadLights();
connectEvents();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/lights":
            self.send_json(HTTPStatus.OK, current_cache())
            return

        if parsed.path == "/api/stream":
            self.handle_sse()
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        label = params.get("label", [None])[0]

        if parsed.path == "/api/lights/on":
            enqueue("on", label)
            self.send_json(HTTPStatus.ACCEPTED, {"queued": True, "action": "on"})
            return

        if parsed.path == "/api/lights/off":
            enqueue("off", label)
            self.send_json(HTTPStatus.ACCEPTED, {"queued": True, "action": "off"})
            return

        if parsed.path == "/api/discover":
            self.handle_discover()
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def handle_discover(self) -> None:
        with _current_lock:
            if _current_job is not None:
                _current_job.abort.set()

        with _run_lock:
            lights = discover_and_cache()

        push_sse("discover", {"count": len(lights)})
        self.send_json(HTTPStatus.OK, {"ok": True, "count": len(lights)})

    def handle_sse(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        client_queue: queue.Queue[str] = queue.Queue(maxsize=128)
        closed = threading.Event()
        with _sse_clients_lock:
            _sse_clients.append((client_queue, closed))

        try:
            while not closed.is_set():
                try:
                    message = client_queue.get(timeout=15)
                    self.wfile.write(message.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            closed.set()
            with _sse_clients_lock:
                try:
                    _sse_clients.remove((client_queue, closed))
                except ValueError:
                    pass

    def send_json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> int:
    LIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not LIGHTS_FILE.exists():
        write_lights_cache([])

    worker = threading.Thread(target=worker_loop, daemon=True)
    worker.start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"LIFX Sync running at http://localhost:{PORT}")
    print(f"Using lights cache: {LIGHTS_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
