#!/usr/bin/env python3
"""
Eufy AIOT platform probe.

Tests whether the T2262 robots are accessible via the Eufy AIOT cloud
(aiot-clean-api-pr.eufylife.com), which uses MQTT with TLS client certs
and protobuf-encoded DPS payloads.

Phases:
  1. REST auth flow → user_center_token + gtoken
  2. AIOT device list → confirm T2262 SNs appear
  3. MQTT credentials → TLS client cert + private key
  4. MQTT connection → subscribe to device topics
  5. Listen for DPS 165 (map data / goto_location) to extract coordinates

Usage:
    python get_aiot_info.py [--mqtt-listen <device_sn> [duration_s]]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
import tempfile
import time
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Credentials (same as get_local_keys.py)
# ---------------------------------------------------------------------------
EUFY_USERNAME = "eufy@whizzy.org"
EUFY_PASSWORD = "SecurityFTW!999"
EUFY_LOGIN_URL = "https://home-api.eufylife.com/v1/user/email/login"

OPENUDID = "abcdef1234567890"
USER_AGENT = "EufyHome-Android-3.1.3-753"

AIOT_BASE = "https://aiot-clean-api-pr.eufylife.com"
HOME_API_BASE = "https://api.eufylife.com"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def eufy_login() -> tuple[str, str]:
    """Login to Eufy cloud. Returns (access_token, user_id)."""
    print("1. Logging in to Eufy cloud...")
    r = requests.post(
        EUFY_LOGIN_URL,
        headers={
            "category": "Home",
            "Accept": "*/*",
            "openudid": OPENUDID,
            "Content-Type": "application/json",
            "clientType": "1",
            "User-Agent": USER_AGENT,
        },
        json={
            "email": EUFY_USERNAME,
            "password": EUFY_PASSWORD,
            "client_id": "eufyhome-app",
            "client_secret": "GQCpr9dSp3uQpsOMgJ4xQ",
        },
        timeout=15,
    )
    data = r.json()
    token = data.get("access_token")
    user_id = str(data.get("user_id", ""))
    if not token:
        raise RuntimeError(f"Eufy login failed: {data}")
    print(f"   access_token: {token[:20]}...  user_id: {user_id}")
    return token, user_id


def get_user_center_info(access_token: str) -> tuple[str, str]:
    """Get user_center_token and gtoken. Returns (user_center_token, gtoken)."""
    print("2. Getting user_center_info...")
    r = requests.get(
        f"{HOME_API_BASE}/v1/user/user_center_info",
        headers={
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "user-agent": USER_AGENT,
            "category": "Home",
            "token": access_token,
            "openudid": OPENUDID,
            "clienttype": "2",
        },
        timeout=15,
    )
    data = r.json()
    print(f"   Response: {json.dumps(data)[:300]}")
    result = data.get("user_info") or data.get("result") or data
    user_center_id = result.get("user_center_id", "")
    user_center_token = result.get("user_center_token", "")
    if not user_center_token:
        raise RuntimeError(f"No user_center_token in response: {data}")
    gtoken = hashlib.md5(user_center_id.encode()).hexdigest()
    print(f"   user_center_id: {user_center_id}")
    print(f"   gtoken (md5): {gtoken}")
    return user_center_token, gtoken


def _aiot_headers(user_center_token: str, gtoken: str) -> dict:
    return {
        "content-type": "application/json; charset=UTF-8",
        "user-agent": USER_AGENT,
        "openudid": OPENUDID,
        "os-version": "Android",
        "model-type": "PHONE",
        "app-name": "eufy_home",
        "x-auth-token": user_center_token,
        "gtoken": gtoken,
    }


def get_mqtt_credentials(user_center_token: str, gtoken: str) -> dict:
    """Get MQTT TLS client certificate and broker info."""
    print("3. Getting MQTT credentials from AIOT endpoint...")
    r = requests.post(
        f"{AIOT_BASE}/app/devicemanage/get_user_mqtt_info",
        headers=_aiot_headers(user_center_token, gtoken),
        json={},
        timeout=15,
    )
    data = r.json()
    print(f"   HTTP {r.status_code}")
    result = data.get("data") or data
    if isinstance(result, dict) and "endpoint_addr" in result:
        print(f"   endpoint_addr: {result['endpoint_addr']}")
        print(f"   thing_name: {result.get('thing_name', '?')}")
        cert_preview = (result.get("certificate_pem") or "")[:60]
        print(f"   certificate_pem: {cert_preview}...")
    else:
        print(f"   Full response: {json.dumps(data)[:500]}")
    return result


def get_aiot_devices(user_center_token: str, gtoken: str) -> list[dict]:
    """Get device list from AIOT endpoint."""
    print("4. Getting AIOT device list...")
    r = requests.post(
        f"{AIOT_BASE}/app/devicerelation/get_device_list",
        headers=_aiot_headers(user_center_token, gtoken),
        json={"attribute": 3},
        timeout=15,
    )
    data = r.json()
    print(f"   HTTP {r.status_code}")
    devices = []
    result = data.get("data") or {}
    raw_devices = result.get("devices") or []
    for entry in raw_devices:
        dev = entry.get("device", entry)
        sn = dev.get("device_sn", dev.get("id", "?"))
        name = dev.get("device_name", dev.get("name", "?"))
        model = dev.get("device_model", dev.get("product_code", "?"))
        dps = dev.get("dps", {})
        print(f"   Device: {name!r:30s}  sn={sn}  model={model}")
        print(f"     DPS keys: {sorted(dps.keys())}")
        devices.append(dev)
    if not devices:
        print(f"   No devices found. Full response: {json.dumps(data)[:800]}")
    return devices


def get_cloud_devices(access_token: str) -> list[dict]:
    """Fallback: get device list from general Eufy cloud API."""
    print("4b. Getting cloud device list (fallback)...")
    r = requests.get(
        f"{HOME_API_BASE}/v1/device/v2",
        headers={
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "user-agent": USER_AGENT,
            "category": "Home",
            "token": access_token,
            "openudid": OPENUDID,
            "clienttype": "2",
        },
        timeout=15,
    )
    data = r.json()
    print(f"   HTTP {r.status_code}: {json.dumps(data)[:600]}")
    return data.get("devices", [])


# ---------------------------------------------------------------------------
# MQTT listener
# ---------------------------------------------------------------------------

def mqtt_listen(mqtt_creds: dict, device_sn: str, device_model: str,
                duration: int = 120) -> None:
    """
    Connect to Eufy AIOT MQTT broker with TLS client cert and listen for
    all messages on the device's topics.  Decodes protobuf where possible.
    """
    import paho.mqtt.client as mqtt

    endpoint = mqtt_creds.get("endpoint_addr", "")
    if not endpoint:
        print("No endpoint_addr in MQTT credentials — cannot connect.")
        return

    host, _, port_str = endpoint.partition(":")
    port = int(port_str) if port_str else 8883
    thing_name = mqtt_creds.get("thing_name", "")
    cert_pem = mqtt_creds.get("certificate_pem", "")
    private_key = mqtt_creds.get("private_key", "")

    if not cert_pem or not private_key:
        print("Missing certificate_pem or private_key — cannot connect.")
        return

    # Write certs to temp files
    tmp_dir = tempfile.mkdtemp()
    cert_path = os.path.join(tmp_dir, "client.crt")
    key_path = os.path.join(tmp_dir, "client.key")
    with open(cert_path, "w") as f:
        f.write(cert_pem)
    with open(key_path, "w") as f:
        f.write(private_key)

    topics = [
        f"cmd/eufy_home/{device_model}/{device_sn}/res",
        f"smart/mb/in/{device_sn}",
        # Broader catch-all in case SN/model mapping is off
        f"cmd/eufy_home/+/{device_sn}/res",
    ]

    received: list[dict] = []

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            print(f"   MQTT connected to {host}:{port}")
            for t in topics:
                client.subscribe(t)
                print(f"   Subscribed: {t}")
        else:
            print(f"   MQTT connect failed: rc={rc}")

    def on_message(client, userdata, msg):
        elapsed = time.time() - userdata["start"]
        print(f"\n[{elapsed:5.1f}s] TOPIC: {msg.topic}")
        payload = msg.payload
        # Try JSON first
        try:
            decoded = json.loads(payload)
            print(f"  JSON: {json.dumps(decoded, indent=2)[:800]}")
            received.append({"topic": msg.topic, "json": decoded})
            return
        except Exception:
            pass
        # Try base64+JSON
        try:
            import base64
            decoded = json.loads(base64.b64decode(payload).decode())
            print(f"  b64+JSON: {json.dumps(decoded, indent=2)[:800]}")
            received.append({"topic": msg.topic, "b64json": decoded})
            return
        except Exception:
            pass
        # Try protobuf raw decode
        try:
            _print_raw_protobuf(payload, "  proto")
            received.append({"topic": msg.topic, "raw_len": len(payload)})
        except Exception:
            print(f"  RAW ({len(payload)} bytes): {payload[:80].hex()}")
            received.append({"topic": msg.topic, "raw_hex": payload[:80].hex()})

    def on_disconnect(client, userdata, rc, properties=None):
        print(f"   MQTT disconnected: rc={rc}")

    def on_subscribe(client, userdata, mid, granted_qos, properties=None):
        pass  # quiet

    client_id = (f"android-eufy_mqtt-eufy_android_{OPENUDID}_{thing_name}"
                 f"-{int(time.time()*1000)}")

    client = mqtt.Client(
        client_id=client_id,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        userdata={"start": time.time()},
    )
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.on_subscribe = on_subscribe
    client.username_pw_set(username=thing_name, password=None)
    client.tls_set(
        certfile=cert_path,
        keyfile=key_path,
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )

    print(f"\n5. Connecting to MQTT broker {host}:{port} ...")
    print(f"   client_id: {client_id[:60]}...")
    print(f"   Listening for {duration}s. Use the Eufy app to send a goto command.\n")

    try:
        client.connect(host, port, keepalive=60)
        client.loop_start()
        time.sleep(duration)
        client.loop_stop()
        client.disconnect()
    except Exception as e:
        print(f"   MQTT error: {e}")
    finally:
        os.unlink(cert_path)
        os.unlink(key_path)
        os.rmdir(tmp_dir)

    print(f"\nReceived {len(received)} MQTT message(s).")
    if received:
        print("Summary:")
        for m in received:
            print(f"  {m.get('topic')}: {list(m.keys())}")


def _print_raw_protobuf(data: bytes, prefix: str = "") -> None:
    """
    Best-effort raw protobuf field decoder.
    Prints field numbers and values without a schema.
    """
    from google.protobuf import descriptor_pb2
    # Manual wire-type decode
    i = 0
    fields = []
    while i < len(data):
        if i >= len(data):
            break
        b = data[i]
        field_num = b >> 3
        wire_type = b & 0x07
        i += 1
        if wire_type == 0:  # varint
            val, i = _read_varint(data, i)
            fields.append((field_num, "varint", val))
        elif wire_type == 2:  # length-delimited
            length, i = _read_varint(data, i)
            val = data[i:i+length]
            i += length
            # Try to decode as UTF-8 string or nested proto
            try:
                fields.append((field_num, "str", val.decode("utf-8")))
            except Exception:
                try:
                    inner = []
                    _collect_raw_proto(val, inner)
                    fields.append((field_num, "nested", inner))
                except Exception:
                    fields.append((field_num, "bytes", val[:20].hex()))
        elif wire_type == 5:  # 32-bit
            val = int.from_bytes(data[i:i+4], "little")
            i += 4
            fields.append((field_num, "i32", val))
        elif wire_type == 1:  # 64-bit
            val = int.from_bytes(data[i:i+8], "little")
            i += 8
            fields.append((field_num, "i64", val))
        else:
            break  # unknown wire type, stop

    for fn, wt, val in fields:
        if wt == "nested":
            print(f"{prefix} field[{fn}] (nested):")
            for nfn, nwt, nval in val:
                print(f"{prefix}   field[{nfn}] ({nwt}): {nval}")
        else:
            print(f"{prefix} field[{fn}] ({wt}): {val}")


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _collect_raw_proto(data: bytes, out: list) -> None:
    i = 0
    while i < len(data):
        b = data[i]
        field_num = b >> 3
        wire_type = b & 0x07
        i += 1
        if wire_type == 0:
            val, i = _read_varint(data, i)
            out.append((field_num, "varint", val))
        elif wire_type == 2:
            length, i = _read_varint(data, i)
            val = data[i:i+length]
            i += length
            try:
                out.append((field_num, "str", val.decode("utf-8")))
            except Exception:
                out.append((field_num, "bytes", val[:20].hex()))
        elif wire_type == 5:
            val = int.from_bytes(data[i:i+4], "little")
            i += 4
            out.append((field_num, "i32", val))
        elif wire_type == 1:
            val = int.from_bytes(data[i:i+8], "little")
            i += 8
            out.append((field_num, "i64", val))
        else:
            break


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Eufy AIOT probe")
    parser.add_argument("--mqtt-listen", metavar="DEVICE_SN",
                        help="After auth, connect to MQTT and listen for this device SN")
    parser.add_argument("--model", default="T2262",
                        help="Device model for MQTT topic (default: T2262)")
    parser.add_argument("--duration", type=int, default=120,
                        help="MQTT listen duration in seconds (default: 120)")
    args = parser.parse_args()

    # Phase 1: REST auth
    access_token, user_id = eufy_login()
    user_center_token, gtoken = get_user_center_info(access_token)
    mqtt_creds = get_mqtt_credentials(user_center_token, gtoken)
    aiot_devices = get_aiot_devices(user_center_token, gtoken)

    if not aiot_devices:
        print("\nAIOT device list is empty — trying cloud device list fallback...")
        cloud_devices = get_cloud_devices(access_token)

    print("\n=== SUMMARY ===")
    if mqtt_creds.get("endpoint_addr"):
        print(f"MQTT broker:  {mqtt_creds['endpoint_addr']}")
        print(f"thing_name:   {mqtt_creds.get('thing_name', '?')}")
        has_cert = bool(mqtt_creds.get("certificate_pem"))
        print(f"Has TLS cert: {has_cert}")
    else:
        print("MQTT credentials: NOT obtained")

    if aiot_devices:
        print(f"\nAIOT devices ({len(aiot_devices)}):")
        for dev in aiot_devices:
            sn = dev.get("device_sn", "?")
            name = dev.get("device_name", "?")
            model = dev.get("device_model", "?")
            print(f"  {name:30s}  sn={sn}  model={model}")
    else:
        print("\nNo AIOT devices found.")

    # Phase 2: MQTT (optional)
    if args.mqtt_listen:
        sn = args.mqtt_listen
        if not mqtt_creds.get("endpoint_addr"):
            print("Cannot listen: no MQTT credentials.")
            sys.exit(1)
        mqtt_listen(mqtt_creds, sn, args.model, args.duration)
    else:
        if mqtt_creds.get("endpoint_addr") and aiot_devices:
            sn = aiot_devices[0].get("device_sn", "")
            model = aiot_devices[0].get("device_model", args.model)
            if sn:
                print(f"\nTo listen for MQTT messages from the first device:")
                print(f"  python get_aiot_info.py --mqtt-listen {sn} --model {model} --duration 120")


if __name__ == "__main__":
    main()
