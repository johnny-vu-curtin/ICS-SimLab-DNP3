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
| `attacker`        | Attack simulator  | Rogue master| Planned (Activity 2)  |

Run it like any other scenario:

```bash
python3 main.py config/solar_plant
docker compose build
docker compose up
```

### REST API additions (port 1111)

| Component         | Endpoint                            | Purpose                                    |
|--------------------|--------------------------------------|---------------------------------------------|
| `dnp3_outstation` | `GET /registers`                     | Current data point values (dashboard)      |
| `dnp3_master`     | `GET /registers`                     | Flattened values across all outstations    |
| `dnp3_master`     | `GET /registers/<outstation_name>`   | Values for one outstation                  |
| `dnp3_master`     | `POST /command/<outstation_name>`    | DirectOperate — `{"type": "binary_output"\|"analogue_output", "index": int, "value": number}` |

### Test suite

```bash
pytest tests/unit/          # no Docker required — pydnp3 mocked via conftest.py
pytest tests/integration/   # requires `docker compose up -d`; auto-skips if stack not running
```

→ Architecture + data mapping: `.claude/doc/dnp3-architecture.md`
→ Config JSON format: `.claude/doc/solar-plant-config.md`
→ Attack support requirements (Activity 2): `.claude/doc/attack-support.md`
→ Library selection + Docker constraints: `.claude/doc/library-notes.md`

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
- **Network topology**: `config/solar_plant` currently runs on a single `vlan_ot`
  network (192.168.0.0/24) — scada master, both outstations, HIL, and UI.
  The two-network `vlan_it`/`vlan_ot` split for `attacker` + IT→OT lateral movement
  is planned for Activity 2, not yet implemented.
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
