# Physical Correlation Rules — `config/solar_plant` (2-inverter)

Source of truth for how physical values in `config/solar_plant/logic/solar_hil_logic.py`
relate to each other. Read this before changing any simulation logic in that file —
the relationships below are deliberate, not incidental.

## Dependency chain

```
solar_irradiance ──┬──> panel_temperature
                    │
                    └──> active_power <── power_curtailment (input, from SCADA)
                              │      <── inverter_enable     (input, from SCADA)
                              │      <── panel_temperature (derate above 25°C)
                              │
              voltage_ac (independent random walk) ──┬──> current_ac (= active_power / voltage_ac)
                                                       │
              frequency (independent Gaussian noise) ─┤
                                                       │
                                                       └──> fault_alarm (voltage OR frequency out of band)
                                                                  │
                                                active_power ─────┴──> inverter_status
```

## Rules

1. **`solar_irradiance → panel_temperature`** — proportional rise, ambient 25°C base, up
   to +30°C at peak irradiance (1000 W/m²). Real PV panels heat up under sunlight.

2. **`solar_irradiance, power_curtailment, inverter_enable → active_power`** —
   `gross_power = irradiance × PANEL_EFFICIENCY × temp_derate × PANEL_AREA_M2`,
   then scaled by `power_curtailment` (%), then zeroed if `inverter_enable` is false.

3. **`panel_temperature → active_power` (derate)** — crystalline-silicon panels lose
   efficiency above 25°C reference: `temp_derate = 1 - TEMP_COEFF_PCT_PER_C/100 × max(0,
   panel_temperature - 25)`. Do not remove this — without it, a panel can run arbitrarily
   hot with zero effect on output, which is not how PV cells behave.

4. **`active_power, voltage_ac → current_ac`** — `current = active_power / voltage_ac`
   (P = V × I). This is a hard physics relationship; never compute `current_ac`
   independently of `active_power`.

5. **`voltage_ac` — independent random walk** (σ=1V/step, clamped ±10V from 230V
   nominal). **Known simplification**: this scenario has no `grid_voltage`/PCC concept,
   so there is nothing to correlate `voltage_ac` against. If a grid-tie reference is
   added later (see `solar_plant_500kw`'s `rules_logic-solar_plant_500kw.md` for how that
   was done), `voltage_ac` should derive from it the same way, not stay an independent walk.

6. **`frequency` — independent Gaussian noise** (σ=0.02Hz around 50Hz). **Known
   simplification**, same reason as #5 — no `grid_frequency` exists in this scenario to
   lock to. A real grid-tied inverter cannot run at a frequency independent of the grid
   it's synchronised to.

7. **`voltage_ac OR frequency → fault_alarm`** — `fault_alarm = (|voltage_ac - 230| > 8V)
   OR (|frequency - 50| > 0.5Hz)`. Grid code anti-islanding protection trips on either
   condition, not just voltage. Do not narrow this back to voltage-only.

8. **`active_power, fault_alarm → inverter_status`** — `inverter_status = (active_power >
   INVERTER_ONLINE_THRESHOLD_W) AND NOT fault_alarm`. A faulted inverter must not also
   report normal online operation — these two booleans are mutually exclusive by design,
   never compute them independently of each other again.

## Constants and why

| Constant | Value | Why |
|---|---|---|
| `PANEL_EFFICIENCY` | 0.18 | 18% monocrystalline panel, at 25°C reference |
| `TEMP_COEFF_PCT_PER_C` | 0.4 | Typical crystalline-silicon temperature coefficient |
| `INVERTER_ONLINE_THRESHOLD_W` | 20.0 | Real string inverters need minimum DC bus power before grid-connecting (anti-islanding/startup check) |
| `FREQ_FAULT_THRESHOLD_HZ` | 0.5 | Typical grid code over/under-frequency trip band |

## Known simplifications NOT yet fixed (carried over from design discussion)

- All outstations sharing this scenario read from the **same** HIL physical_value
  tables — `inverter_1` and `inverter_2` always report identical values. No per-inverter
  independent weather/electrical model.
- `voltage_ac` and `frequency` have no upstream grid reference to correlate against (see
  rules #5/#6) — this scenario predates the grid-tie modelling added in
  `solar_plant_500kw`. Consider porting that approach here if needed.
