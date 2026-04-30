#!/usr/bin/env python3
"""
Tuya goto coordinate interceptor via ARP poisoning.

ARP poisons the phone so its traffic to the robot (192.168.42.144:6668)
flows through this machine.  Captures the TCP stream, decrypts Tuya v3.3
SET packets, and extracts goto x,y from DPS 124.

Requires root (raw sockets):
    sudo standalone/.venv/bin/python standalone/intercept_goto.py

Workflow:
    1. Script auto-detects phone IP (first new TCP connection to robot)
       -- OR pass --phone-ip <ip> to skip detection
    2. ARP poisons phone ↔ robot so traffic flows through us
    3. IP forwarding passes traffic transparently
    4. Every DPS 124 SET packet is decrypted and printed
    5. When goto x,y appears → prints coordinates and exits
    6. ARP state is restored on exit

Both upstairs and downstairs robots are supported.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import struct
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Device config (same as tuya_local_control.py)
# ---------------------------------------------------------------------------
DEVICES = {
    "upstairs": {
        "ip": "192.168.42.144",
        "key": b"get{P<x#OI<qUenE",
    },
    "downstairs": {
        "ip": "192.168.42.17",
        "key": b"Sz~5?p~Gsjg$.s$$",
    },
}

MY_IP   = "192.168.42.43"
IFACE   = "enxac1a3de93d5e"
TUYA_PORT = 6668


# ---------------------------------------------------------------------------
# Tuya v3.3 packet decryption
# ---------------------------------------------------------------------------

def _tuya_decrypt(payload: bytes, key: bytes) -> bytes | None:
    """AES-ECB decrypt a Tuya v3.3 payload, stripping version prefix."""
    from Crypto.Cipher import AES
    if not payload:
        return None
    # Strip 15-byte v3.3 header if present ("3.3" + 12 null bytes)
    if payload[:3] == b"3.3":
        payload = payload[15:]
    if len(payload) % 16 != 0:
        return None
    try:
        cipher = AES.new(key[:16], AES.MODE_ECB)
        plain = cipher.decrypt(payload)
        # Strip PKCS7 padding
        pad = plain[-1]
        if 1 <= pad <= 16:
            plain = plain[:-pad]
        return plain
    except Exception:
        return None


def parse_tuya_packet(data: bytes, key: bytes) -> dict | None:
    """
    Parse a raw Tuya v3.3 TCP packet.

    Packet layout (55AA prefix):
      0- 3: magic 0x000055AA
      4- 7: seqno
      8-11: cmd
     12-15: length  (= retcode(4) + encrypted_payload + CRC(4) + suffix(4))
     16-19: retcode
     20 .. (16+length-9): AES-ECB encrypted payload
     (16+length-8) .. (16+length-5): CRC32
     (16+length-4) .. (16+length-1): suffix 0x0000AA55

    Returns decoded message dict or None if not a valid/decryptable Tuya packet.
    """
    if len(data) < 16:
        return None
    if data[:4] != b"\x00\x00U\xaa":
        return None
    seq     = int.from_bytes(data[4:8],   "big")
    cmd     = int.from_bytes(data[8:12],  "big")
    length  = int.from_bytes(data[12:16], "big")

    total = 16 + length
    if total > len(data):
        return None

    retcode   = int.from_bytes(data[16:20], "big")
    # encrypted = header(16) + retcode(4) .. total - suffix(8)
    enc_start = 20
    enc_end   = 16 + length - 8   # strip CRC(4) + suffix(4)
    if enc_end <= enc_start:
        return None
    encrypted = data[enc_start:enc_end]

    plain = _tuya_decrypt(encrypted, key)
    if not plain:
        return None

    try:
        msg = json.loads(plain)
    except Exception:
        return None

    return {"seq": seq, "cmd": cmd, "retcode": retcode, "msg": msg}


def decode_dps124(value: str) -> dict | None:
    """Decode a base64+JSON DPS 124 value."""
    try:
        return json.loads(base64.b64decode(value).decode())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ARP spoofing
# ---------------------------------------------------------------------------

_spoof_running = False
_spoof_thread: threading.Thread | None = None


def _get_mac(ip: str, iface: str) -> str | None:
    """Resolve IP→MAC from ARP table or by sending an ARP request."""
    import subprocess
    result = subprocess.run(["arp", "-n", ip], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == ip:
            mac = parts[2]
            if mac not in ("incomplete", "(incomplete)"):
                return mac
    return None


def _start_arp_spoof(phone_ip: str, robot_ip: str, iface: str,
                     phone_mac: str | None = None, robot_mac: str | None = None) -> None:
    """
    Continuously send ARP replies:
      - Tell phone:  robot_ip is at OUR mac
      - Tell robot:  phone_ip is at OUR mac
    So all traffic flows through us (IP forwarding does the rest).
    """
    global _spoof_running, _spoof_thread

    from scapy.all import ARP, Ether, sendp, get_if_hwaddr

    our_mac    = get_if_hwaddr(iface)
    phone_mac  = phone_mac or _get_mac(phone_ip,  iface)
    robot_mac  = robot_mac or _get_mac(robot_ip,  iface)

    if not phone_mac or not robot_mac:
        print(f"  Cannot resolve MACs: phone={phone_mac} robot={robot_mac}")
        return

    print(f"  Our MAC:   {our_mac}")
    print(f"  Phone MAC: {phone_mac}  ({phone_ip})")
    print(f"  Robot MAC: {robot_mac}  ({robot_ip})")

    # Packets to send continuously
    poison_phone = Ether(dst=phone_mac) / ARP(
        op=2, pdst=phone_ip, hwdst=phone_mac,
        psrc=robot_ip, hwsrc=our_mac
    )
    poison_robot = Ether(dst=robot_mac) / ARP(
        op=2, pdst=robot_ip, hwdst=robot_mac,
        psrc=phone_ip, hwsrc=our_mac
    )

    _spoof_running = True

    def _loop():
        while _spoof_running:
            sendp([poison_phone, poison_robot], iface=iface, verbose=False)
            time.sleep(1.5)

    _spoof_thread = threading.Thread(target=_loop, daemon=True)
    _spoof_thread.start()
    print("  ARP spoofing started.")


def _stop_arp_spoof(phone_ip: str, robot_ip: str, iface: str) -> None:
    """Restore correct ARP entries on phone and robot."""
    global _spoof_running

    _spoof_running = False
    if _spoof_thread:
        _spoof_thread.join(timeout=3)

    try:
        from scapy.all import ARP, Ether, sendp, get_if_hwaddr

        phone_mac = _get_mac(phone_ip,  iface)
        robot_mac = _get_mac(robot_ip,  iface)
        our_mac   = get_if_hwaddr(iface)

        if phone_mac and robot_mac:
            restore_phone = Ether(dst=phone_mac) / ARP(
                op=2, pdst=phone_ip, hwdst=phone_mac,
                psrc=robot_ip, hwsrc=robot_mac
            )
            restore_robot = Ether(dst=robot_mac) / ARP(
                op=2, pdst=robot_ip, hwdst=robot_mac,
                psrc=phone_ip, hwsrc=phone_mac
            )
            sendp([restore_phone, restore_robot] * 5, iface=iface, verbose=False)
            print("  ARP entries restored.")
    except Exception as e:
        print(f"  ARP restore error: {e}")


# ---------------------------------------------------------------------------
# IP forwarding
# ---------------------------------------------------------------------------

def _enable_ip_forward() -> bool:
    try:
        with open("/proc/sys/net/ipv4/ip_forward", "r") as f:
            was = f.read().strip()
        with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
            f.write("1\n")
        return was == "1"
    except Exception as e:
        print(f"  Warning: could not enable IP forwarding: {e}")
        return False


def _disable_ip_forward() -> None:
    try:
        with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
            f.write("0\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Phase 1: Discover phone IP by watching for new connections to robot
# ---------------------------------------------------------------------------

def discover_phone_ip(robot_ip: str, iface: str, timeout: int = 30) -> str | None:
    """
    Wait for a new TCP connection to robot_ip:6668.
    Returns the source IP (the phone).
    """
    from scapy.all import sniff, TCP, IP

    print(f"Waiting up to {timeout}s for phone to connect to {robot_ip}:6668 ...")
    print("(Open the Eufy app on your phone)")

    found: list[str] = []

    def _pkt(pkt):
        if (IP in pkt and TCP in pkt
                and pkt[IP].dst == robot_ip
                and pkt[TCP].dport == TUYA_PORT
                and pkt[TCP].flags & 0x02  # SYN
                and pkt[IP].src != MY_IP):
            found.append(pkt[IP].src)

    sniff(
        iface=iface,
        filter=f"tcp and dst host {robot_ip} and dst port {TUYA_PORT}",
        prn=_pkt,
        stop_filter=lambda _: bool(found),
        timeout=timeout,
        store=False,
    )
    return found[0] if found else None


# ---------------------------------------------------------------------------
# Phase 2: Capture and decrypt
# ---------------------------------------------------------------------------

def capture_goto(robot_ip: str, phone_ip: str, key: bytes,
                 iface: str, duration: int = 180) -> tuple[int, int] | None:
    """
    Sniff TCP traffic between phone and robot.
    Decrypt every Tuya SET packet (cmd=7) containing DPS 124.
    Return (x, y) when a goto command is found.
    """
    from scapy.all import sniff, TCP, IP, Raw

    print(f"\nCapturing Tuya traffic between {phone_ip} ↔ {robot_ip} ...")
    print(f"({duration}s window — use the Eufy app to send a goto to the bin)\n")

    found_coords: list[tuple[int, int]] = []
    buf: dict[tuple, bytes] = {}  # (src,sport) → accumulated bytes

    def _pkt(pkt):
        if IP not in pkt or TCP not in pkt or Raw not in pkt:
            return

        src = pkt[IP].src
        dst = pkt[IP].dst
        if not ({src, dst} == {phone_ip, robot_ip}):
            return

        stream_key = (src, pkt[TCP].sport)
        buf[stream_key] = buf.get(stream_key, b"") + bytes(pkt[Raw])
        data = buf[stream_key]

        # Try to find Tuya packet boundaries in the buffer
        offset = 0
        while offset < len(data):
            idx = data.find(b"\x00\x00U\xaa", offset)
            if idx == -1:
                break
            if len(data) - idx < 20:
                buf[stream_key] = data[idx:]
                break
            length = int.from_bytes(data[idx+12:idx+16], "big")
            pkt_end = idx + 20 + length - 4  # header(20) + payload(length) - suffix(4 already in length? check)
            # Tuya length = retcode(4) + payload + suffix(4), so full packet = 20 + length
            pkt_end = idx + 20 + length
            if pkt_end > len(data):
                buf[stream_key] = data[idx:]
                break

            raw_pkt = data[idx:pkt_end]
            offset = pkt_end

            parsed = parse_tuya_packet(raw_pkt, key)
            if not parsed:
                continue

            cmd = parsed["cmd"]
            msg = parsed["msg"]
            direction = "phone→robot" if src == phone_ip else "robot→phone"

            # cmd 7 = CONTROL (SET from app to device)
            # cmd 8 = STATUS (response from device)
            dps = msg.get("dps", {})
            if "124" in dps:
                decoded = decode_dps124(dps["124"])
                print(f"  [{direction}] cmd={cmd} DPS124: {decoded}")
                if isinstance(decoded, dict):
                    method = decoded.get("method", "")
                    data_field = decoded.get("data", {})
                    if method == "goto" and "x" in data_field and "y" in data_field:
                        x = data_field["x"]
                        y = data_field["y"]
                        map_id = data_field.get("mapId")
                        print(f"\n{'='*50}")
                        print(f"  *** GOTO COORDINATES FOUND ***")
                        print(f"  x={x}  y={y}  mapId={map_id}")
                        print(f"{'='*50}\n")
                        found_coords.append((x, y))
            elif dps and cmd == 7:
                # Any SET command from phone — show DPS keys
                print(f"  [{direction}] SET cmd={cmd} DPS keys={list(dps.keys())}")

        if found_coords:
            return True  # stop sniff

    sniff(
        iface=iface,
        filter=f"tcp and host {phone_ip} and host {robot_ip} and port {TUYA_PORT}",
        prn=_pkt,
        stop_filter=lambda _: bool(found_coords),
        timeout=duration,
        store=False,
    )

    return found_coords[0] if found_coords else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if os.geteuid() != 0:
        print("This script requires root.  Run with: sudo standalone/.venv/bin/python standalone/intercept_goto.py")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Intercept Eufy goto coordinates via ARP spoof")
    parser.add_argument("--device",    default="upstairs", choices=list(DEVICES),
                        help="Which robot (default: upstairs)")
    parser.add_argument("--phone-ip",  default=None,
                        help="Phone IP (auto-detected from first connection if omitted)")
    parser.add_argument("--phone-mac", default=None,
                        help="Phone MAC address (e.g. aa:bb:cc:dd:ee:ff) — skips ARP table lookup")
    parser.add_argument("--robot-mac", default=None,
                        help="Robot MAC address — skips ARP table lookup")
    parser.add_argument("--iface",     default=IFACE,
                        help=f"Network interface (default: {IFACE})")
    parser.add_argument("--duration",  type=int, default=180,
                        help="Capture duration in seconds (default: 180)")
    args = parser.parse_args()

    dev      = DEVICES[args.device]
    robot_ip = dev["ip"]
    key      = dev["key"]
    iface    = args.iface

    print(f"Target robot: {args.device} ({robot_ip})")
    print(f"Interface:    {iface}")
    print()

    # Step 1: Find phone IP
    phone_ip = args.phone_ip
    if not phone_ip:
        phone_ip = discover_phone_ip(robot_ip, iface, timeout=60)
        if not phone_ip:
            print("Could not detect phone IP. Pass --phone-ip <ip> manually.")
            sys.exit(1)
        print(f"Detected phone IP: {phone_ip}")

    # Step 2: Enable IP forwarding
    was_forwarding = _enable_ip_forward()
    print(f"IP forwarding: enabled (was {'on' if was_forwarding else 'off'})")

    # Step 3: Start ARP spoofing
    print(f"\nStarting ARP poison: {phone_ip} ↔ {robot_ip}")
    _start_arp_spoof(phone_ip, robot_ip, iface,
                     phone_mac=args.phone_mac, robot_mac=args.robot_mac)
    time.sleep(2)  # let ARP tables update

    # Cleanup on Ctrl+C
    coords: list[tuple[int, int]] = []

    def _cleanup(signum=None, frame=None):
        print("\nCleaning up...")
        _stop_arp_spoof(phone_ip, robot_ip, iface)
        if not was_forwarding:
            _disable_ip_forward()
        if coords:
            print(f"\nFinal result: goto({coords[0][0]}, {coords[0][1]})")
            print(f"\nAdd to tuya_local_control.py DEVICES['{args.device}']:")
            print(f"  'bin_x': {coords[0][0]},")
            print(f"  'bin_y': {coords[0][1]},")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    # Step 4: Capture
    result = capture_goto(robot_ip, phone_ip, key, iface, duration=args.duration)

    if result:
        coords.append(result)

    _cleanup()


if __name__ == "__main__":
    main()
