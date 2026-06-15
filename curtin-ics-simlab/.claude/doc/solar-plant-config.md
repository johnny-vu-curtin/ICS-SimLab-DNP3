# Solar Plant Configuration Format

Reference: `config/solar_plant/configuration.json`

This documents the JSON schema extensions added for DNP3 components.
Existing Modbus fields (`plcs`, `hmis`, `sensors`, `actuators`, `hils`, `serial_networks`) are unchanged — see existing scenarios for reference.

---

## Top-level structure

```json
{
  "ui":              { ... },           // unchanged from Modbus
  "dnp3_masters":    [ ... ],           // SCADA master(s)
  "dnp3_outstations": [ ... ],          // inverters / RTUs
  "hils":            [ ... ],           // unchanged, drives physical values
  "ip_networks":     [ ... ],           // single vlan_ot network (current)
  "serial_networks": []                 // empty for DNP3 (no RTU serial links)
}
```

`"attackers": [ ... ]` is a planned key (Activity 2, not yet implemented) — see
the `attackers` section below.

---

## dnp3_masters

```json
"dnp3_masters": [
  {
    "name": "scada",
    "network": {
      "ip": "192.168.0.10",
      "docker_network": "vlan_ot"
    },
    "master_address": 1,
    "outstations": [
      { "name": "inverter_1", "ip": "192.168.0.20", "outstation_address": 10, "port": 20000 },
      { "name": "inverter_2", "ip": "192.168.0.21", "outstation_address": 11, "port": 20000 }
    ],
    "poll_interval_s": 5,
    "unsolicited": true
  }
]
```

Fields:
- `master_address` — DNP3 link-layer address for the master (typically 1).
- `outstations` — list of outstations the master connects to; addresses must match outstation config.
- `poll_interval_s` — Class 0 integrity poll interval in seconds.
- `unsolicited` — enable/disable unsolicited response reception.

---

## dnp3_outstations

```json
"dnp3_outstations": [
  {
    "name": "inverter_1",
    "network": {
      "ip": "192.168.0.20",
      "docker_network": "vlan_ot"
    },
    "outstation_address": 10,
    "master_address": 1,
    "port": 20000,
    "hil": "solar_hil",
    "analogue_inputs": [
      { "index": 0, "physical_value": "voltage_ac",         "deadband": 1.0  },
      { "index": 1, "physical_value": "current_ac",         "deadband": 0.1  },
      { "index": 2, "physical_value": "active_power",       "deadband": 10.0 },
      { "index": 3, "physical_value": "frequency",          "deadband": 0.05 },
      { "index": 4, "physical_value": "solar_irradiance",   "deadband": 5.0  },
      { "index": 5, "physical_value": "panel_temperature",  "deadband": 0.5  }
    ],
    "binary_inputs": [
      { "index": 0, "physical_value": "inverter_status" },
      { "index": 1, "physical_value": "fault_alarm"     }
    ],
    "binary_outputs": [
      { "index": 0, "physical_value": "inverter_enable" }
    ],
    "analogue_outputs": [
      { "index": 0, "physical_value": "power_curtailment" }
    ]
  }
]
```

Fields:
- `outstation_address` — unique DNP3 link-layer address per inverter.
- `hil` — name of the HIL that drives this outstation's physical values in SQLite.
- `analogue_inputs[].deadband` — value change threshold that triggers a Class 1/2 event.

Multiple inverters: duplicate the object with a different `name`, `ip`, and `outstation_address`.

---

## attackers (planned — Activity 2, not yet implemented)

Not present in the current `config/solar_plant/configuration.json`. Proposed schema:

```json
"attackers": [
  {
    "name": "attacker",
    "network": {
      "ip": "192.168.1.50",
      "docker_network": "vlan_it"
    },
    "targets": [
      { "name": "inverter_1", "ip": "192.168.0.20", "outstation_address": 10 },
      { "name": "inverter_2", "ip": "192.168.0.21", "outstation_address": 11 }
    ],
    "master_address": 99,
    "logic": "attack_logic.py"
  }
]
```

- Attacker starts in `vlan_it`. Attack scripts route to `vlan_ot` targets.
- `logic` — Python file in `config/<scenario>/logic/` containing attack behaviour.
- `master_address` — rogue master address (must differ from legitimate master).
- Requires the two-network `ip_networks` layout shown below. There is no
  `src/setup.py` build function for this component type yet.

---

## ip_networks

### Current (single `vlan_ot` network)

```json
"ip_networks": [
  {
    "docker_name": "vlan_ot",
    "name": "ics_ot_network",
    "subnet": "192.168.0.0/24"
  }
]
```

### Planned (Activity 2 — two networks, for `attackers`)

```json
"ip_networks": [
  {
    "docker_name": "vlan_ot",
    "name": "ics_ot_network",
    "subnet": "192.168.0.0/24"
  },
  {
    "docker_name": "vlan_it",
    "name": "ics_it_network",
    "subnet": "192.168.1.0/24"
  }
]
```

`scada` (dnp3_master) would be assigned IPs on both networks — it bridges IT and OT.

---

## hils — solar plant HIL

```json
"hils": [
  {
    "name": "solar_hil",
    "logic": "solar_hil_logic.py",
    "network": {
      "ip": "192.168.0.30",
      "docker_network": "vlan_ot"
    },
    "physical_values": [
      { "name": "voltage_ac",        "io": "output" },
      { "name": "current_ac",        "io": "output" },
      { "name": "active_power",      "io": "output" },
      { "name": "frequency",         "io": "output" },
      { "name": "solar_irradiance",  "io": "output" },
      { "name": "panel_temperature", "io": "output" },
      { "name": "inverter_status",   "io": "output" },
      { "name": "fault_alarm",       "io": "output" },
      { "name": "inverter_enable",   "io": "input"  },
      { "name": "power_curtailment", "io": "input"  }
    ]
  }
]
```

HIL logic file: `config/solar_plant/logic/solar_hil_logic.py`

The HIL drives `output` values (sensor readings) and reads `input` values (control commands written by the outstation) from SQLite. See `src/components/hil.py` for the runtime loop.
