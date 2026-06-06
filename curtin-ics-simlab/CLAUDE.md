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

## DNP3 additions (in progress)

Three new component types being added:

| Type              | Role              | DNP3 role   |
|-------------------|-------------------|-------------|
| `dnp3_master`     | SCADA controller  | Master      |
| `dnp3_outstation` | Solar inverter    | Outstation  |
| `attacker`        | Attack simulator  | Rogue master|

New scenario: `config/solar_plant/`

→ Architecture + data mapping: `.claude/doc/dnp3-architecture.md`
→ Config JSON format: `.claude/doc/solar-plant-config.md`
→ Attack support requirements: `.claude/doc/attack-support.md`
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

- **DNP3 library**: use `dnp3-python` (VOLTTRON/PNNL) only.
  `pydnp3` is abandoned (last release 2018, requires Python 2.7 headers — will not build).
- **Docker base image for DNP3 containers**: `ubuntu:22.04` + Python 3.10 or 3.11.
  `dnp3-python` has no wheel for Python 3.12 on Ubuntu 24.04.
- **SQLite WAL mode**: must be enabled for DNP3 high-frequency polling.
  Currently commented out at `src/setup.py:622` — uncomment before DNP3 work.
- **Network topology**: DNP3 simulation requires two Docker networks (`vlan_it`, `vlan_ot`).
  Single-network topology cannot model IT→OT lateral movement for attack datasets.
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
