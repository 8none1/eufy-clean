#!/usr/bin/env python3
"""
Eufy/Tuya local key grabber.

Authenticates via Tuya Mobile API to retrieve localKey for each Eufy device.
The localKey is needed for the Tuya local (LAN) protocol.

Usage:
    python get_local_keys.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import struct
import uuid
from base64 import b64decode
from hashlib import md5
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Credentials (from martijnpoppen/eufy-clean and Rjevski/eufy-clean-local-key-grabber)
# ---------------------------------------------------------------------------
EUFY_USERNAME = "eufy@whizzy.org"
EUFY_PASSWORD = "SecurityFTW!999"
EUFY_LOGIN_URL = "https://home-api.eufylife.com/v1/user/email/login"
EUFY_UA = "EufyHome-Android-3.1.3-753"
EUFY_OPENUDID = "abcdef1234567890"

TUYA_CLIENT_ID = "yx5v9uc3ef9wg3v9atje"
TUYA_APP_SECRET = "s8x78u7xwymasd9kqa7a73pjhxqsedaj"
TUYA_BMP_SECRET = "cepev5pfnhua4dkqkdpmnrdxx378mpjr"
TUYA_CERT_SIGN = "A"
# HMAC key = certSign + "_" + bmpSecret + "_" + appSecret
TUYA_HMAC_KEY = f"{TUYA_CERT_SIGN}_{TUYA_BMP_SECRET}_{TUYA_APP_SECRET}".encode()

TUYA_BASE_URL = "https://a1.tuyaeu.com/api.json"

# AES key+IV for UID → password derivation
TUYA_PASSWORD_KEY = bytes([
    36, 78, 109, 138, 86, 172, 135, 145,
    36, 67, 45, 139, 108, 188, 162, 196,
])
TUYA_PASSWORD_IV = bytes([
    119, 36, 86, 242, 167, 102, 76, 243,
    57, 44, 53, 151, 233, 62, 87, 71,
])

# Fields included in the Tuya HMAC signing (in sort order)
TUYA_SIGN_KEYS = {
    "a", "v", "lat", "lon", "lang", "deviceId", "appVersion", "ttid",
    "isH5", "h5Token", "os", "clientId", "postData", "time", "requestId",
    "et", "n4h5", "sid", "sp",
}


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------

def shuffled_md5(value: str) -> str:
    h = md5(value.encode()).hexdigest()
    return h[8:16] + h[0:8] + h[24:32] + h[16:24]


def tuya_sign(params: dict[str, Any]) -> str:
    """Compute HMAC-SHA256 Tuya sign over eligible params."""
    sorted_keys = sorted(params.keys())
    parts = []
    for k in sorted_keys:
        if k not in TUYA_SIGN_KEYS:
            continue
        v = params[k]
        if v is None or v == "":
            continue
        if k == "postData":
            parts.append(f"postData={shuffled_md5(str(v))}")
        else:
            parts.append(f"{k}={v}")
    message = "||".join(parts)
    return hmac.new(TUYA_HMAC_KEY, message.encode(), hashlib.sha256).hexdigest()


def base_params(action: str, version: str = "1.0", sid: str | None = None,
                gid: str | None = None) -> dict[str, Any]:
    p: dict[str, Any] = {
        "appVersion": "2.4.0",
        "deviceId": "abcdef1234567890abcdef1234567890abcdef12345",
        "platform": "sdk_gphone64_arm64",
        "clientId": TUYA_CLIENT_ID,
        "lang": "en",
        "osSystem": "12",
        "os": "Android",
        "timeZoneId": "Europe/London",
        "ttid": "android",
        "et": "0.0.1",
        "sdkVersion": "3.0.8cAnker",
        "time": str(int(__import__("time").time())),
        "requestId": str(uuid.uuid4()).replace("-", ""),
        "a": action,
        "v": version,
    }
    if sid:
        p["sid"] = sid
    if gid:
        p["gid"] = gid
    return p


def tuya_post(action: str, data: dict | None = None, version: str = "1.0",
              sid: str | None = None, gid: str | None = None,
              base_url: str = TUYA_BASE_URL) -> dict:
    params = base_params(action, version, sid, gid)
    if data:
        params["postData"] = json.dumps(data, separators=(",", ":"))
    params["sign"] = tuya_sign(params)
    r = requests.post(base_url, data=params, timeout=15)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Password derivation
# ---------------------------------------------------------------------------

def derive_tuya_password(uid: str) -> str:
    """AES-CBC encrypt UID then MD5 → the 'base' password for login."""
    from Crypto.Cipher import AES
    # Pad UID to multiple of 16 by left-padding with '0'
    padded = uid.zfill(16 * math.ceil(max(len(uid), 1) / 16))
    cipher = AES.new(TUYA_PASSWORD_KEY, AES.MODE_CBC, TUYA_PASSWORD_IV)
    encrypted = cipher.encrypt(padded.encode())
    return md5(encrypted.hex().upper().encode()).hexdigest()


def rsa_no_padding_encrypt(exponent: str, modulus: str, message: str) -> str:
    """
    RSA encryption with no padding (textbook RSA).
    modulus and exponent are decimal strings (as returned by Tuya token API).
    Returns hex-encoded ciphertext zero-padded to key byte length.
    """
    n = int(modulus)
    e = int(exponent)
    m = int(message.encode().hex(), 16)
    c = pow(m, e, n)
    key_len_hex = (n.bit_length() + 7) // 8 * 2
    return hex(c)[2:].zfill(key_len_hex)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def eufy_login() -> tuple[str, str]:
    """Login to Eufy cloud. Returns (access_token, user_id)."""
    print("Logging in to Eufy...")
    r = requests.post(
        EUFY_LOGIN_URL,
        headers={
            "category": "Home", "Accept": "*/*",
            "openudid": EUFY_OPENUDID,
            "Content-Type": "application/json",
            "clientType": "1",
            "User-Agent": EUFY_UA,
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
    if not token or not user_id:
        raise RuntimeError(f"Eufy login failed: {data}")
    print(f"  user_id: {user_id}")
    return token, user_id


def tuya_get_local_keys(user_id: str) -> list[dict]:
    """
    Full Tuya Mobile API flow to get device local keys.
    Returns list of device dicts with 'id', 'name', 'localKey'.
    """
    uid = f"eh-{user_id}"
    print(f"Starting Tuya Mobile API flow for uid={uid} ...")

    # 1. Get login token (ephemeral RSA key)
    print("  1. Creating Tuya login token...")
    resp = tuya_post("tuya.m.user.uid.token.create",
                     data={"uid": uid, "countryCode": "44"})
    print(f"     Response: {json.dumps(resp)[:200]}")
    if not resp.get("success"):
        raise RuntimeError(f"tuya.m.user.uid.token.create failed: {resp}")

    result = resp["result"]
    token = result["token"]
    exponent = result.get("exponent", "65537")
    public_key = result["publicKey"]  # decimal string

    # 2. Derive and encrypt the password
    print("  2. Deriving password...")
    base_password = derive_tuya_password(uid)
    print(f"     base_password (md5 of encrypted uid): {base_password}")
    encrypted_passwd = rsa_no_padding_encrypt(exponent, public_key, base_password)

    # 3. Login with UID + encrypted password
    print("  3. Logging in to Tuya...")
    login_data = {
        "uid": uid,
        "passwd": encrypted_passwd,
        "countryCode": "44",
        "createGroup": True,
        "ifencrypt": 1,
        "options": {"group": 1},
        "token": token,
    }
    resp2 = tuya_post("tuya.m.user.uid.password.login.reg",
                      data=login_data)
    print(f"     Response: {json.dumps(resp2)[:300]}")

    # Fall back to the non-.reg variant if needed
    if not resp2.get("success"):
        print("     .reg failed, trying without .reg ...")
        resp2 = tuya_post("tuya.m.user.uid.password.login", data=login_data)
        print(f"     Response: {json.dumps(resp2)[:300]}")

    if not resp2.get("success"):
        raise RuntimeError(f"Tuya login failed: {resp2}")

    sid = resp2["result"]["sid"]
    # Use the domain returned in the response if present
    domain = resp2["result"].get("domain", {})
    api_url = domain.get("mobileApiUrl") or TUYA_BASE_URL
    if not api_url.endswith("/api.json"):
        api_url = api_url.rstrip("/") + "/api.json"
    print(f"  Tuya SID obtained. API URL: {api_url}")

    # 4. List homes
    print("  4. Listing homes...")
    homes_resp = tuya_post("tuya.m.location.list", version="2.1", sid=sid, base_url=api_url)
    print(f"     Response: {json.dumps(homes_resp)[:400]}")
    if not homes_resp.get("success"):
        raise RuntimeError(f"tuya.m.location.list failed: {homes_resp}")

    homes = homes_resp.get("result", [])
    if not homes:
        print("     No homes found!")
        return []

    # 5. For each home, list devices
    all_devices = []
    for home in homes:
        gid = str(home.get("groupId") or home.get("id", ""))
        home_name = home.get("name", gid)
        print(f"  5. Listing devices for home: {home_name} (gid={gid})...")
        dev_resp = tuya_post("tuya.m.my.group.device.list", version="1.0",
                             sid=sid, gid=gid, base_url=api_url)
        print(f"     Response: {json.dumps(dev_resp)[:500]}")
        if dev_resp.get("success") and dev_resp.get("result"):
            for dev in dev_resp["result"]:
                entry = {
                    "id": dev.get("devId", dev.get("id", "")),
                    "name": dev.get("name", ""),
                    "localKey": dev.get("localKey", ""),
                    "product_id": dev.get("productId", ""),
                    "category": dev.get("category", ""),
                    "online": dev.get("online", False),
                    "ip": dev.get("ip", ""),
                }
                all_devices.append(entry)
                print(f"     Device: {entry['name']} | id={entry['id']} | "
                      f"localKey={entry['localKey']} | online={entry['online']}")

    # Also try shared devices
    print("  5b. Checking shared devices...")
    shared_resp = tuya_post("tuya.m.my.shared.device.list", version="1.0",
                            sid=sid, base_url=api_url)
    if shared_resp.get("success") and shared_resp.get("result"):
        for dev in shared_resp["result"]:
            entry = {
                "id": dev.get("devId", dev.get("id", "")),
                "name": dev.get("name", ""),
                "localKey": dev.get("localKey", ""),
                "product_id": dev.get("productId", ""),
                "online": dev.get("online", False),
                "ip": dev.get("ip", ""),
            }
            print(f"     Shared device: {entry}")
            all_devices.append(entry)

    return all_devices


if __name__ == "__main__":
    _, user_id = eufy_login()
    devices = tuya_get_local_keys(user_id)
    print("\n=== SUMMARY ===")
    for d in devices:
        print(f"  {d['name']:30s}  id={d['id']}  localKey={d['localKey']}  online={d['online']}")
