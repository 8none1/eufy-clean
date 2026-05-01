#!/usr/bin/env python3
"""
Listen for DPS 125 (map info) pushed by the robot when the Eufy app connects.

Run this, then open the Eufy app and navigate to the downstairs robot's map.
The robot will push DPS 125 containing the map ID within a few seconds.

Usage:
    standalone/.venv/bin/python standalone/get_map_id.py [upstairs|downstairs]
"""
import base64
import json
import sys
import time

import tinytuya

DEVICES = {
    "upstairs": {
        "id": "bf3b83d14f132d51b0gzpk",
        "ip": "192.168.42.144",
        "key": "get{P<x#OI<qUenE",
    },
    "downstairs": {
        "id": "bfc291ad10e8247fefwnk2",
        "ip": "192.168.42.17",
        "key": "Sz~5?p~Gsjg$.s$$",
    },
}

device_name = sys.argv[1] if len(sys.argv) > 1 else "downstairs"
dev = DEVICES[device_name]

d = tinytuya.Device(dev_id=dev["id"], address=dev["ip"], local_key=dev["key"], version=3.3)
d.set_socketTimeout(3)
d.set_socketPersistent(True)
d.set_socketRetryLimit(0)


def decode(v):
    if isinstance(v, str) and len(v) > 8:
        try:
            return json.loads(base64.b64decode(v).decode())
        except Exception:
            pass
    return v


print(f"Listening for DPS pushes from {device_name} ({dev['ip']}) ...")
print(">>> Open the Eufy app and navigate to this robot's map <<<")
print()

deadline = time.time() + 180
found = False

while time.time() < deadline:
    try:
        msg = d.receive()
    except Exception:
        msg = None
        time.sleep(0.2)

    if msg and "dps" in msg:
        print(f"[{time.time():.0f}] DPS push received:")
        for k, v in sorted(msg["dps"].items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else 999):
            decoded = decode(v)
            print(f"  DPS {k:>4}: {decoded}")

        if "125" in msg["dps"]:
            info = decode(msg["dps"]["125"])
            print()
            print("=" * 50)
            print("  *** DPS 125 (map info) received ***")
            print(f"  {info}")
            if isinstance(info, dict):
                map_id = info.get("mapId") or info.get("defaultId") or info.get("id")
                if map_id:
                    print(f"  Map ID: {map_id}")
                    print()
                    print(f"  Update tuya_local_control.py DEVICES['{device_name}']['map_id'] = {map_id}")
            print("=" * 50)
            found = True
            break

        if "128" in msg["dps"]:
            print(f"\n  DPS 128 (map ID field): {decode(msg['dps']['128'])}")
        print()
    else:
        print(".", end="", flush=True)

if not found:
    print("\nTimed out after 180s — DPS 125 not received.")
    print("Try: start a cleaning run, wait for it to begin, then open the map view in the app.")
