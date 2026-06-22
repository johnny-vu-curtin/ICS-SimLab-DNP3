# Physical Correlation Rules — `config/solar_plant_500kw` (5-inverter, grid-tie)

Source of truth for how physical values in
`config/solar_plant_500kw/logic/solar_hil_logic.py` relate to each other. Read this
before changing any simulation logic in that file. This scenario extends the rules in
[`rules_logic-solar_plant.md`](rules_logic-solar_plant.md) with a grid-tie model
(`grid_connected`/`grid_voltage`/`grid_frequency` at the point of common coupling) —
read that file first for the rules shared with the base scenario (irradiance →
temperature → power chain, temperature derating, current = power/voltage).

## What's different from `config/solar_plant`

The base scenario has no upstream grid reference, so `voltage_ac` and `frequency` were
independent noise processes there (documented as a known simplification). This scenario
adds `grid_voltage`/`grid_frequency`/`grid_connected` at the PCC, so those two values
are now **derived from the grid**, not independent.

## Dependency chain (additions/changes vs. base scenario)

```
grid_voltage (PCC, 400V LV bus) ──> voltage_ac = grid_voltage/√3 + voltage_rise(active_power) + noise
grid_frequency (PCC)            ──> frequency  = grid_frequency + small measurement noise
grid_connected (PCC breaker)    ──> inverter_status (AND'd in, alongside power threshold and fault_alarm)
```

## Rules

1. **`grid_voltage → voltage_ac`** — `voltage_ac = grid_voltage / √3 + voltage_rise +
   N(0, 0.5)`. 400V LV three-phase line-to-line divided by √3 gives 230.9V phase
   voltage — matches the 230V nominal almost exactly (standard European LV split).
   `voltage_ac` is **derived**, not an independent random walk — terminal voltage is
   tied to the upstream grid bus it's connected to.

2. **`active_power → voltage_rise` (feeder voltage-rise effect)** —
   `voltage_rise = (active_power / RATED_POWER_W) × FEEDER_VOLTAGE_RISE_V_AT_RATED_POWER`
   (up to +5V at 100kW full export). Real distribution feeders see local voltage rise
   proportional to injected power (high PV penetration is a known cause of feeder
   over-voltage in the real world) — this is a deliberate, if simplified, model of that
   effect. The 5V/100kW coefficient is a modelling assumption, not a measured constant;
   change it if you have better reference data, but keep the proportional relationship.

3. **`grid_frequency → frequency`** — `frequency = grid_frequency + N(0, 0.005)`. A
   grid-tied inverter's PLL (phase-locked loop) synchronises to the grid; it cannot run
   at an independently-drifting frequency. Never decouple this back into independent
   noise — that was the bug this rule fixes (see commit `7a29833`).

4. **`grid_connected → inverter_status`** — `inverter_status = (active_power >
   INVERTER_ONLINE_THRESHOLD_W) AND NOT fault_alarm AND grid_connected`. This plant has
   **no battery storage** (confirmed requirement) — it cannot energise islanded. If
   `grid_connected` ever goes false, every inverter must show offline, full stop.

5. **All other rules from the base scenario still apply unchanged**: irradiance →
   temperature → power chain, temperature derating, `current_ac = active_power /
   voltage_ac`, `fault_alarm` from voltage-or-frequency deviation, `fault_alarm`
   excluding `inverter_status`.

## Constants specific to this scenario

| Constant | Value | Why |
|---|---|---|
| `PANEL_AREA_M2` | 556.0 | Sized for ~100kW peak per inverter: 1000 W/m² × 18% × 556m² ≈ 100kW |
| `RATED_POWER_W` | 100000.0 | Per-inverter rated capacity, scales the voltage-rise effect |
| `FEEDER_VOLTAGE_RISE_V_AT_RATED_POWER` | 5.0 | Max local voltage rise at full 100kW export (typical LV feeder effect, modelling assumption) |
| `NOMINAL_GRID_VOLTAGE` | 400.0 | LV three-phase bus at the point of common coupling |

## Known simplifications NOT yet fixed

- **`grid_connected` is hardcoded `True` forever** — no grid outage is ever simulated,
  so the interlock in rule #4 currently has no observable effect during normal
  operation. This was an explicit decision (see project memory
  `project_solar_plant_500kw.md`) — only revisit if a scenario needs to exercise grid
  outage behaviour (e.g. a future Activity 2 attack script tripping the PCC breaker).
- **Shared HIL across all 5 inverters** — `inverter_1`..`inverter_5` all read the same
  physical_value tables, so they always report identical values. No per-inverter
  independent weather/electrical model (deliberate simplification, same as the base
  scenario's 2-inverter setup).
