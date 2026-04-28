#!/usr/bin/env python3
"""
Eufy Clean standalone MQTT probe.

Authenticates with Eufy cloud, discovers devices, connects to AWS IoT MQTT,
subscribes to all topics, and logs + decodes every message. Sends a start
command to the named device on connect, stops sending commands after N hours.

Usage:
    python eufy_probe.py [--no-command] [--config path/to/config.json]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import ssl
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
import requests

# ---------------------------------------------------------------------------
# Path setup — load protos directly, bypassing the HA-dependent package __init__.py
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
PROTO_CLOUD_DIR = REPO_ROOT / "custom_components" / "robovac_mqtt" / "proto" / "cloud"
COMPONENT_DIR = REPO_ROOT / "custom_components" / "robovac_mqtt"

import types as _types
import importlib.util as _ilu


def _load_proto(name: str):
    """Load a proto file from the cloud dir as a proper package member."""
    full_name = f"custom_components.robovac_mqtt.proto.cloud.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = _ilu.spec_from_file_location(full_name, PROTO_CLOUD_DIR / f"{name}.py")
    mod = _ilu.module_from_spec(spec)
    mod.__package__ = "custom_components.robovac_mqtt.proto.cloud"
    sys.modules[full_name] = mod
    # Also register under the short name for intra-proto relative imports
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Build the minimal package hierarchy so relative imports inside the proto
# files resolve correctly (they do "from ...proto.cloud import common_pb2" etc.)
for _pkg in [
    "custom_components",
    "custom_components.robovac_mqtt",
    "custom_components.robovac_mqtt.proto",
    "custom_components.robovac_mqtt.proto.cloud",
]:
    if _pkg not in sys.modules:
        _m = _types.ModuleType(_pkg)
        _m.__package__ = _pkg
        _m.__path__ = []  # mark as package
        sys.modules[_pkg] = _m

# Set __path__ on cloud package to point at the real directory
sys.modules["custom_components.robovac_mqtt.proto.cloud"].__path__ = [str(PROTO_CLOUD_DIR)]
sys.modules["custom_components.robovac_mqtt.proto.cloud"].__file__ = str(PROTO_CLOUD_DIR / "__init__.py")

# Load proto modules in dependency order
for _proto_name in [
    "common_pb2", "clean_param_pb2", "control_pb2", "work_status_pb2",
    "station_pb2", "clean_statistics_pb2", "consumable_pb2",
    "error_code_pb2", "scene_pb2", "stream_pb2", "universal_data_pb2",
]:
    _load_proto(_proto_name)

clean_statistics_pb2 = sys.modules["custom_components.robovac_mqtt.proto.cloud.clean_statistics_pb2"]
consumable_pb2      = sys.modules["custom_components.robovac_mqtt.proto.cloud.consumable_pb2"]
control_pb2         = sys.modules["custom_components.robovac_mqtt.proto.cloud.control_pb2"]
error_code_pb2      = sys.modules["custom_components.robovac_mqtt.proto.cloud.error_code_pb2"]
scene_pb2           = sys.modules["custom_components.robovac_mqtt.proto.cloud.scene_pb2"]
station_pb2         = sys.modules["custom_components.robovac_mqtt.proto.cloud.station_pb2"]
stream_pb2          = sys.modules["custom_components.robovac_mqtt.proto.cloud.stream_pb2"]
universal_data_pb2  = sys.modules["custom_components.robovac_mqtt.proto.cloud.universal_data_pb2"]
work_status_pb2     = sys.modules["custom_components.robovac_mqtt.proto.cloud.work_status_pb2"]

# Load utils.py (only depends on google.protobuf, no HA)
_spec = _ilu.spec_from_file_location("eufy_utils", COMPONENT_DIR / "utils.py")
_utils_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_utils_mod)
decode = _utils_mod.decode
encode = _utils_mod.encode
encode_message = _utils_mod.encode_message

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eufy_probe")
# Quieten paho's own chatter
logging.getLogger("paho.mqtt").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# API constants (mirrors const.py)
# ---------------------------------------------------------------------------
UA = "EufyHome-Android-3.1.3-753"
LOGIN_URL = "https://home-api.eufylife.com/v1/user/email/login"
USER_INFO_URL = "https://api.eufylife.com/v1/user/user_center_info"
DEVICE_LIST_URL = "https://aiot-clean-api-pr.eufylife.com/app/devicerelation/get_device_list"
DEVICE_V2_URL = "https://api.eufylife.com/v1/device/v2"
MQTT_INFO_URL = "https://aiot-clean-api-pr.eufylife.com/app/devicemanage/get_user_mqtt_info"

DPS_MAP = {
    "152": "PLAY_PAUSE",
    "153": "WORK_STATUS",
    "154": "CLEANING_PARAMETERS",
    "155": "DIRECTION",
    "156": "MULTI_MAP_SW",
    "158": "CLEAN_SPEED",
    "160": "FIND_ROBOT",
    "163": "BATTERY_LEVEL",
    "164": "MAP_EDIT",
    "165": "MAP_DATA",
    "166": "MAP_STREAM",
    "167": "CLEANING_STATISTICS",
    "168": "ACCESSORIES_STATUS",
    "169": "MAP_MANAGE",
    "173": "STATION_STATUS / GO_HOME",
    "176": "UNSETTING",
    "177": "ERROR_CODE",
    "180": "SCENE_INFO",
}

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def eufy_login(username: str, password: str, openudid: str) -> dict[str, Any]:
    """Full auth flow → returns dict with session, user_info, mqtt_creds."""
    log.info("Logging in as %s ...", username)
    r = requests.post(
        LOGIN_URL,
        headers={
            "category": "Home",
            "Accept": "*/*",
            "openudid": openudid,
            "Content-Type": "application/json",
            "clientType": "1",
            "User-Agent": UA,
        },
        json={
            "email": username,
            "password": password,
            "client_id": "eufyhome-app",
            "client_secret": "GQCpr9dSp3uQpsOMgJ4xQ",
        },
        timeout=30,
    )
    r.raise_for_status()
    session = r.json()
    if not session.get("access_token"):
        raise RuntimeError(f"Login failed: {session}")
    log.info("Login OK — access_token obtained")

    # User info
    log.info("Fetching user info ...")
    r = requests.get(
        USER_INFO_URL,
        headers={
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "user-agent": UA,
            "category": "Home",
            "token": session["access_token"],
            "openudid": openudid,
            "clienttype": "2",
        },
        timeout=30,
    )
    r.raise_for_status()
    user_info = r.json()
    if not user_info.get("user_center_id"):
        raise RuntimeError(f"No user_center_id: {user_info}")
    user_info["gtoken"] = hashlib.md5(user_info["user_center_id"].encode()).hexdigest()
    log.info("User info OK — user_center_id=%s", user_info["user_center_id"])

    common_aiot_headers = {
        "user-agent": UA,
        "openudid": openudid,
        "os-version": "Android",
        "model-type": "PHONE",
        "app-name": "eufy_home",
        "x-auth-token": user_info["user_center_token"],
        "gtoken": user_info["gtoken"],
        "content-type": "application/json; charset=UTF-8",
    }

    # MQTT credentials
    log.info("Fetching MQTT credentials ...")
    r = requests.post(MQTT_INFO_URL, headers=common_aiot_headers, timeout=30)
    r.raise_for_status()
    mqtt_creds = r.json().get("data")
    if not mqtt_creds:
        raise RuntimeError(f"No MQTT creds in response: {r.json()}")
    log.info("MQTT creds OK — endpoint=%s  thing=%s",
             mqtt_creds.get("endpoint_addr"), mqtt_creds.get("thing_name"))

    return {
        "session": session,
        "user_info": user_info,
        "mqtt_creds": mqtt_creds,
        "aiot_headers": common_aiot_headers,
    }


def probe_http_status(auth: dict[str, Any], device_id: str) -> None:
    """Try a few HTTP endpoints to get device status without MQTT."""
    log.info("Probing HTTP status endpoints for device %s ...", device_id)
    headers = auth["aiot_headers"]

    endpoints = [
        ("POST", "https://aiot-clean-api-pr.eufylife.com/app/devicemanage/get_device_info",
         {"device_sn": device_id}),
        ("POST", "https://aiot-clean-api-pr.eufylife.com/app/clean/get_device_info",
         {"device_sn": device_id}),
        ("POST", "https://aiot-clean-api-pr.eufylife.com/app/devicemanage/get_device_status",
         {"device_sn": device_id}),
        ("POST", "https://aiot-clean-api-pr.eufylife.com/app/devicemanage/device_properties",
         {"device_sn": device_id}),
        ("GET", f"https://api.eufylife.com/v1/device/{device_id}/info", None),
    ]

    session_headers = {
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "user-agent": UA,
        "category": "Home",
        "token": auth["session"]["access_token"],
        "openudid": headers["openudid"],
        "clienttype": "2",
    }

    for method, url, body in endpoints:
        try:
            if method == "POST":
                r = requests.post(url, headers=headers, json=body, timeout=10)
            else:
                r = requests.get(url, headers=session_headers, timeout=10)
            log.info("  %s %s → HTTP %s: %s",
                     method, url.split("/")[-1], r.status_code,
                     r.text[:200] if r.status_code != 200 else r.json())
        except Exception as e:
            log.info("  %s %s → error: %s", method, url.split("/")[-1], e)


def get_devices(auth: dict[str, Any]) -> list[dict[str, Any]]:
    """Discover devices. Try AIOT first, fall back to cloud V2."""
    log.info("Fetching AIOT device list ...")
    r = requests.post(
        DEVICE_LIST_URL,
        headers=auth["aiot_headers"],
        json={"attribute": 3},
        timeout=30,
    )
    aiot_devices = []
    if r.status_code == 200:
        data = r.json()
        log.debug("AIOT raw: %s", json.dumps(data, indent=2)[:2000])
        raw = data.get("data", {}).get("devices") or []
        aiot_devices = [d["device"] for d in raw]
        log.info("AIOT returned %d device(s)", len(aiot_devices))
    else:
        log.warning("AIOT list failed: HTTP %s", r.status_code)

    log.info("Fetching cloud V2 device list ...")
    r = requests.get(
        DEVICE_V2_URL,
        headers={
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "user-agent": UA,
            "category": "Home",
            "token": auth["session"]["access_token"],
            "openudid": auth["mqtt_creds"].get("user_id", ""),
            "clienttype": "2",
        },
        timeout=30,
    )
    cloud_devices = []
    if r.status_code == 200:
        data = r.json()
        log.debug("Cloud V2 raw: %s", json.dumps(data, indent=2)[:2000])
        cloud_devices = data.get("devices", [])
        log.info("Cloud V2 returned %d device(s)", len(cloud_devices))
    else:
        log.warning("Cloud V2 list failed: HTTP %s", r.status_code)

    # Build device records
    devices = []
    if aiot_devices:
        cloud_by_id = {d["id"]: d for d in cloud_devices}
        for dev in aiot_devices:
            sn = dev.get("device_sn", "")
            cloud = cloud_by_id.get(sn, {})
            product_code = cloud.get("product", {}).get("product_code", "") or dev.get("device_model", "")
            devices.append({
                "device_id": sn,
                "device_name": cloud.get("alias_name") or cloud.get("name") or sn,
                "product_code": product_code,
                "model_truncated": product_code[:5],
                "soft_version": dev.get("main_sw_version") or dev.get("soft_version") or "",
                "dps": dev.get("dps", {}),
            })
    elif cloud_devices:
        log.warning("AIOT empty — using cloud V2 only (no initial DPS)")
        for dev in cloud_devices:
            product_code = dev.get("product", {}).get("product_code", "")
            devices.append({
                "device_id": dev["id"],
                "device_name": dev.get("alias_name") or dev.get("name") or dev["id"],
                "product_code": product_code,
                "model_truncated": product_code[:5],
                "soft_version": dev.get("software_version", ""),
                "dps": {},
            })

    for d in devices:
        log.info("  Device: name=%-20s  id=%s  product_code=%s  (truncated=%s)",
                 d["device_name"], d["device_id"], d["product_code"], d["model_truncated"])

    return devices


# ---------------------------------------------------------------------------
# Protobuf decoding helpers
# ---------------------------------------------------------------------------

def try_decode_all(dps_key: str, b64_value: str) -> str:
    """Try all known protobuf types and return the best decode."""
    dps_name = DPS_MAP.get(dps_key, f"DPS-{dps_key}")

    # Battery level is a plain int string, not protobuf
    if dps_key == "163":
        return f"{dps_name} = {b64_value!r}  (battery: {b64_value}%)"

    # Clean speed is a plain int index
    if dps_key == "158":
        speeds = ["No Suction", "Standard", "Boost IQ", "MAX", "MAX+"]
        try:
            idx = int(b64_value)
            speed = speeds[idx] if 0 <= idx < len(speeds) else b64_value
        except (ValueError, TypeError):
            speed = b64_value
        return f"{dps_name} = {b64_value!r}  (speed: {speed})"

    # Protobuf types to try in order of specificity
    candidates = [
        ("WorkStatus",        work_status_pb2.WorkStatus,           True),
        ("StationResponse",   station_pb2.StationResponse,          False),
        ("CleanStatistics",   clean_statistics_pb2.CleanStatistics, False),
        ("ConsumableResponse",consumable_pb2.ConsumableResponse,    False),
        ("ErrorCode",         error_code_pb2.ErrorCode,             False),
        ("SceneResponse",     scene_pb2.SceneResponse,              True),
        ("UniversalData",     universal_data_pb2.UniversalDataResponse, True),
        ("RoomParams",        stream_pb2.RoomParams,                True),
        ("ModeCtrlRequest",   control_pb2.ModeCtrlRequest,          False),
    ]

    results = []
    for type_name, proto_type, has_len in candidates:
        try:
            msg = decode(proto_type, b64_value, has_length=has_len)
            text = str(msg).strip()
            if text:
                results.append(f"{type_name}: {text}")
        except Exception:
            pass

    if results:
        # Prefer the longest / most informative decode
        best = max(results, key=len)
        return f"{dps_name}  →  {best}"

    return f"{dps_name} = {b64_value!r}  (could not decode)"


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------

class EufyProbe:
    def __init__(
        self,
        devices: list[dict[str, Any]],
        mqtt_creds: dict[str, Any],
        openudid: str,
        command_device_name: str,
        send_commands: bool,
    ):
        self.devices = devices
        self.mqtt_creds = mqtt_creds
        self.openudid = openudid
        self.command_device = next(
            (d for d in devices if command_device_name.lower() in d["device_name"].lower()),
            devices[0] if devices else None,
        )
        self.send_commands = send_commands
        self.start_time = time.time()
        self.command_sent = False

        self._clients: list[mqtt.Client] = []
        self._cert_files: list[str] = []

    def _make_client(self, device: dict[str, Any]) -> mqtt.Client:
        creds = self.mqtt_creds
        ts = int(time.time() * 1000)
        client_id = (
            f"android-{creds['app_name']}-eufy_android_{self.openudid}_{creds['user_id']}-{ts}"
        )

        client = mqtt.Client(client_id=client_id, transport="tcp")
        client.username_pw_set(creds["thing_name"])

        # Write cert + key to temp files
        cert_file = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".pem")
        cert_file.write(creds["certificate_pem"])
        cert_file.close()
        os.chmod(cert_file.name, stat.S_IRUSR | stat.S_IWUSR)

        key_file = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".key")
        key_file.write(creds["private_key"])
        key_file.close()
        os.chmod(key_file.name, stat.S_IRUSR | stat.S_IWUSR)

        self._cert_files.extend([cert_file.name, key_file.name])

        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=cert_file.name, keyfile=key_file.name)
        client.tls_set_context(ctx)

        # Bind device info to the client via userdata
        client.user_data_set(device)

        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.on_subscribe = self._on_subscribe

        return client

    def _on_connect(self, client: mqtt.Client, device: dict, flags, rc):
        if rc != 0:
            log.error("Connect failed for %s: rc=%d", device["device_name"], rc)
            return

        log.info("Connected for device: %s  (product=%s  id=%s)",
                 device["device_name"], device["product_code"], device["device_id"])

        # Subscribe to the specific topic (using truncated model as in HA)
        specific_topic = (
            f"cmd/eufy_home/{device['model_truncated']}/{device['device_id']}/res"
        )
        client.subscribe(specific_topic)
        log.info("  Subscribed: %s", specific_topic)

        # Also subscribe using the full product code in case truncation is wrong
        if device["product_code"] != device["model_truncated"]:
            full_topic = (
                f"cmd/eufy_home/{device['product_code']}/{device['device_id']}/res"
            )
            client.subscribe(full_topic)
            log.info("  Subscribed: %s  (full product code)", full_topic)

        # Wildcards — try to catch anything the broker will deliver
        for wildcard in ["cmd/eufy_home/#", "eufy_home/#", "#"]:
            result = client.subscribe(wildcard)
            log.info("  Subscribed: %s  (rc=%s)", wildcard, result[0])

        # Send commands to the target device
        if (
            self.send_commands
            and self.command_device
            and device["device_id"] == self.command_device["device_id"]
            and not self.command_sent
        ):
            elapsed = time.time() - self.start_time
            if elapsed < self.command_timeout_hours * 3600:
                log.info("Sending start_auto to %s ...", device["device_name"])
                self._send_start(client, device)
                # Also request accessory status to see if robot responds at all
                self._send_status_request(client, device)
                self.command_sent = True

    def _on_subscribe(self, client: mqtt.Client, device: dict, mid, granted_qos):
        # granted_qos is a list of QoS codes; 0x80 means subscription rejected by broker
        for i, qos in enumerate(granted_qos):
            if qos == 0x80:
                log.error("  SUBSCRIPTION REJECTED (0x80) for mid=%s index=%d on %s — broker policy denied it",
                          mid, i, device["device_name"])
            else:
                log.debug("  Subscription granted QoS=%d for mid=%s on %s", qos, mid, device["device_name"])

    def _on_disconnect(self, client: mqtt.Client, device: dict, rc):
        log.warning("Disconnected from %s: rc=%d", device["device_name"], rc)

    def _on_message(self, client: mqtt.Client, device: dict, msg: mqtt.MQTTMessage):
        log.info("─" * 70)
        log.info("MESSAGE on topic: %s", msg.topic)
        log.info("  for device context: %s", device["device_name"])
        try:
            outer = json.loads(msg.payload.decode())
            payload_raw = outer.get("payload", {})
            if isinstance(payload_raw, str):
                payload_data = json.loads(payload_raw)
            else:
                payload_data = payload_raw

            log.debug("  outer keys: %s", list(outer.keys()))
            log.debug("  payload keys: %s", list(payload_data.keys()) if isinstance(payload_data, dict) else "not a dict")

            dps = payload_data.get("data", {})
            if not dps:
                log.info("  No DPS data in payload. Full payload: %s",
                         json.dumps(payload_data)[:500])
                return

            log.info("  DPS keys received: %s", list(dps.keys()))
            for key, value in dps.items():
                if isinstance(value, str) and len(value) > 4:
                    decoded = try_decode_all(str(key), value)
                    log.info("  [%s] %s", key, decoded)
                else:
                    dps_name = DPS_MAP.get(str(key), f"DPS-{key}")
                    log.info("  [%s] %s = %r", key, dps_name, value)

        except Exception as e:
            log.warning("  Failed to parse message: %s", e)
            log.debug("  Raw payload: %s", msg.payload[:500])

    def _send_start(self, client: mqtt.Client, device: dict):
        """Send start_auto command."""
        # Build start_auto manually — ModeCtrlRequest(method=0, auto_clean=AutoClean(clean_times=1))
        # START_AUTO_CLEAN = 0 in the Method enum
        msg = control_pb2.ModeCtrlRequest(
            method=0,
            auto_clean=control_pb2.AutoClean(clean_times=1, force_mapping=False),
        )
        value = encode_message(msg)
        dps = {"152": value}

        ts = int(time.time() * 1000)
        creds = self.mqtt_creds
        client_id = (
            f"android-{creds['app_name']}-eufy_android_{self.openudid}_{creds['user_id']}"
        )
        mqtt_val = {
            "head": {
                "client_id": client_id,
                "cmd": 65537,
                "cmd_status": 2,
                "msg_seq": 1,
                "seed": "",
                "sess_id": client_id,
                "sign_code": 0,
                "timestamp": ts,
                "version": "1.0.0.1",
            },
            "payload": json.dumps({
                "account_id": creds["user_id"],
                "data": dps,
                "device_sn": device["device_id"],
                "protocol": 2,
                "t": ts,
            }),
        }

        log.info("  DPS payload: %s", dps)
        log.info("  Full MQTT message: %s", json.dumps(mqtt_val)[:500])

        topic = f"cmd/eufy_home/{device['model_truncated']}/{device['device_id']}/req"
        payload_bytes = json.dumps(mqtt_val).encode()
        result = client.publish(topic, payload_bytes)
        log.info("  start_auto published to %s  (rc=%s  mid=%s)",
                 topic, result.rc, result.mid)

        # Also try with full product code
        if device["product_code"] != device["model_truncated"]:
            topic2 = f"cmd/eufy_home/{device['product_code']}/{device['device_id']}/req"
            result2 = client.publish(topic2, payload_bytes)
            log.info("  start_auto also published to %s  (full code)  (rc=%s)",
                     topic2, result2.rc)

    def _send_raw_dps(self, client: mqtt.Client, device: dict, dps: dict, label: str):
        """Send an arbitrary DPS dict to the device."""
        ts = int(time.time() * 1000)
        creds = self.mqtt_creds
        client_id = (
            f"android-{creds['app_name']}-eufy_android_{self.openudid}_{creds['user_id']}"
        )
        mqtt_val = {
            "head": {
                "client_id": client_id,
                "cmd": 65537,
                "cmd_status": 2,
                "msg_seq": 1,
                "seed": "",
                "sess_id": client_id,
                "sign_code": 0,
                "timestamp": ts,
                "version": "1.0.0.1",
            },
            "payload": json.dumps({
                "account_id": creds["user_id"],
                "data": dps,
                "device_sn": device["device_id"],
                "protocol": 2,
                "t": ts,
            }),
        }
        topic = f"cmd/eufy_home/{device['model_truncated']}/{device['device_id']}/req"
        result = client.publish(topic, json.dumps(mqtt_val).encode())
        log.info("  [%s] published to %s  (rc=%s)", label, topic, result.rc)
        if device["product_code"] != device["model_truncated"]:
            topic2 = f"cmd/eufy_home/{device['product_code']}/{device['device_id']}/req"
            result2 = client.publish(topic2, json.dumps(mqtt_val).encode())
            log.info("  [%s] also published to %s  (full code, rc=%s)", label, topic2, result2.rc)

    def _send_status_request(self, client: mqtt.Client, device: dict):
        """Send a ConsumableRequest (accessories query) to see if robot responds."""
        from google.protobuf.json_format import MessageToDict
        # ConsumableRequest with empty get_types asks for all accessory status
        req = consumable_pb2.ConsumableRequest()
        value = encode_message(req)
        dps = {"168": value}
        log.info("Sending accessories status request to %s ...", device["device_name"])
        self._send_raw_dps(client, device, dps, "accessories_req")

    def run(self, command_timeout_hours: float = 2.0, run_minutes: int = 30):
        self.command_timeout_hours = command_timeout_hours

        endpoint = self.mqtt_creds["endpoint_addr"]
        log.info("Connecting to MQTT endpoint: %s", endpoint)

        for device in self.devices:
            client = self._make_client(device)
            client.connect(endpoint, 8883, keepalive=60)
            client.loop_start()
            self._clients.append(client)
            time.sleep(1)  # stagger connections

        log.info("Listening for %d minutes (Ctrl-C to stop early) ...", run_minutes)
        # Resend command every 60s in case robots wake up during the run
        deadline = time.time() + run_minutes * 60
        cmd_interval = 60
        last_cmd = time.time()
        try:
            while time.time() < deadline:
                time.sleep(5)
                if (
                    self.send_commands
                    and self.command_device
                    and (time.time() - last_cmd) >= cmd_interval
                    and (time.time() - self.start_time) < self.command_timeout_hours * 3600
                ):
                    target = self.command_device
                    for c in self._clients:
                        if c._userdata and c._userdata["device_id"] == target["device_id"]:
                            log.info("Re-sending start_auto to %s (periodic)...", target["device_name"])
                            self._send_start(c, target)
                            break
                    last_cmd = time.time()
        except KeyboardInterrupt:
            log.info("Interrupted.")
        finally:
            for client in self._clients:
                client.loop_stop()
                client.disconnect()
            for f in self._cert_files:
                try:
                    os.unlink(f)
                except OSError:
                    pass
            log.info("Done.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Eufy MQTT probe")
    parser.add_argument("--config", default=Path(__file__).parent / "config.json")
    parser.add_argument("--no-command", action="store_true",
                        help="Don't send any commands, just listen")
    parser.add_argument("--minutes", type=int, default=30,
                        help="How long to listen (default 30)")
    args = parser.parse_args()

    with open(args.config) as f:
        config = json.load(f)

    username = config["username"]
    password = config["password"]
    command_device_name = config.get("command_device", "Downstairs")
    command_timeout_hours = config.get("command_timeout_hours", 2)
    openudid = str(uuid.uuid4()).replace("-", "")[:16]

    auth = eufy_login(username, password, openudid)
    devices = get_devices(auth)

    if not devices:
        log.error("No devices found. Exiting.")
        sys.exit(1)

    # Probe HTTP endpoints to find any non-MQTT status path
    for d in devices:
        probe_http_status(auth, d["device_id"])

    probe = EufyProbe(
        devices=devices,
        mqtt_creds=auth["mqtt_creds"],
        openudid=openudid,
        command_device_name=command_device_name,
        send_commands=not args.no_command,
    )
    probe.run(
        command_timeout_hours=command_timeout_hours,
        run_minutes=args.minutes,
    )


if __name__ == "__main__":
    main()
