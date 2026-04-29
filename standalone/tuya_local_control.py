#!/usr/bin/env python3
"""
Eufy X8 local Tuya protocol control — goto and position monitoring.

Communicates via Tuya local protocol v3.3 on port 6668.
Commands are sent via DPS 124 (command_trans) as base64-encoded JSON:
  {"method": "<name>", "data": {...}, "timestamp": <ms>}

Usage:
    # Monitor all DPS messages (run while robot is active to find coordinates):
    python tuya_local_control.py monitor

    # Send robot to specific coordinates:
    python tuya_local_control.py goto <device> <x> <y>

    # Start auto clean:
    python tuya_local_control.py start <device>

    # Return to base:
    python tuya_local_control.py home <device>

    # Print device status:
    python tuya_local_control.py status <device>

device:  "upstairs" or "downstairs"
x, y:    integer coordinates in the robot's internal SLAM coordinate space
         (use 'find_coords' to discover these — see below)

    # Intercept bin coordinates from the Eufy app (one-time setup):
    python tuya_local_control.py intercept_pos <device>
    # Polls DPS 124 while you tap the map in the Eufy app to send
    # the robot to the bin.  Prints the x,y to hardcode for automation.
"""
from __future__ import annotations

import base64
import json
import sys
import time

import tinytuya

# ---------------------------------------------------------------------------
# Device registry
# ---------------------------------------------------------------------------
DEVICES = {
    "upstairs": {
        "name": "Upstairs Clean",
        "id": "bf3b83d14f132d51b0gzpk",
        "ip": "192.168.42.144",
        "key": "get{P<x#OI<qUenE",
        "version": 3.3,
        "map_id": 202,  # from DPS 125 defaultID
    },
    "downstairs": {
        "name": "Downstairs",
        "id": "bfc291ad10e8247fefwnk2",
        "ip": "192.168.42.17",
        "key": "Sz~5?p~Gsjg$.s$$",
        "version": 3.3,
        "map_id": None,  # map cleared by factory reset
    },
}

# DPS numbers (Eufy X8 Tuya v3.3 protocol)
DPS_POWER         = "1"    # bool: power on/off
DPS_ACTIVATE      = "2"    # bool: start/stop
DPS_WORK_MODE     = "5"    # str: "auto", "Nosweep", "Edge", "Spot", etc.
DPS_WORK_STATUS   = "15"   # str: "Sleeping", "Running", "Charging", etc.
DPS_RETURN_HOME   = "101"  # bool: True = return to dock
DPS_CLEAN_SPEED   = "102"  # str: "Quiet", "Standard", "Turbo", "Max"
DPS_LOCATE        = "103"  # bool: toggle find-robot beeper
DPS_BATTERY       = "104"  # int: battery %
DPS_MAP_DATA      = "121"  # str: raw map data (when streaming)
DPS_COMMAND_TRANS = "124"  # str: b64 JSON command transport
DPS_MAP_INFO      = "125"  # str: b64 JSON map metadata


def _device(name: str) -> tinytuya.Device:
    info = DEVICES[name]
    d = tinytuya.Device(
        dev_id=info["id"],
        address=info["ip"],
        local_key=info["key"],
        version=info["version"],
    )
    d.set_socketTimeout(8)
    return d


def _encode_cmd(method: str, data: dict | None = None) -> str:
    """Encode a DPS 124 command as base64 JSON."""
    payload: dict = {"method": method, "timestamp": round(time.time() * 1000)}
    if data:
        payload["data"] = data
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()


def _decode_dps(dps: dict) -> dict:
    """Decode any base64+JSON DPS values for display."""
    result = {}
    for k, v in dps.items():
        if isinstance(v, str) and len(v) > 8:
            try:
                decoded = base64.b64decode(v).decode("utf-8")
                result[k] = json.loads(decoded)
                continue
            except Exception:
                pass
        result[k] = v
    return result


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_status(device_name: str) -> None:
    """Print current device status."""
    d = _device(device_name)
    raw = d.status()
    if not raw or "dps" not in raw:
        print(f"No response from {device_name}")
        return
    decoded = _decode_dps(raw["dps"])
    print(f"=== {DEVICES[device_name]['name']} ===")
    for k, v in sorted(decoded.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999):
        print(f"  DPS {k:>4}: {v}")


def cmd_start(device_name: str) -> None:
    """Start auto clean."""
    d = _device(device_name)
    result = d.set_value(int(DPS_WORK_MODE), "auto")
    print(f"start auto: {result}")


def cmd_home(device_name: str) -> None:
    """Send robot back to dock."""
    d = _device(device_name)
    result = d.set_value(int(DPS_RETURN_HOME), True)
    print(f"return home: {result}")


def cmd_goto(device_name: str, x: int, y: int) -> None:
    """
    Send robot to specific map coordinates via DPS 124 'goto' method.

    IMPORTANT: The robot must be active (Locating/Running/Completed-after-clean)
    to accept this command.  Sending it while Sleeping returns "F" (Fail).

    Best use: trigger immediately after robot transitions to "Completed" state
    (i.e., it just docked after a cleaning run — the map is still loaded).

    Coordinate discovery:
      Option A — Eufy app: use the app to send a goto command while running
                 `monitor` — the DPS 124 echo should include x, y.
      Option B — Tuya IoT developer API: register at iot.tuya.com, create a
                 project, link the device, then use
                 GET /v1.0/users/sweepers/file/{device_id}/realtime-map to
                 download the 48-byte-header map binary.  The header contains
                 the dock (pile) position and origin coordinates.

    Coordinates are sint32 in the robot's SLAM frame (probably 50mm units).
    """
    info = DEVICES[device_name]
    if info["map_id"] is None:
        print(f"ERROR: {device_name} has no stored map (cleared by factory reset)")
        return

    data = {"mapId": info["map_id"], "x": x, "y": y}
    cmd = _encode_cmd("goto", data)
    print(f"Sending goto: mapId={info['map_id']} x={x} y={y}")

    d = _device(device_name)
    d.set_socketPersistent(True)
    d.set_socketTimeout(5)

    response = d.set_value(int(DPS_COMMAND_TRANS), cmd)
    if response and "dps" in response:
        decoded = _decode_dps(response["dps"])
        dps124 = decoded.get("124")
        if isinstance(dps124, dict):
            result = dps124.get("result", "?")
            print(f"  Result: {result}")
            if result == "F":
                print("  'F' = Failed.  Is the robot active (not Sleeping)?")
                print("  Status:", _decode_dps(d.status().get("dps", {})).get("15"))
            elif result == "O":
                print("  'O' = OK! Robot navigating to target.")
        else:
            print(f"  Response: {response}")
    else:
        print(f"  No response (sent OK or robot ignored)")

    # Confirm with a status poll
    import time; time.sleep(2)
    status = _decode_dps(d.status().get("dps", {}))
    print(f"  DPS15 (work status): {status.get('15')}")


def cmd_test_goto(device_name: str) -> None:
    """
    Verify the goto command is working.

    Confirmed: method='goto' is recognised by the robot (returns DPS 124 response).
    result='F' while Sleeping is normal — the robot needs to be active.
    result='O' means successful navigation to target.

    This test sends goto(0,0) and reports what the robot replies.
    """
    info = DEVICES[device_name]
    d = _device(device_name)
    d.set_socketPersistent(True)
    d.set_socketTimeout(5)

    # Check current state
    status = d.status()
    dps = _decode_dps(status.get("dps", {}))
    print(f"Current DPS15 (work status): {dps.get('15')}")

    data = {"mapId": info.get("map_id", 202), "x": 0, "y": 0}
    cmd = _encode_cmd("goto", data)
    response = d.set_value(int(DPS_COMMAND_TRANS), cmd)

    if response and "dps" in response:
        decoded = _decode_dps(response["dps"])
        dps124 = decoded.get("124")
        if isinstance(dps124, dict):
            result = dps124.get("result", "?")
            print(f"goto(0,0): result='{result}'")
            if result == "F":
                print("  → 'F' = Failed (expected if robot is Sleeping — goto needs active state)")
            elif result == "O":
                print("  → 'O' = Accepted! Robot will navigate.")
        else:
            print(f"goto(0,0): response={decoded}")
    else:
        print(f"goto(0,0): no DPS response → {response}")


def cmd_goto_when_active(device_name: str, x: int, y: int, timeout: int = 300) -> None:
    """
    Wait until the robot is in an active/completed state, then send goto.

    Use this to test goto with real coordinates when the robot has just
    finished cleaning (map loaded, DPS15 = "Completed" or "Running").

    Also useful for triggering from a HA automation:
      trigger: state change to "Completed" → call this script
    """
    import sys
    info = DEVICES[device_name]
    if info["map_id"] is None:
        print(f"ERROR: {device_name} has no stored map")
        return

    d = _device(device_name)
    d.set_socketPersistent(True)
    d.set_socketTimeout(5)

    ACTIVE_STATES = {"Running", "Cleaning", "Locating", "Completed", "completed"}

    print(f"Waiting up to {timeout}s for robot to be active...")
    start = time.time()
    while time.time() - start < timeout:
        status = d.status()
        dps = _decode_dps(status.get("dps", {}))
        state = dps.get("15", "")
        print(f"  [{time.time()-start:.0f}s] DPS15={state}")
        if state in ACTIVE_STATES:
            print(f"Robot is active ({state}). Sending goto({x},{y})...")
            data = {"mapId": info["map_id"], "x": x, "y": y}
            cmd = _encode_cmd("goto", data)
            response = d.set_value(int(DPS_COMMAND_TRANS), cmd)
            if response and "dps" in response:
                decoded = _decode_dps(response["dps"])
                dps124 = decoded.get("124")
                if isinstance(dps124, dict):
                    print(f"  goto result: {dps124.get('result')}")
                else:
                    print(f"  response: {decoded}")
            return
        time.sleep(5)

    print(f"Timed out waiting for active state.")


def cmd_intercept_pos(device_name: str, duration: int = 90) -> None:
    """
    Intercept the goto coordinates sent by the Eufy app.

    The Eufy app's tap-on-map goto encodes the target x,y in DPS 124.
    This command polls device status in a tight loop and prints the
    coordinates whenever DPS 124 contains a goto with x,y data.

    Workflow:
      1. Make sure the robot is active (just finished a clean, or start one)
      2. Run this command
      3. In the Eufy app, tap the map where you want the robot to go (e.g. near the bin)
      4. This script prints the x,y coordinates
      5. Hardcode them in DEVICES or pass to 'goto_active'

    This works because d.status() is a direct poll — unlike d.receive() which
    only sees pushes to our own connection, status() reads whatever DPS 124 the
    robot last processed (including commands from the app).
    """
    d = _device(device_name)
    d.set_socketPersistent(True)
    d.set_socketTimeout(5)

    print(f"Polling {DEVICES[device_name]['name']} for {duration}s ...")
    print(">>> In the Eufy app, tap the map to send the robot to your target location <<<")
    print()

    last_dps124 = None
    start = time.time()
    poll_count = 0

    while time.time() - start < duration:
        try:
            raw = d.status()
        except Exception as e:
            print(f"  [poll error: {e}]")
            time.sleep(1)
            continue

        if not raw or "dps" not in raw:
            time.sleep(1)
            continue

        decoded = _decode_dps(raw["dps"])
        dps124 = decoded.get("124")

        poll_count += 1
        elapsed = time.time() - start

        # Print any change in DPS 124
        if dps124 != last_dps124 and dps124 is not None:
            last_dps124 = dps124
            print(f"[{elapsed:5.1f}s] DPS 124 changed: {dps124}")

            if isinstance(dps124, dict):
                method = dps124.get("method", "")
                data = dps124.get("data", {})
                if method == "goto" and ("x" in data or "posX" in data):
                    x = data.get("x", data.get("posX"))
                    y = data.get("y", data.get("posY"))
                    print(f"\n*** TARGET COORDINATES: x={x}, y={y} ***")
                    print(f"    mapId: {data.get('mapId')}")
                    print(f"\nAdd to DEVICES['{device_name}']:")
                    print(f"    \"bin_x\": {x},")
                    print(f"    \"bin_y\": {y},")
                    return
                elif data:
                    # Print any coordinates we see regardless of method name
                    for key in ("x", "y", "posX", "posY", "pileX", "pileY"):
                        if key in data:
                            print(f"  {key} = {data[key]}")

        # Print a heartbeat every 10s so we know it's alive
        if poll_count % 20 == 0:
            state = decoded.get("15", "?")
            print(f"[{elapsed:5.1f}s] still polling... DPS15={state}, DPS124={type(dps124).__name__}")

        time.sleep(0.5)

    print(f"\nFinished after {duration}s. No goto coordinates intercepted.")
    print("Make sure the robot is active (DPS15 not Sleeping) before using the app to send a goto.")


def cmd_monitor(device_name: str, duration: int = 120) -> None:
    """
    Monitor all DPS updates from the robot for <duration> seconds.

    Run this while the robot is active (cleaning or being controlled via the
    Eufy app) to capture position data and command formats.

    DPS 124 updates will show the decoded command/response including coordinates.
    """
    d = _device(device_name)
    d.set_socketPersistent(True)
    d.set_socketTimeout(2)
    d.set_socketRetryLimit(0)

    print(f"Monitoring {DEVICES[device_name]['name']} for {duration}s ...")
    print("(Use Eufy app to send goto command — watch DPS 124 for coordinates)")
    print()

    last_dps15 = None
    last_heartbeat = time.time()

    start = time.time()
    while time.time() - start < duration:
        elapsed = time.time() - start

        try:
            msg = d.receive()
        except Exception:
            msg = None

        if msg and "dps" in msg:
            decoded = _decode_dps(msg["dps"])
            interesting = {k: v for k, v in decoded.items()
                           if k in ("15", "124", "125", "121", "142", "5")}
            if interesting:
                print(f"[{elapsed:6.1f}s] DPS push:")
                for k, v in sorted(interesting.items(), key=lambda x: int(x[0])):
                    print(f"         {k}: {v}")

                # When DPS 15 changes, poll full status immediately to catch
                # any DPS 124 that the robot might not have pushed
                new_dps15 = decoded.get("15")
                if new_dps15 and new_dps15 != last_dps15:
                    last_dps15 = new_dps15
                    # Small delay then full poll
                    time.sleep(0.5)
                    full_status = d.status()
                    if full_status and "dps" in full_status:
                        full = _decode_dps(full_status["dps"])
                        dps124 = full.get("124")
                        if dps124:
                            print(f"         [poll] DPS 124: {dps124}")
                            if isinstance(dps124, dict):
                                inner = dps124.get("data", {})
                                if any(k in inner for k in ("x", "y", "posX", "posY")):
                                    print(f"  *** COORDINATES FOUND: {inner} ***")

                print()

        # Heartbeat every 10s
        if time.time() - last_heartbeat >= 10:
            d.heartbeat()
            last_heartbeat = time.time()

    print("Monitor complete.")


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------

USAGE = """
Usage:
  tuya_local_control.py status         <device>
  tuya_local_control.py start          <device>
  tuya_local_control.py home           <device>
  tuya_local_control.py goto           <device> <x> <y>
  tuya_local_control.py goto_active    <device> <x> <y> [timeout_s]
  tuya_local_control.py intercept_pos  <device> [duration_seconds]
  tuya_local_control.py test_goto      <device>
  tuya_local_control.py monitor        <device> [duration_seconds]

device: upstairs | downstairs

To discover bin coordinates (one-time setup):
  1. Start a clean: 'start <device>'  (robot must be active, not sleeping)
  2. Run:          'intercept_pos <device>'
  3. In the Eufy app, tap the map to send the robot to the bin location
  4. Script prints the x,y coordinates — hardcode them in DEVICES or use goto_active

Normal use (after coordinates are known):
  python tuya_local_control.py goto_active <device> <x> <y>
"""


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(USAGE)
        return

    cmd = args[0].lower()
    device = args[1].lower() if len(args) > 1 else "upstairs"

    if device not in DEVICES:
        print(f"Unknown device '{device}'. Choose: {list(DEVICES)}")
        sys.exit(1)

    if cmd == "status":
        cmd_status(device)
    elif cmd == "intercept_pos":
        duration = int(args[2]) if len(args) > 2 else 90
        cmd_intercept_pos(device, duration)
    elif cmd == "test_goto":
        cmd_test_goto(device)
    elif cmd == "goto_active":
        if len(args) < 4:
            print("goto_active requires x and y")
            sys.exit(1)
        timeout = int(args[4]) if len(args) > 4 else 300
        cmd_goto_when_active(device, int(args[2]), int(args[3]), timeout)
    elif cmd == "start":
        cmd_start(device)
    elif cmd == "home":
        cmd_home(device)
    elif cmd == "goto":
        if len(args) < 4:
            print("goto requires x and y: goto <device> <x> <y>")
            sys.exit(1)
        cmd_goto(device, int(args[2]), int(args[3]))
    elif cmd == "monitor":
        duration = int(args[2]) if len(args) > 2 else 120
        cmd_monitor(device, duration)
    else:
        print(f"Unknown command '{cmd}'")
        print(USAGE)


if __name__ == "__main__":
    main()
