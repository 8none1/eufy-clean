# eufy-clean — Claude Context

## What this repo is

A fork of [GijsKruize/eufy-clean](https://github.com/GijsKruize/eufy-clean), rebranded as
**Eufy Robovac MQTT (8none1)**. It is a Home Assistant custom component that controls Eufy
robot vacuums via MQTT/protobuf using Eufy's cloud ("novel") API. It replaces Will's older
[8none1/robovac](https://github.com/8none1/robovac) fork, which used the legacy Tuya local
protocol and had no mapping support.

**GitHub**: https://github.com/8none1/eufy-clean
**HA domain**: `robovac_mqtt`
**Installed via**: HACS → Custom Repositories → this repo URL

## Will's devices

| Device | Model code | Notes |
|--------|-----------|-------|
| X8 (Upstairs) | T2262EV | "EV" suffix = hardware revision. Newer Eufy app marks this as unsupported. |
| X8 (Downstairs) | T2262 | Plain model. Both are vacuum-only, no mop, basic charging dock. |

Both devices arrive via the **cloud fallback path** (AIOT API returns empty for these model
codes; cloud V1 API is used instead). This means initial DPS state is `{}` — battery level
and activity only update after the first MQTT push from the device.

## Protocol overview

**"Novel API"** — protobuf messages serialised and base64-encoded, sent as the value of
DPS 152 over MQTT to AWS IoT Core with mutual TLS.

### Auth flow
1. Email + password → `home-api.eufylife.com/v1/user/email/login` (Android User-Agent required — iOS UA returns 403)
2. access_token → `api.eufylife.com/v1/user/user_center_info` → user_center_token
3. user_center_token → `aiot-clean-api-pr.eufylife.com/app/devicemanage/get_user_mqtt_info` → per-user MQTT certificate + private key (mutual TLS)
4. Device list: try AIOT first → fall back to `api.eufylife.com/v1/device/v2` cloud list

**User-Agent must be**: `EufyHome-Android-3.1.3-753` (iOS UA causes 403)

### MQTT topics
- Subscribe: `cmd/eufy_home/{device_model}/{device_id}/res`
- Publish: `cmd/eufy_home/{device_model}/{device_id}/req`

### Message format (outgoing)
```json
{
  "head": {"client_id": "...", "cmd": 65537, "cmd_status": 2, ...},
  "payload": "{\"account_id\": \"...\", \"data\": {<dps>}, \"device_sn\": \"...\", \"protocol\": 2, \"t\": <ts>}"
}
```

### Message format (incoming)
```json
{"payload": {"data": {<dps key>: <base64 protobuf>}}}
```
`payload` may be a nested JSON string or a dict.

### Key DPS numbers
| Name | DPS | Direction | Notes |
|------|-----|-----------|-------|
| PLAY_PAUSE | 152 | cmd + status | ModeCtrlRequest / WorkStatus |
| WORK_STATUS | 153 | status | WorkStatus protobuf |
| WORK_MODE | 153 | status | same key |
| CLEANING_PARAMETERS | 154 | cmd | |
| BATTERY_LEVEL | 163 | status | plain int string |
| MAP_DATA | 165 | status | UniversalDataResponse or RoomParams |
| MAP_STREAM | 166 | status | |
| CLEANING_STATISTICS | 167 | status | CleanStatistics |
| ACCESSORIES_STATUS | 168 | cmd + status | ConsumableRequest / ConsumableResponse |
| MAP_MANAGE | 169 | cmd | |
| GO_HOME / STATION_STATUS | 173 | cmd + status | StationRequest (send) / StationResponse (recv) |
| UNSETTING | 176 | cmd | |
| ERROR_CODE | 177 | status | ErrorCode |
| SCENE_INFO | 180 | status | SceneResponse |

## Key files

| File | Purpose |
|------|---------|
| `custom_components/robovac_mqtt/const.py` | DeviceCapability enum, per-model capability sets, DPS_MAP, error codes |
| `custom_components/robovac_mqtt/models.py` | VacuumState, AccessoryState, CleaningPreferences dataclasses |
| `custom_components/robovac_mqtt/coordinator.py` | EufyCleanCoordinator — MQTT message handling, debounce, has_capability() |
| `custom_components/robovac_mqtt/api/client.py` | Paho MQTT client wrapper, mutual TLS, async event loop bridge |
| `custom_components/robovac_mqtt/api/cloud.py` | EufyLogin — auth, device discovery, AIOT → cloud fallback |
| `custom_components/robovac_mqtt/api/http.py` | Raw HTTP calls to Eufy cloud APIs |
| `custom_components/robovac_mqtt/api/parser.py` | update_state() — decodes incoming DPS → VacuumState |
| `custom_components/robovac_mqtt/api/commands.py` | build_command() — all outgoing DPS command builders |
| `custom_components/robovac_mqtt/utils.py` | encode/decode helpers for protobuf ↔ base64 |
| `custom_components/robovac_mqtt/proto/cloud/` | Generated protobuf Python files |
| `custom_components/robovac_mqtt/vacuum.py` | HA VacuumEntity |
| `custom_components/robovac_mqtt/sensor.py` | Sensors: battery, accessories, task status, etc. |
| `custom_components/robovac_mqtt/button.py` | Buttons: dock actions, accessory resets |
| `custom_components/robovac_mqtt/select.py` | Selects: room, scene, wash/dry config |
| `custom_components/robovac_mqtt/switch.py` | Switches: auto empty, auto wash |
| `custom_components/robovac_mqtt/number.py` | Numbers: wash frequency |
| `custom_components/robovac_mqtt/binary_sensor.py` | Binary sensors (charging etc.) |

## Capability system

Defined in `const.py`. Each device model maps to a frozenset of `DeviceCapability` flags.
Entities check `coordinator.has_capability(cap)` before being created.

```python
class DeviceCapability(str, Enum):
    MOP = "mop"            # Has mop cloth + water tank
    AUTO_EMPTY = "auto_empty"      # Dock auto-empties dustbin
    STATION_WASH = "station_wash"  # Dock washes the mop cloth
    SCENES = "scenes"      # Supports scene/custom cleaning presets
```

### What's gated on what

| Capability | Entities hidden when absent |
|-----------|----------------------------|
| MOP | Wash Frequency Mode (select), Wash Frequency Value (number), Cleaning Tray Remaining (sensor), Mopping Cloth Remaining (sensor), Reset Cleaning Tray (button), Reset Mopping Cloth (button) |
| AUTO_EMPTY | Auto Empty (switch), Auto Empty Mode (select), Empty Dust Bin (button), Dock Status (sensor) |
| STATION_WASH | Auto Wash (switch), Wash Mop (button), Dry Mop (button), Stop Dry Mop (button), Dry Duration (select), Water Level (sensor) |
| SCENES | Scene (select) |

The X8 (T2262 / T2262EV) has **no capabilities** — it is a plain vacuum with a basic
charging dock. Only universal entities show: battery, fan speed, room clean, activity,
task status, error, cleaning stats, accessory sensors (filter, brushes, sensors), map.

## Model variant note (T2262EV)

Eufy's newer iOS app ("Eufy", distinct from "Eufy Clean") reports T2262EV as unsupported
despite both devices being sold as X8. The EV suffix is a hardware revision. Both are
treated identically in the capability system. If firmware behaviour diverges (e.g. different
DPS layout on the goto command), T2262EV will need its own entry in `EUFY_CLEAN_DEVICES`
and the capability sets.

## Dock status behaviour

The X8's basic charging dock never sends `STATION_STATUS` (DPS 173). `dock_status` is
inferred from `WorkStatus` states 0/1/3 (sleep/standby/charging) and set to `"Idle"` when
no station message has arrived. Smart-dock models (AUTO_EMPTY) receive explicit
`StationResponse` messages that override this.

## Initial state problem

Cloud-fallback devices (including Will's X8s) are discovered via the V1 cloud API which
returns no DPS. `VacuumState` defaults apply (battery=0, activity="idle") until the first
MQTT push arrives — typically within 1–2 minutes of the robot reconnecting or changing
state. There is currently no "request current state" command sent on connect.

## Running tests

```bash
cd /home/will/source/eufy-clean
.venv/bin/python -m pytest tests/ -q
```

Tests use `pytest-asyncio` and `homeassistant` test stubs. The `.venv` was created with:
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
```

## Release / deployment

A GitHub Actions workflow (`.github/workflows/release.yaml`) auto-bumps the patch version
in `manifest.json` and creates a GitHub release on every push to `main`. HACS downloads
the release asset. After a push, wait ~1 minute for the release to appear, then update via
HACS in HA.

The `[skip ci]` commit message suffix is used by the version-bump commit to prevent an
infinite release loop.

## What has been done (in this fork)

- Fixed login 403: switched User-Agent to Android string
- Fixed device discovery: added cloud V1 fallback when AIOT returns empty
- Fixed MQTT connection: proper asyncio event + `call_soon_threadsafe` + TLS 1.2 minimum
- Fixed temp file cleanup for MQTT certificates
- Fixed `async_shutdown()` to cancel debounce timer on unload
- Fixed `ConfigEntryNotReady` when no coordinators initialise
- Added capability-based entity gating (all entity files)
- Added model capability sets in `const.py`
- Added `battery_level` property to vacuum entity
- Added dock status inference for basic docks
- Rebranded from upstream: name, codeowners, domain remains `robovac_mqtt`
- HACS metadata: topics, issues enabled, hacs.json

## What still needs doing

### Immediate goal: "go to location" after cleaning

The original reason for this fork. After the robot finishes cleaning, send it to a specific
(x, y) coordinate — e.g. next to the bin for easy emptying.

**Command to implement**: `EUFY_CLEAN_CONTROL.START_GOTO_CLEAN` (value 4) in
`EUFY_CLEAN_CONTROL` enum (already defined in `const.py`).

The protobuf for this is `ModeCtrlRequest` with method=4 and a `go_to` sub-message
containing the target coordinates. The exact protobuf field is in `control_pb2.py`.
Map coordinates come from `VacuumState.map_id` and are in the robot's internal coordinate
system (visible in the map data).

**HA automation plan**: Trigger when vacuum activity transitions to "docked" (after
cleaning, not after a manual return). Use `trigger_source` to distinguish: "app" / "robot"
triggered cleans should trigger the goto; "schedule" cleans should not (unless wanted).

### Other

- No "get current state" request on MQTT connect — state only updates on push
- `SCENES` capability not mapped to any model yet (scenes require map data; models TBD)
- T2262EV behaviour not validated against T2262 — might need its own capability entry
