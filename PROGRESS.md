# Eufy X8 Bin-Goto Feature — Progress Log

## Goal

After a cleaning run completes, automatically navigate the robot to the household bin
(trash can) so the dustbin can be emptied, then send it home.  This requires knowing
the SLAM coordinates of the bin in the robot's internal coordinate space.

---

## Robot inventory

| Label | IP | Local key | Notes |
|---|---|---|---|
| upstairs | 192.168.42.144 | `get{P<x#OI<qUenE` | T2262 / T2262EV |
| downstairs | 192.168.42.17 | `Sz~5?p~Gsjg$.s$$` | T2262 / T2262EV |

Laptop (calculon): `192.168.42.43`, interface `enxac1a3de93d5e` (USB Ethernet adapter),
MAC `ac:1a:3d:e9:3d:5e`.

Phone (Will's iPhone): IP `192.168.42.153`, MAC `28:49:e9:10:59:b6`.

Robot MAC (upstairs): `5c:c5:63:7d:05:e4`.

---

## Protocol facts established

### Tuya local (LAN) protocol v3.3
- TCP port 6668, AES-ECB encrypted with the device `localKey` (first 16 bytes)
- Packet format: `55AA` prefix, 16-byte header (`prefix[4] + seqno[4] + cmd[4] + length[4]`),
  4-byte retcode, AES-ECB payload, 4-byte CRC, 4-byte suffix `AA55`
- Payload may have a 15-byte `"3.3\x00…"` version prefix — strip before decrypting
- cmd 7 = CONTROL (app → device), cmd 8 = STATUS (device → app)

### DPS 124 — `command_trans`
The bidirectional command transport for goto, map requests, etc.  Value is
base64-encoded JSON.

**App → robot (goto command):**
```json
{"method":"goto","data":{"mapId":1,"x":1234,"y":5678},"timestamp":1234567890123}
```

**Robot → app (response):**
```json
{"method":"goto","data":{"result":"S"},"timestamp":…}
```
Result codes: `S` = started, `O` = arrived, `F` = failed/sleeping.

**Critical:** The goto coordinates only appear in the SET command sent from the app
to the robot over TCP.  They are NOT stored in any DPS status field that can be polled.
The only way to capture them is to intercept the TCP stream.

### Other DPS of interest
- DPS 15: robot state (`Completed`, `Sleeping`, `Cleaning`, etc.) — triggers the automation
- DPS 120: map data — returns nothing useful via local polling (likely requires active map session)
- DPS 121: map stream trigger — no effect when set locally
- DPS 128: map ID

### AIOT platform (Eufy cloud MQTT)
Newer Eufy devices use a separate AIOT platform for map data (DPS 152-180, protobuf).
**T2262 / T2262EV robots are NOT registered on the AIOT broker.**  Confirmed by:
- `get_device_list` returns empty device list
- MQTT SUBACK returns `0x80` (denied) for all topics

No map data is accessible via AIOT for these robots.

---

## Coordinate interception approach

Since coordinates only exist in the app's TCP SET command, the strategy is:

1. ARP-poison the phone so traffic to `robot_ip` flows through the laptop
2. ARP-poison the robot so responses to `phone_ip` flow through the laptop
3. Enable IP forwarding so the laptop transparently forwards all traffic
4. Capture TCP stream on port 6668, decrypt, extract goto x/y from DPS 124

### What has been confirmed working
- **ARP spoofing works**: confirmed by tcpdump — phone SYNs to robot arrive at laptop
- **IP forwarding works**: full TCP handshake completes (SYN → ACK → data seen in tcpdump)
- **Data flows**: 104-byte and 88-byte payloads seen transiting through laptop
- **App uses local Tuya protocol** (not cloud) on port 6668 ✓
- **Robot → phone traffic bypasses laptop**: robot side ARP works but robot sends
  SYN-ACK directly to phone MAC anyway; phone traffic still goes through us.
  **This is fine** — the goto command goes phone → robot, which is the direction we capture.

### Scapy crash (fixed)
scapy's `L2ListenSocket` crashed with `'Layer [Raw] not found'` on Linux when
receiving TCP packets with no payload (SYN/ACK frames).  **Fixed** by replacing the
scapy `sniff()` call entirely with a raw `AF_PACKET / SOCK_RAW` socket that parses
Ethernet/IP/TCP headers in pure Python.  No extra dependencies.

---

## Current state — READY TO TEST

The interceptor script is at `standalone/intercept_goto.py`.  It has been written,
debugged, and the scapy crash fixed.  **The raw-socket version has not yet been
run end-to-end** (the fix was committed just before writing this document).

### Run command
```bash
sudo standalone/.venv/bin/python standalone/intercept_goto.py \
  --robot-ip 192.168.42.144 \
  --key "get{P<x#OI<qUenE" \
  --phone-ip 192.168.42.153 \
  --phone-mac 28:49:e9:10:59:b6 \
  --robot-mac 5c:c5:63:7d:05:e4
```

### Procedure
1. **Force-quit the Eufy app** on the phone (so it makes a fresh connection after poisoning)
2. Run the command above
3. Wait for the `READY — NOW open the Eufy app` banner
4. Open the Eufy app — let it connect to the robot
5. Use the app to send the robot to the bin location ("Go to location" / point on map)
6. Script prints coordinates and exits; Ctrl+C also works
7. Cleanup is automatic (ARP restored, iptables rules removed)

Expected output when successful:
```
==================================================
  *** GOTO COORDINATES FOUND ***
  x=1234  y=5678  mapId=1
==================================================
```

---

## Next steps after capturing coordinates

1. Add coordinates to `standalone/tuya_local_control.py` DEVICES dict:
   ```python
   "upstairs": {
       "ip": "192.168.42.144",
       "key": b"get{P<x#OI<qUenE",
       "bin_x": <X>,
       "bin_y": <Y>,
       "bin_map_id": <mapId>,
   }
   ```

2. Implement `cmd_goto_bin` in `tuya_local_control.py`:
   ```python
   def cmd_goto_bin(device_name: str) -> None:
       d = _device(device_name)
       dev = DEVICES[device_name]
       payload = json.dumps({"method":"goto","data":{"mapId":dev["bin_map_id"],
                             "x":dev["bin_x"],"y":dev["bin_y"]},"timestamp":int(time.time()*1000)})
       d.set_value(124, base64.b64encode(payload.encode()).decode())
   ```

3. Test `goto_bin` manually:
   ```bash
   standalone/.venv/bin/python standalone/tuya_local_control.py goto_bin upstairs
   ```

4. Build Home Assistant automation:
   - Trigger: DPS 15 → `Completed`
   - Action: call `goto_bin` (via shell command or HA REST)
   - Then: wait for user to empty bin (button press or time delay)
   - Then: send robot home (DPS 101 = True / return_home command)

---

## Files

| File | Purpose |
|---|---|
| `standalone/intercept_goto.py` | ARP MitM interceptor — captures bin coordinates |
| `standalone/tuya_local_control.py` | Local robot control via Tuya LAN protocol |
| `standalone/get_aiot_info.py` | AIOT auth + MQTT probe (confirmed T2262 not AIOT) |
| `standalone/get_local_keys.py` | Retrieve device local keys from Eufy/Tuya cloud |
| `standalone/requirements.txt` | Python deps: tinytuya, scapy, pycryptodome, paho-mqtt, protobuf |

Virtual environment: `standalone/.venv/`  — activate or use `standalone/.venv/bin/python` directly.

---

## Dead ends (do not re-investigate)

- **Tuya developer API**: robots are registered to Eufy account only, not to a Tuya IoT Platform developer account
- **AIOT cloud API**: T2262 not registered; device list empty; MQTT denied
- **DPS 120 map polling**: returns nothing useful; map data requires an active session handshake we haven't reverse-engineered
- **Spiral coordinate search from (0,0)**: the bin is nowhere near the dock; not a viable approach
- **MitM with TLS**: Tuya local protocol is AES-ECB over plain TCP, no TLS — no certificate issues
