# DNP3 Architecture

## Component roles

### dnp3_master (SCADA)
- One per simulation.
- Issues Class 0/1/2/3 integrity polls on a configurable interval.
- Receives unsolicited responses (Class 1/2 events) from outstations.
- Issues control commands: Binary Output (CROB, Group 12) and Analogue Output (Group 41).
- Exposes REST API on port 1111 for Streamlit dashboard.
- Communicates via DNP3-TCP port 20000.

### dnp3_outstation (solar inverter / RTU)
- One container per inverter — count is config-driven.
- Each has a unique DNP3 outstation address.
- Reads physical values from SQLite, exposes them as DNP3 data points.
- Receives control commands from master, writes results back to SQLite.
- Sends unsolicited responses on Class 1/2 events (threshold-based).
- Exposes REST API on port 1111 for Streamlit dashboard.

### attacker
- One per simulation (optional — omit from config to disable).
- Sits in `vlan_it` network, simulates a rogue DNP3 master.
- Attack logic loaded from `config/<scenario>/logic/attack_logic.py`.
- Does NOT expose REST API — for dataset generation only.

---

## DNP3 data point mapping (per outstation)

| Direction            | DNP3 object            | Group | Physical value            |
|----------------------|------------------------|-------|---------------------------|
| Outstation → Master  | Analogue Input         | 30    | voltage_ac (V)            |
| Outstation → Master  | Analogue Input         | 30    | current_ac (A)            |
| Outstation → Master  | Analogue Input         | 30    | active_power (W)          |
| Outstation → Master  | Analogue Input         | 30    | frequency (Hz)            |
| Outstation → Master  | Analogue Input         | 30    | solar_irradiance (W/m²)   |
| Outstation → Master  | Analogue Input         | 30    | panel_temperature (°C)    |
| Outstation → Master  | Binary Input           | 1     | inverter_status (on/off)  |
| Outstation → Master  | Binary Input           | 1     | fault_alarm               |
| Master → Outstation  | Binary Output (CROB)   | 12    | enable / disable inverter |
| Master → Outstation  | Analogue Output        | 41    | power_curtailment (W)     |

Analogue Input events (Group 32) with timestamps are generated when values cross configured deadbands — these populate Class 1/2 event buffers and trigger unsolicited responses.

---

## Network topology

```
vlan_it  192.168.1.0/24          vlan_ot  192.168.0.0/24
┌──────────────────────┐         ┌──────────────────────────────┐
│  attacker            │         │  scada (dnp3_master)         │
│  engineering_ws      │──fw─────│  inverter_1 (dnp3_outstation)│
└──────────────────────┘         │  inverter_2 (dnp3_outstation)│
                                 │  solar_hil                   │
                                 └──────────────────────────────┘
```

`scada` bridges both networks. `attacker` starts in `vlan_it`; attack scripts pivot into `vlan_ot`.

---

## DNP3 function codes — must be implemented

These function codes are required for correct operation and future attack simulation:

| FC   | Name                    | Required for              |
|------|-------------------------|---------------------------|
| 0x01 | Read                    | Class polls               |
| 0x02 | Write                   | Time sync, control output |
| 0x03 | Direct Operate          | Control commands          |
| 0x04 | Direct Operate No Ack   | Attack DoS variant        |
| 0x07 | Warm Restart            | Attack: warm restart      |
| 0x0D | Cold Restart            | Attack: cold restart      |
| 0x14 | Enable Unsolicited      | Normal operation          |
| 0x15 | Disable Unsolicited     | Attack: block events      |
| 0x81 | Response                | All responses              |
| 0x82 | Unsolicited Response    | Event-driven reporting    |

---

## DNP3 object groups — must be implemented

| Group | Variation | Description                          |
|-------|-----------|--------------------------------------|
| 1     | 2         | Binary Input with flags              |
| 2     | 2         | Binary Input Event with time         |
| 10    | 2         | Binary Output with flags             |
| 12    | 1         | Control Relay Output Block (CROB)    |
| 20    | 1         | Counter (32-bit)                     |
| 30    | 1         | Analogue Input (32-bit float)        |
| 32    | 4         | Analogue Input Event with time       |
| 40    | 1         | Analogue Output status               |
| 41    | 1         | Analogue Output (32-bit float)       |
| 43    | 5         | Analogue Output Event with time      |
| 50    | 1         | Time and Date (time synchronisation) |
| 60    | 1–4       | Class objects (poll targets)         |

---

## Event classes

| Class   | Content                     | Trigger                              |
|---------|-----------------------------|--------------------------------------|
| Class 0 | Static (current values)     | Integrity poll from master           |
| Class 1 | High-priority events        | Value crosses deadband × 2           |
| Class 2 | Medium-priority events      | Value crosses deadband × 1           |
| Class 3 | Low-priority events         | Periodic (time-based)                |

Class 1/2 events trigger unsolicited responses. Class 3 events are polled.

---

## SQLite interaction (outstation)

Outstation reads physical values from SQLite every poll cycle:

```python
SELECT value FROM <physical_value_name>
ORDER BY timestamp DESC LIMIT 1
```

Control commands write back:

```python
INSERT INTO <physical_value_name> (value, hil) VALUES (?, ?)
```

**WAL mode must be enabled** before DNP3 work. Uncomment in `src/setup.py:622`:
```python
cursor.execute("PRAGMA journal_mode=WAL;")
cursor.execute("PRAGMA synchronous=NORMAL;")
```
