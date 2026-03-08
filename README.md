# LIFX Sync Add-on

Home Assistant add-on for synchronized LIFX LAN control with a hardened burst-and-verify engine.

## Repository Layout

```text
.
├── repository.yaml
├── README.md
└── lifx-sync/
    ├── config.yaml
    ├── Dockerfile
    ├── run.sh
    ├── discover.py
    ├── server.py
    ├── sync_core.py
    ├── sync_on.py
    ├── sync_off.py
    └── lights.json
```

## What The Add-on Does

- Runs a web UI and JSON API on port `5050`
- Discovers LIFX lights over the LAN protocol
- Stores the light cache in `/data/lights.json` so it survives restarts and upgrades
- Uses a synchronized multi-packet burst plus verify-and-retry flow to keep bulbs in sync
- Exposes simple endpoints Home Assistant automations can call from Lutron Pico triggers

## Install In Home Assistant

1. Put this repository on GitHub.
2. In Home Assistant, open `Settings -> Add-ons -> Add-on Store`.
3. Open the menu in the top-right corner and choose `Repositories`.
4. Paste your GitHub repository URL.
5. Install the `LIFX Sync` add-on and start it.

The add-on uses `host_network: true` so UDP broadcast discovery can reach your bulbs directly.

## Home Assistant Configuration

Add this to `configuration.yaml`:

```yaml
rest_command:
  lifx_on:
    url: "http://localhost:5050/api/lights/on"
    method: POST

  lifx_off:
    url: "http://localhost:5050/api/lights/off"
    method: POST
```

Restart Home Assistant after adding the `rest_command` block.

## Lutron Pico Automation

With the built-in `lutron_caseta` integration on the standard Smart Bridge, Pico remotes appear as device triggers. Create two automations:

1. `Button on press` -> call `rest_command.lifx_on`
2. `Button off press` -> call `rest_command.lifx_off`

If you want only a subset of lights, call the filtered endpoints instead:

```yaml
rest_command:
  lifx_downlights_on:
    url: "http://localhost:5050/api/lights/on?label=Downlight"
    method: POST

  lifx_downlights_off:
    url: "http://localhost:5050/api/lights/off?label=Downlight"
    method: POST
```

## API Routes

- `GET /` - web UI
- `GET /api/lights` - current cached lights
- `GET /api/stream` - live progress events
- `POST /api/lights/on` - sync all cached lights on
- `POST /api/lights/off` - sync all cached lights off
- `POST /api/lights/on?label=Downlight` - sync a label group on
- `POST /api/lights/off?label=Downlight` - sync a label group off
- `POST /api/discover` - re-scan the network and rewrite the cache

## Local Development

From the `lifx-sync/` directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install lifxlan
python3 discover.py
python3 server.py
```

For local testing, `server.py` defaults to `lifx-sync/lights.json`. Inside Home Assistant, `run.sh` exports `LIGHTS_FILE=/data/lights.json`.
