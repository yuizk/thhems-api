# HEMS API

[English](README.md) | [日本語](README.ja.md)

Flask API and Home Assistant integration for Toyota Home / DENSO Smart Airs
air conditioning and the associated HEMS controller web UI, including supported
door locks and shutters. This is an unofficial community project; compatibility
depends on the controller model, firmware, and web UI. It is not affiliated with
or supported by the equipment manufacturers.

## Architecture

Home Assistant → authenticated Flask API → single Selenium worker → controller
web UI. Read requests use background snapshots; control requests have priority
and finite deadlines. Chrome and a matching ChromeDriver are installed during
the Docker build. The image targets Linux amd64.

## Start locally

```sh
git clone https://github.com/yuizk/hems-api.git
cd hems-api
cp .env.example .env
# Edit .env: replace every placeholder with your own configuration.
docker compose config --quiet
docker compose build --no-cache
docker compose up -d
```

The controller URL, controller login, password, and separate READ / CONTROL API
keys are required. Generate two independent random keys (for example with
`openssl rand -hex 32`). `HEMS_DISABLE_LOCK` is optional and defaults to
`false`. Do not commit `.env`. Starting the API connects to the configured
controller for background reads.

The default port binding accepts connections only from the local host. For Home
Assistant on another host, deliberately bind to a trusted LAN interface or use a
TLS reverse proxy and firewall. Never expose the API directly to the internet;
API keys do not encrypt traffic. The READ key permits GET requests; the CONTROL
key permits both reads and physical controls. See [API reference](docs/api-reference.md).

The Compose configuration preserves the browser's current `seccomp:unconfined`
and Chrome `--no-sandbox` requirements. Run it on a trusted host/network.

## Home Assistant

Use `configuration_hems.yaml.example` as a package/configuration example. Replace
`hems-api.example.invalid` with your API host and configure `hems_api_key_read`
and `hems_api_key_control` in Home Assistant's `secrets.yaml`. The example requires
Home Assistant's MQTT integration and a broker. Merge it with your existing
configuration and check YAML before reloading.

The example refreshes state after an air-conditioning command even when the HTTP
request fails, and retains only validated snapshots. A timeout can mean the
device received the command: inspect state before retrying. Do not automatically
resend controls after an error.

### Homes without a door lock, or with lock control disabled

The API detects the floors, door lock, and shutter exposed by each controller
session. `GET /status` returns the detected `floors`; add or remove the
floor-specific blocks in `configuration_hems.yaml.example` to match that list.
Missing security devices are reported as `NOT_INSTALLED` and are not controlled.

Set `HEMS_DISABLE_LOCK=true` to keep an installed lock visible as `DISABLED`
while refusing lock commands. Remove the lock REST command, MQTT lock, and lock
automation blocks from the Home Assistant example when no lock is installed or
lock control is disabled. Shutter publishing is independent and continues when
the lock is unavailable.

## Development and image checks

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -v
python -m ruff check .
docker build --no-cache -t hems-api:smoke .
scripts/smoke-image.sh hems-api:smoke
```

The smoke runs real Chrome with `--network none` and dummy credentials; it never
logs into a HEMS controller. CI runs tests and lint, and a weekly workflow builds
current Stable Chrome and runs this smoke. Maintainer releases build once, smoke
that image, compare image IDs, and push the same image to a private GHCR package.
Users build locally; access to the maintainer's package is not required.

## Disclaimer and license

This software can operate air conditioning, door locks, and shutters. Incorrect
configuration, unsupported equipment, or failures can cause unintended physical
operation, loss of security, or damage. You are responsible for confirming device
compatibility, safe operation, access controls, and manual recovery. Keep the
manufacturer's controls available. No warranty or safety certification is provided.

Released under the [MIT License](LICENSE).
