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

These function codes are required for correct operation and future attack simulation.
**Corrected 2026-11-08** — a live capture (tshark DNP3 dissector on `ics_ot_network`)
showed the real wire values for Direct Operate/Restart differ from earlier drafts of
this table; real IEEE 1815 numbering used below:

| FC   | Name                    | Required for              |
|------|-------------------------|---------------------------|
| 0x01 | Read                    | Class polls               |
| 0x02 | Write                   | Time sync, control output — confirmed on wire 2026-11-11 (LAN time sync, Group 50 Var 1) |
| 0x03 | Select                  | Select-Before-Operate (step 1) |
| 0x04 | Operate                 | Select-Before-Operate (step 2) |
| 0x05 | Direct Operate          | Control commands — confirmed on wire (not 0x03) |
| 0x06 | Direct Operate No Ack   | Attack DoS variant        |
| 0x0D | Cold Restart            | Attack: cold restart — confirmed on wire |
| 0x0E | Warm Restart            | Attack: warm restart (not 0x07 — that's Immediate Freeze, unused here) |
| 0x14 | Enable Unsolicited      | Normal operation — confirmed on wire |
| 0x15 | Disable Unsolicited     | Attack: block events — confirmed on wire |
| 0x18 | Record Current Time     | LAN time sync — confirmed on wire 2026-11-11 (part of the LAN sync sequence, not Delay Measure) |
| 0x81 | Response                | All responses              |
| 0x82 | Unsolicited Response    | Event-driven reporting — confirmed on wire |

(The earlier "FC 0x80: File transfer" entry in `attack-support.md` doesn't correspond to
any real DNP3 function code — real file-transfer operations are FC 0x19–0x1E.)

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

**Status: complete, tagged `v0.1-dnp3-basic` (commit `9d1aa98`).** Verified end-to-end
with a live Wireshark capture on the `ics_ot_network` interface (`tcp.port == 20000`):
master ↔ outstation Class 0123 integrity polls every 5 s, plus unsolicited responses on
deadband crossing — both correctly Confirmed on the wire. (An earlier build of
`dnp3_master.py` crashed on startup — `class _CommandCallback(opendnp3.ICommandCallback)`
subclassed an interface that doesn't exist in `dnp3-python==0.3.0b2`; fixed by passing a
plain `Callable[[ICommandTaskResult], None]` to `DirectOperate()` instead.)

What the function-code / object-group tables above require for **full** attack
support (Activity 2) vs. what `dnp3_master.py` / `dnp3_outstation.py` implement today:

| Feature                            | Status                                                                         |
|-------------------------------------|---------------------------------------------------------------------------------|
| FC 0x01 Read / Class 0–3 polls      | Implemented — `master.AddClassScan(ClassField.AllClasses(), ...)`              |
| FC 0x05 Direct Operate              | Implemented — `SolarCommandHandler.Operate()` (CROB + AnalogOutput*). Delivery confirmed live; **the calling process used to crash on command-task completion (dnp3-python 0.3.0b2 bug) — fixed 2026-11-08, see CLAUDE.md "dnp3-python command path crashes on completion"** |
| Unsolicited responses (FC 0x82)     | Implemented — `allowUnsolicited=True`, `EventMode.Detect` + per-point deadband |
| Group 30/32 Analogue Input (+Event) | Implemented                                                                    |
| Group 1/2 Binary Input (+Event)     | Implemented                                                                    |
| Group 12 CROB / Group 41 AO         | Implemented                                                                    |
| FC 0x03/0x04 Select-Before-Operate  | Nominally present (`SolarCommandHandler.Select()` exists to satisfy the interface) but unconditionally returns `SUCCESS` — no real reservation/timeout state, so not meaningfully implemented |
| FC 0x0E/0x0D Warm/Cold Restart      | **Implemented 2026-11-10** (`solar_plant_500kw` branch) — `WarmRestartSupport`/`ColdRestartSupport` return `SUPPORTED_DELAY_COARSE`; `WarmRestart()` logs and returns a delay; `ColdRestart()` exits the process after 2s so Docker's `restart: unless-stopped` policy (added to `build_dnp3_outstation_yaml()`) brings the container back up fresh. Master side: `POST /restart/<name>` `{"type":"warm"\|"cold"}`. **`Restart()`'s completion crashes the calling process exactly like `DirectOperate()`'s does** (confirmed live: `TaskCompletion.SUCCESS` on a warm restart killed `scada` the same `PyThreadState_Get` GIL fault) — same Disable/isolated-worker/Enable treatment as the command path. Verified live: warm + cold restart both land (outstation logs the request; `inverter_X`'s cold-restarted container uptime resets while its creation time doesn't, confirming Docker actually restarted it), `scada` stays up through both, confirmed on the wire (FC13/FC14 seen via tshark). |
| Group 50 Time sync (Write)          | **Partially implemented 2026-11-11** — `WriteAbsoluteTime()` logs the received time and marks the outstation synced (used to decide when to re-request); the value itself still isn't stored/reused for anything (needed for full attack #7). Trigger fixed: `GetApplicationIIN()` now reports `IINBit.NEED_TIME` until synced (previously never fired — see CLAUDE.md "Group 50 time sync never triggers" for the 2026-11-09 finding that led here). Confirmed live via `POST /timesync/<name>`: full LAN sync sequence FC21(Disable Unsol)→FC1(Read)→**FC24(Record Current Time)**→**FC2(Write, Group 50 Var 1)**→FC20(Enable Unsol), each answered FC129. **LAN mode uses Record Current Time, not Delay Measure (FC23)** — FC23 is for the `NonLAN` mode's round-trip delay compensation, not used here. **The task crashes the calling process** (uncaught C++ `pybind11::cast_error` → `terminate()` — not a catchable Python GIL fault like DirectOperate/Restart), so it must never run on the persistent polling master (`timeSyncMode` there is deliberately `None`, not `LAN`) — only exercised via the isolated `/timesync` worker. |
| FC 0x14/0x15 Enable/Disable Unsolicited | **Implemented 2026-11-10** — previously these only ever appeared automatically on the master's own session startup; now exposed on-demand via `POST /unsolicited/<name>` `{"action":"enable"\|"disable"}`, using `master.PerformFunction()` with Class1/2/3 headers. No command-task involved (no callback param on `PerformFunction`), so — unlike Restart/DirectOperate — this does **not** crash the calling process; no isolation needed. Confirmed on the wire (FC20/FC21). |

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
