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

### attacker (planned — Activity 2, not yet implemented)
- One per simulation (optional — omit from config to disable).
- Sits in `vlan_it` network, simulates a rogue DNP3 master.
- Attack logic loaded from `config/<scenario>/logic/attack_logic.py`.
- Does NOT expose REST API — for dataset generation only.
- Requires the two-network topology described below. Not present in the
  current `config/solar_plant/configuration.json`, which uses a single
  `vlan_ot` network.

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

### Current (Phase 1, implemented)

```
vlan_ot  192.168.0.0/24
┌────────────────────────────────────────┐
│  ui                    192.168.0.5      │
│  scada (dnp3_master)   192.168.0.10     │
│  inverter_1 (dnp3_outstation) .20       │
│  inverter_2 (dnp3_outstation) .21       │
│  solar_hil             192.168.0.30     │
└────────────────────────────────────────┘
```

### Planned (Activity 2 — attacker support, not yet implemented)

```
vlan_it  192.168.1.0/24          vlan_ot  192.168.0.0/24
┌──────────────────────┐         ┌──────────────────────────────┐
│  attacker            │         │  scada (dnp3_master)         │
│  engineering_ws      │──fw─────│  inverter_1 (dnp3_outstation)│
└──────────────────────┘         │  inverter_2 (dnp3_outstation)│
                                 │  solar_hil                   │
                                 └──────────────────────────────┘
```

`scada` would bridge both networks. `attacker` starts in `vlan_it`; attack scripts pivot into `vlan_ot`.

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

## Phase 1 implementation status

What the function-code / object-group tables above require for **full** attack
support (Activity 2) vs. what `dnp3_master.py` / `dnp3_outstation.py` implement today:

| Feature                            | Status                                                                         |
|-------------------------------------|---------------------------------------------------------------------------------|
| FC 0x01 Read / Class 0–3 polls      | Implemented — `master.AddClassScan(ClassField.AllClasses(), ...)`              |
| FC 0x03/0x04 Direct Operate         | Implemented — `SolarCommandHandler.Operate()` (CROB + AnalogOutput*)           |
| Unsolicited responses (FC 0x82)     | Implemented — `allowUnsolicited=True`, `EventMode.Detect` + per-point deadband |
| Group 30/32 Analogue Input (+Event) | Implemented                                                                    |
| Group 1/2 Binary Input (+Event)     | Implemented                                                                    |
| Group 12 CROB / Group 41 AO         | Implemented                                                                    |
| FC 0x07/0x0D Warm/Cold Restart      | Not implemented — `WarmRestartSupport`/`ColdRestartSupport` return `RestartMode.UNSUPPORTED` (needed for attack #6) |
| Group 50 Time sync (Write)          | Stub only — `WriteAbsoluteTime()` logs and returns `True`; value is not stored or reused (needed for attack #7) |

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

**Concurrency**: WAL mode was tried and reverted — it broke existing Modbus
scenarios. Instead, the outstation's poll loop (`src/components/dnp3_outstation.py`)
sets a busy timeout on its own connection:
```python
conn.execute("PRAGMA busy_timeout = 2000;")  # wait up to 2s if hil.py holds the lock
```
combined with the framework default `PRAGMA synchronous = OFF` set in `src/setup.py`
when the database is created. Sufficient for 2 outstations @ 5 s poll interval —
revisit if polling frequency increases significantly.
