# DNP3 Attack Support Requirements

Attack scripts are NOT implemented in Activity 1. This document defines what the DNP3 implementation must expose so that Activity 2 attack scripts can be added without modifying core framework code.

---

## Attack taxonomy

| #  | Attack type                    | Criticality | DNP3 feature required                         |
|----|-------------------------------|-------------|-----------------------------------------------|
| 1  | Replay                        | High        | Sequence counters, event timestamps (Group 32)|
| 2  | Spoofing                      | High        | Source IP tracking in master, UR origin check |
| 3  | Man-in-the-Middle             | Critical    | Full link/transport/application layer framing |
| 4  | Unauthorized command injection| Critical    | FC 0x03/0x04 handlers on outstation           |
| 5  | Denial of Service             | High        | Connection rate-limiting hooks                |
| 6  | Warm/Cold Restart             | High        | FC 0x07 (warm), FC 0x0D (cold) on outstation  |
| 7  | Time synchronisation attack   | High        | Group 50 write handler on outstation          |
| 8  | Unsolicited response spoofing | Critical    | UR sequence management, source validation     |
| 9  | False data injection          | Critical    | Realistic noise model in HIL (not flat values)|

---

## What the DNP3 implementation MUST expose for each attack

### Attacks 1, 2, 8 — Replay / Spoofing / UR manipulation
- Outstation must maintain a **sequence counter** (Application layer, 4-bit).
- Unsolicited responses must include correct sequence number increments.
- Master must log source IP of every UR message — attack scripts can spoof this.
- Event timestamps (Group 32) must use real wall-clock time from SQLite `timestamp` column.

### Attack 3 — MITM
- Full DNP3 framing at all three layers must be correct:
  - Data Link Layer: start bytes 0x0564, length, control, destination, source, CRC
  - Transport Layer: FIR/FIN bits, sequence number
  - Application Layer: FC, sequence, objects
- If framing is wrong, captured PCAPs will be unrecognisable by Wireshark DNP3 dissector → useless for IDS.

### Attack 4 — Unauthorized command injection
- Outstation must handle **FC 0x03 (Direct Operate)** and **FC 0x04 (Direct Operate No Ack)**.
- It must write the command result to SQLite regardless of source IP (no auth in v1).
- Attack scripts send FC 0x03/0x04 with Binary Output (Group 12) or Analogue Output (Group 41).

### Attack 5 — DoS
- Outstation must handle **connection reset gracefully** — it must reconnect to master after a flood drops the TCP session.
- Master must handle **reconnection backoff** — it must not crash if outstation is unreachable.
- No special FC required; DoS operates at TCP layer.

### Attack 6 — Warm/Cold Restart
- Outstation must handle **FC 0x07 (Warm Restart)**: reset sequence counters, re-announce via unsolicited.
- Outstation must handle **FC 0x0D (Cold Restart)**: full container restart (exit process — Docker restarts it).
- Cold restart clears all in-memory event buffers — this is the expected and correct behaviour.

### Attack 7 — Time synchronisation
- Outstation must handle **Group 50 Variation 1 (Write)** to update internal clock.
- Time value must be stored and used as the timestamp in subsequent Group 32 events.
- Attack scripts write an arbitrary epoch value to desync event timestamps.

### Attack 9 — False data injection
- HIL `solar_hil_logic.py` must generate **realistic noise** on all physical values:
  - Irradiance: sinusoidal day/night cycle + Gaussian noise (σ ≈ 5 W/m²)
  - Voltage: 230V nominal ± random walk (σ ≈ 1V per cycle)
  - Frequency: 50Hz ± 0.02Hz noise
- Flat or deterministic values make injected anomalies trivially detectable — defeating IDS evaluation.

---

## PCAP capture requirements

Attack scripts must produce two labelled datasets:

| Dataset  | Content                                  | Label in CSV |
|----------|------------------------------------------|--------------|
| Benign   | Normal polling, URs, control commands    | 0            |
| Malicious| One of attacks 1–9, labelled by type     | 1 (+ subtype)|

Capture tool: Wireshark / `tshark` on the Docker bridge interface (`ics_ot_network`).

```bash
tshark -i ics_ot_network -w data/pcap/benign_<date>.pcap &
# run simulation for N minutes
kill %1
```

Each PCAP file should have a companion CSV with per-packet labels:
`timestamp, src_ip, dst_ip, dnp3_fc, dnp3_object_group, label, attack_type`

---

## Attack container logic interface

`config/solar_plant/logic/attack_logic.py` must implement a single entry point:

```python
def run(targets: list[dict], master_address: int, config: dict):
    """
    targets: list of {"name": str, "ip": str, "outstation_address": int}
    master_address: rogue master DNP3 address
    config: full scenario config dict
    """
```

The attacker container calls `run()` on startup. Attack type and intensity are controlled via environment variables set in the container config — no code changes needed to switch attack types.

---

## Wireshark validation

Before generating datasets, validate that Wireshark correctly dissects the traffic:

1. Start simulation: `docker compose up`
2. Capture: `tshark -i ics_ot_network -Y dnp3 -V | head -100`
3. Verify: FC, object group, values visible in dissector output.

If DNP3 frames are not recognised, the framing implementation is incorrect — fix before generating any datasets.
