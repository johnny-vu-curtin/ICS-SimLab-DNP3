# ICS-SimLab DNP3

Containerised ICS simulation framework extended with DNP3 protocol for energy-sector cybersecurity research (MPhil, Curtin University). The base framework supports Modbus TCP/RTU. This branch adds DNP3-TCP and a solar plant scenario to generate realistic benign + attack traffic datasets for IDS/XAI research.

Active branch: `feature/dnp3-solar-plant`

---

## Repository layout

```
main.py                        # entry point
src/setup.py                   # config → docker-compose.yaml + container dirs
src/components/                # component runtime logic (plc, hmi, sensor, actuator, hil, ui)
src/docker-files/              # Dockerfiles
config/<scenario>/             # scenario definitions (configuration.json + logic/)
simulation/                    # GENERATED at runtime — do not edit
docker-compose.yaml            # GENERATED at runtime — do not edit
```

---

## Run an existing scenario

```bash
python3 main.py config/smart_grid   # generates simulation/ and docker-compose.yaml
docker compose build
docker compose up
# Dashboard: http://localhost:8501
```

Existing scenarios: `smart_grid`, `water_bottle_factory`, `intelligent_electronic_device`

---

## Key ports

| Port  | Service                        |
|-------|--------------------------------|
| 8501  | Streamlit dashboard (UI)       |
| 1111  | REST API — per component       |
| 502   | Modbus TCP (inbound)           |
| 5020  | Modbus TCP (exposed)           |
| 20000 | DNP3-TCP (new)                 |

---

## Runtime data flow

```
HIL logic ↔ SQLite (physical state) ↔ Sensors / Actuators
                                    ↔ DNP3 Outstations ↔ DNP3 Master
                                    ↔ Modbus PLCs / HMIs
```

SQLite lives at `simulation/communications/physical_interactions.db`.
It is the single shared physical state bus — all components read/write through it.

---

## DNP3 additions

**Phase 1 (complete, tagged `v0.1-dnp3-basic`)** — `dnp3_master` (SCADA) and `dnp3_outstation` (solar inverter) communicate over DNP3-TCP in the new `config/solar_plant/` scenario. Verified with a live Wireshark capture on the `ics_ot_network` interface (`tcp.port == 20000`): Class 0123 integrity polls every 5 s plus unsolicited responses on deadband crossing, both Confirmed correctly.

| Type              | Role              | DNP3 role   | Status                |
|-------------------|-------------------|-------------|-----------------------|
| `dnp3_master`     | SCADA controller  | Master      | Implemented           |
| `dnp3_outstation` | Solar inverter    | Outstation  | Implemented           |
| `attacker`        | Attack simulator  | Rogue master| Implemented (Activity 2 — command injection) |

Run it like any other scenario:

```bash
python3 main.py config/solar_plant
docker compose build
docker compose up
```

### Activity 2 — attacker component + `config/solar_plant_attack`

`attacker` is a first-class component type (built by `src/setup.py` like any other) that
runs as a rogue DNP3 master. The `config/solar_plant_attack/` scenario clones
`config/solar_plant` and adds a second network (`vlan_it`, 192.168.1.0/24) plus a
dual-homed `attacker` (IT 192.168.1.100 + OT 192.168.0.200) for IT→OT lateral movement.

```bash
python3 main.py config/solar_plant_attack
docker compose build && docker compose up -d
```

- Attack type/intensity are config-driven (`attack.type`, `attack.intensity`,
  `attack.start_delay_s` in `configuration.json` → env vars `ATTACK_TYPE` etc.). Switching
  attacks needs no code change. `type: "none"` makes the rogue master idle.
- Attack logic lives in `config/solar_plant_attack/logic/attack_logic.py`, entry point
  `run(targets, master_address, config)`, dispatched by `ATTACKS[type]`.
- **Implemented attack**: `command_injection` (taxonomy #4) — unauthenticated FC 0x03
  DirectOperate forcing `inverter_enable=0` + `power_curtailment=100`. Verified live: both
  land in SQLite, attacker stays up indefinitely.
- **Dataset methodology**: capture benign and malicious in **separate phases**. For a
  clean attack capture, `docker compose stop scada` first so the rogue master is the only
  master on outstation address 1 (two masters on the same address contend for the single
  outstation session and flap — realistic spoofing contention, but it muddies labels).

### REST API additions (port 1111)

| Component         | Endpoint                            | Purpose                                    |
|--------------------|--------------------------------------|---------------------------------------------|
| `dnp3_outstation` | `GET /registers`                     | Current data point values (dashboard)      |
| `dnp3_master`     | `GET /registers`                     | Flattened values across all outstations    |
| `dnp3_master`     | `GET /registers/<outstation_name>`   | Values for one outstation                  |
| `dnp3_master`     | `POST /command/<outstation_name>`    | DirectOperate — `{"type": "binary_output"\|"analogue_output", "index": int, "value": number}`. Runs in an isolated worker process (see "dnp3-python command path crashes on completion" below) — safe to call repeatedly. |

### Test suite

```bash
pytest tests/unit/          # no Docker required — pydnp3 mocked via conftest.py
pytest tests/integration/   # requires `docker compose up -d`; auto-skips if stack not running
```

→ Architecture + data mapping: `.claude/doc/dnp3-architecture.md`
→ Config JSON format: `.claude/doc/solar-plant-config.md`
→ Attack support requirements (Activity 2): `.claude/doc/attack-support.md`
→ Library selection + Docker constraints: `.claude/doc/library-notes.md`
→ Physical correlation rules for `config/solar_plant` HIL logic: `.claude/doc/rules_logic-solar_plant.md`
  — read before changing any simulation logic in `solar_hil_logic.py`
→ Physical correlation rules for `config/solar_plant_500kw` HIL logic (grid-tie model):
  `.claude/doc/rules_logic-solar_plant_500kw.md` — read this first, it builds on the rules above

### Comment convention for DNP3 code

- Files that are **entirely** DNP3 code (`dnp3_master.py`, `dnp3_outstation.py`,
  `config/*/logic/solar_hil_logic.py`): a single `# Author: Van Sanh Vu, Purpose: DNP3
  development` line near the top of the file is enough — do not repeat it above every
  function/class.
- Files that **mix** DNP3 with other protocols (e.g. `ui.py`, shared with Modbus): add
  the `# Author: Van Sanh Vu, Purpose: DNP3 development` line locally, right above each
  DNP3-specific function/block, since there's no single whole-file author tag that fits.
- Every function and class (in both cases above) must still have its own one-line
  `# PURPOSE: ...` comment describing what it does.

---

## Add a new scenario (no code changes)

1. Create `config/<name>/configuration.json`
2. Add logic files to `config/<name>/logic/`
3. Run `python3 main.py config/<name>`

Schema reference: `.claude/doc/solar-plant-config.md`

---

## Add a new component type (requires code changes)

1. `src/components/<type>.py` — runtime logic
2. `src/setup.py` — add `build_<type>_yaml()` + `build_<type>_directory()`
3. `src/docker-files/<type>/Dockerfile`
4. `src/components/ui.py` — add section if component needs dashboard visibility

---

## Critical constraints — read before touching DNP3 code

- **DNP3 library**: use `dnp3-python` (VOLTTRON/PNNL) only, pinned to `dnp3-python==0.3.0b2`.
  `pydnp3` is abandoned (last release 2018, requires Python 2.7 headers — will not build).
  Installed only inside `src/docker-files/dnp3/Dockerfile` — NOT in host `requirements.txt`
  (host runs Python 3.12, which has no compatible wheel).
- **Docker base image for DNP3 containers**: `ubuntu:22.04` + Python 3.10 or 3.11.
  `dnp3-python` has no wheel for Python 3.12 on Ubuntu 24.04.
- **SQLite concurrency**: WAL mode was tried and reverted — it broke existing Modbus
  scenarios. Current approach is `PRAGMA synchronous = OFF` (`src/setup.py`) plus
  `PRAGMA busy_timeout = 2000` in the outstation's poll loop
  (`src/components/dnp3_outstation.py`). Sufficient for 2 outstations @ 5 s poll
  interval — revisit if polling frequency increases significantly.
- **Network topology**: `config/solar_plant` runs on a single `vlan_ot` network
  (192.168.0.0/24) — scada master, both outstations, HIL, and UI. The two-network
  `vlan_it`/`vlan_ot` split for `attacker` + IT→OT lateral movement is implemented in
  `config/solar_plant_attack` (Activity 2). `build_attacker_yaml` accepts either a single
  `network` dict or a `networks` list (dual-homed); `build_network_yaml` already loops
  over `ip_networks`, so adding `vlan_it` needs no code change.
- **dnp3-python command path crashes on completion (0.3.0b2)**: issuing a DirectOperate
  (FC 0x05 — not 0x03, that's Select; confirmed on the wire) reliably delivers the frame
  AND drives the outstation, but the calling process then dies when the command task
  completes — a native asio-thread GIL fault (`PyThreadState_Get ... thread state NULL`)
  that happens with Python *or* C++ completion callbacks alike. Live-reproduced on the
  **legitimate** master too (2026-11-08): the old code called `DirectOperate()` directly
  on the persistent polling master object, so the very first real command killed `scada`
  outright (exit 139) with no `restart:` policy to recover it — this broke
  `tests/integration/test_dnp3_comms.py` from I-07b onward if ever run live.
  **Fixed** in `src/components/dnp3_master.py`: `POST /command/<name>` now spawns a
  background thread (`_run_isolated_command`) that (1) `master.Disable()`s the
  persistent connection to that one outstation — confirmed live that a *second*
  simultaneous TCP session on the same link address makes both flap and silently drops
  queued commands, so the persistent session must step aside first, not just be a second
  connection running alongside it; (2) runs the command in a disposable child OS process
  (`_cli_send_command`, invoked via `subprocess.Popen([sys.executable, __file__,
  "--send-command", ...])` — a real `exec()`, not `fork()`, since this process already
  has live asio polling threads and forking those is unsafe) that connects fresh, sends
  one DirectOperate, and exits — any crash is contained to that worker; (3)
  `master.Enable()`s polling again. A per-outstation `threading.Lock` in
  `_command_locks` serializes overlapping requests to the same outstation. The attacker's
  own workaround in `config/solar_plant_attack/logic/attack_logic.py` (`_inject_single`,
  forking **one child process per command**) is a different, narrower case — it works
  because that parent process never creates its own `DNP3Manager`/asio threads, so
  `fork()` is safe there specifically; do not port that fork-based pattern into
  `dnp3_master.py`, which does have live asio threads.
- **Phase 1 command-handler bugs fixed in Activity 2** (`src/components/dnp3_outstation.py`
  — only ever exercised once a control command is actually sent): (1) `ICommandHandler`'s
  pure virtual is `Start()`, not `Begin()` — the old name aborted the outstation on the
  first command; (2) command handler + outstation application must be held in a
  module-level `_keep_alive` list — pybind11 keeps only weak refs, so they were GC'd and
  the next virtual call aborted; (3) `opendnp3.ControlCode` has no bare `CLOSE` in this
  binding (use `CLOSE_PULSE_ON`/`PULSE_ON`); (4) `Operate()` now wraps its body in
  try/except so a malformed/malicious command returns `DOWNSTREAM_FAIL` instead of
  aborting the container.
- **Port 20000**: IANA-registered DNP3-TCP default. Do not change without updating all configs.

---

## Verify DNP3 library installs correctly

```bash
docker run --rm python:3.11-slim pip install dnp3-python
```

Expected: installs without compilation. If it fails, check Python version and platform.

---

## Research context

MPhil — Curtin University (School of EECMS)
Supervisors: Dr Sonny Pham, Dr Sie Teng Soh, Dr Mahathir Almashor
Collaboration: CSIRO

Three activities:
1. DNP3 support + solar plant simulation (current)
2. Attack scenarios → labelled PCAP datasets
3. Autoencoder IDS + XAI (SHAP/LIME + Gemma 4)
