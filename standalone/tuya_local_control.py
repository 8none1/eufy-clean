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

# DPS 124 methods to try when querying robot position
POSITION_QUERY_METHODS = [
    "getPos", "getCurPos", "workStatus", "getPosInfo",
    "getPosition", "curPos", "robotPos",
    "getWorkStatus", "queryPos", "getMap",
    "getCleanInfo", "currentStatus", "getRobotPos",
    "getChargePos", "getDockPos",
]

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

    last_dps15 = None
    last_snapshot = {}
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
        elapsed = time.time() - start
        poll_count += 1
        state = decoded.get("15", "?")

        # On any state change, dump every DPS value — position may be in an unknown DPS
        if state != last_dps15:
            print(f"[{elapsed:5.1f}s] DPS15 changed: {last_dps15} → {state}")
            print(f"  Full DPS dump:")
            for k, v in sorted(decoded.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else 999):
                print(f"    DPS {k:>4}: {v}")
            print()
            last_dps15 = state
            last_snapshot = dict(decoded)

        # On any individual DPS change, print just what changed
        else:
            changed = {k: v for k, v in decoded.items() if last_snapshot.get(k) != v}
            if changed:
                for k, v in sorted(changed.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else 999):
                    print(f"[{elapsed:5.1f}s] DPS {k:>4} changed: {last_snapshot.get(k)} → {v}")
                last_snapshot = dict(decoded)

        # Heartbeat
        if poll_count % 20 == 0:
            print(f"[{elapsed:5.1f}s] still polling... DPS15={state}")

        time.sleep(0.5)

    print(f"\nFinished after {duration}s.")
    print("Review the DPS dump above for any values that look like coordinates (large integers).")


def cmd_query_pos(device_name: str) -> None:
    """
    One-shot position query: full DPS dump + try all DPS 124 position methods.

    Run this WHILE the robot is parked at the bin (sent there via Eufy app).
    Any method that returns x/y coordinates is the bin position.

    Usage:
      python tuya_local_control.py query_pos upstairs
    """
    d = _device(device_name)
    d.set_socketPersistent(True)
    d.set_socketTimeout(5)

    print(f"=== Position query — {DEVICES[device_name]['name']} ===")
    print()

    # Full DPS dump
    print("--- Full DPS status ---")
    raw = d.status()
    if raw and "dps" in raw:
        decoded = _decode_dps(raw["dps"])
        for k, v in sorted(decoded.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else 999):
            print(f"  DPS {k:>4}: {v}")
    else:
        print("  (no response)")
    print()

    # Try each DPS 124 position method
    print("--- DPS 124 position method queries ---")
    for method in POSITION_QUERY_METHODS:
        try:
            cmd = _encode_cmd(method)
            resp = d.set_value(int(DPS_COMMAND_TRANS), cmd)
            if resp and "dps" in resp:
                decoded_resp = _decode_dps(resp["dps"])
                dps124 = decoded_resp.get("124")
                tag = ""
                if isinstance(dps124, dict):
                    data = dps124.get("data", {})
                    if isinstance(data, dict) and any(
                        isinstance(data.get(kk), (int, float))
                        for kk in data
                        if any(c in kk.lower() for c in ("x", "y", "pos", "coord"))
                    ):
                        tag = "  *** COORDS? ***"
                print(f"  {method:<22} → {dps124}{tag}")
            else:
                print(f"  {method:<22} → (no DPS response)")
        except Exception as e:
            print(f"  {method:<22} → ERROR: {e}")
        time.sleep(0.8)

    print()
    print("If any method returned x/y values, add them to DEVICES as bin_x/bin_y.")


def cmd_find_bin_pos(device_name: str, duration: int = 300) -> None:
    """
    Comprehensive monitor: dump ALL DPS while robot navigates to the bin.

    Combines: receive() for pushes + status poll every 5s + DPS 124 queries.

    Procedure:
      1. Run: python tuya_local_control.py find_bin_pos upstairs
      2. In the Eufy app, tap the map → send robot to the bin
      3. When robot arrives at bin, press Ctrl+C (or let it time out)
      4. Check output for x/y values — run query_pos for a targeted snapshot
    """
    d = _device(device_name)
    d.set_socketPersistent(True)
    d.set_socketTimeout(2)
    d.set_socketRetryLimit(0)

    print(f"=== Bin position finder — {DEVICES[device_name]['name']} ===")
    print(f"Duration: {duration}s  |  Ctrl+C to stop early")
    print()

    # Initial full snapshot
    try:
        raw = d.status()
        last_snapshot = _decode_dps(raw.get("dps", {})) if raw else {}
    except Exception:
        last_snapshot = {}

    if last_snapshot:
        print("Initial state:")
        for k, v in sorted(last_snapshot.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else 999):
            print(f"  DPS {k:>4}: {v}")
        print()

    print("READY — use Eufy app to send robot to the bin now")
    print()

    found_candidates: list[tuple[str, object]] = []
    start = time.time()
    last_poll = time.time()
    last_query = time.time()
    query_idx = 0

    def _note_coords(source: str, decoded: dict) -> None:
        for k, v in decoded.items():
            target = v.get("data", v) if isinstance(v, dict) else {}
            if isinstance(target, dict):
                coord_hits = [kk for kk in target if isinstance(target[kk], (int, float))
                              and any(c in kk.lower() for c in ("x", "y", "pos", "coord"))]
                if coord_hits:
                    print(f"\n  *** POSSIBLE COORDS [{source}] DPS {k}: {target} ***")
                    found_candidates.append((source, target))

    try:
        while time.time() - start < duration:
            elapsed = time.time() - start

            # Listen for pushes
            try:
                msg = d.receive()
            except Exception:
                msg = None
                time.sleep(0.2)

            if msg and "dps" in msg:
                decoded = _decode_dps(msg["dps"])
                changed = {k: v for k, v in decoded.items() if last_snapshot.get(k) != v}
                if changed:
                    print(f"\n[{elapsed:6.1f}s] DPS push — changed: {list(changed)}")
                    for k, v in sorted(decoded.items(),
                                       key=lambda x: int(x[0]) if str(x[0]).isdigit() else 999):
                        marker = " ◄" if k in changed else ""
                        print(f"  DPS {k:>4}: {v}{marker}")
                    last_snapshot = {**last_snapshot, **decoded}
                    _note_coords(f"push@{elapsed:.0f}s", decoded)

            # Full status poll every 5s
            if time.time() - last_poll >= 5:
                last_poll = time.time()
                try:
                    raw = d.status()
                    if raw and "dps" in raw:
                        decoded = _decode_dps(raw["dps"])
                        changed = {k: v for k, v in decoded.items() if last_snapshot.get(k) != v}
                        if changed:
                            print(f"\n[{elapsed:6.1f}s] Poll changes:")
                            for k, v in sorted(changed.items(),
                                               key=lambda x: int(x[0]) if str(x[0]).isdigit() else 999):
                                print(f"  DPS {k:>4}: {last_snapshot.get(k)!r} → {v!r}")
                            last_snapshot = {**last_snapshot, **decoded}
                            _note_coords(f"poll@{elapsed:.0f}s", decoded)
                        else:
                            state = decoded.get("15", "?")
                            print(f"[{elapsed:6.1f}s] poll OK  DPS15={state!r}    ", end="\r", flush=True)
                except Exception as e:
                    print(f"[{elapsed:6.1f}s] poll error: {e}")

            # DPS 124 position queries every 20s
            if time.time() - last_query >= 20:
                last_query = time.time()
                method = POSITION_QUERY_METHODS[query_idx % len(POSITION_QUERY_METHODS)]
                query_idx += 1
                try:
                    cmd = _encode_cmd(method)
                    print(f"\n[{elapsed:6.1f}s] → DPS 124 query: {method!r}")
                    resp = d.set_value(int(DPS_COMMAND_TRANS), cmd)
                    if resp and "dps" in resp:
                        decoded_resp = _decode_dps(resp["dps"])
                        dps124 = decoded_resp.get("124")
                        print(f"  ← {dps124}")
                        if isinstance(dps124, dict):
                            _note_coords(f"query-{method}", {"124": dps124})
                    else:
                        print(f"  ← (no response)")
                except Exception as e:
                    print(f"  [query error: {e}]")

    except KeyboardInterrupt:
        elapsed = time.time() - start
        print(f"\n[{elapsed:.0f}s] Ctrl+C — stopping.")

    print()
    elapsed = time.time() - start
    print(f"=== SUMMARY ({elapsed:.0f}s elapsed) ===")
    if found_candidates:
        print(f"Coordinate candidates ({len(found_candidates)}):")
        for source, data in found_candidates:
            print(f"  [{source}]  {data}")
        print()
        print("Next: run 'query_pos upstairs' with robot parked at the bin for a clean snapshot.")
    else:
        print("No explicit x/y values found in DPS 124 responses.")
        print()
        print("Try: run 'query_pos upstairs' while robot is AT the bin — different connection")
        print("     type may expose more DPS methods.")
        print()
        print("Also check the DPS dump above for large integers in any DPS — those could")
        print("be coordinates even if not labeled as x/y.")


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
  tuya_local_control.py query_pos      <device>
  tuya_local_control.py find_bin_pos   <device> [duration_seconds]
  tuya_local_control.py test_goto      <device>
  tuya_local_control.py monitor        <device> [duration_seconds]

device: upstairs | downstairs

To discover bin coordinates (new approach — goto goes via cloud, not local TCP):

  Option A — query_pos (run when robot is already at the bin):
    1. Use Eufy app to send robot to the bin
    2. When it arrives, run: query_pos <device>
    3. This dumps all DPS + tries 15 DPS 124 position methods
    4. Look for x/y values in the output

  Option B — find_bin_pos (run while robot is navigating):
    1. Run: find_bin_pos <device>
    2. Use Eufy app to send robot to the bin
    3. Script monitors all DPS pushes + polls + queries for the full duration
    4. Ctrl+C when robot arrives; check output for coordinates

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
    elif cmd == "query_pos":
        cmd_query_pos(device)
    elif cmd == "find_bin_pos":
        duration = int(args[2]) if len(args) > 2 else 300
        cmd_find_bin_pos(device, duration)
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
