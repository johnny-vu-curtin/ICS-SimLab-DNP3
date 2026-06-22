import time
import math
import random
import numpy as np
from threading import Thread

# Author: Van Sanh Vu, Purpose: DNP3 development
# Solar plant HIL logic — 500 kW plant, 5 x 100 kW inverters sharing one HIL profile
# (each outstation in dnp3_outstations reads the same physical_value tables — same
# simplification as the 2-inverter config/solar_plant baseline; per-inverter
# independent weather/electrical models are not in scope for this scenario).
#
# Physical model (per-inverter, scaled for 100 kW peak):
#   irradiance  — sinusoidal 24 h cycle (peak at solar noon) + Gaussian noise σ=5 W/m²
#   temperature — ambient 25 °C base, rises ~30 °C at peak irradiance
#   active_power — irradiance × panel efficiency × area, capped by power_curtailment,
#                  zeroed when inverter_enable=False. Area sized so 1000 W/m² peak
#                  irradiance × 18 % efficiency × 556 m² ≈ 100 kW.
#   voltage_ac  — 230 V nominal + random walk (σ=1 V/step, clamped ±10 V)
#   current_ac  — derived from power / voltage
#   frequency   — 50 Hz ± 0.02 Hz Gaussian noise (inverter output)
#   inverter_status — True when active_power exceeds the inverter's minimum
#                      grid-connect threshold (INVERTER_ONLINE_THRESHOLD_W)
#   fault_alarm — True when voltage deviates > 8 V from nominal
#
#   grid_connected — point-of-common-coupling breaker status (steady True; no
#                    disconnect simulation in this scenario)
#   grid_voltage   — 400 V LV three-phase bus at PCC, ± Gaussian noise σ=2 V
#   grid_frequency — 50 Hz grid reference, ± Gaussian noise σ=0.01 Hz (tighter than
#                    inverter-side frequency since grid is the stiffer reference)
#
# Noise is intentional: flat/deterministic values make anomalies trivially detectable,
# defeating IDS evaluation (attack #9 requirement).


PANEL_EFFICIENCY   = 0.18    # 18 % monocrystalline
PANEL_AREA_M2      = 556.0   # sized for ~100 kW peak per inverter: 1000 * 0.18 * 556 ≈ 100 kW
NOMINAL_VOLTAGE    = 230.0
NOMINAL_FREQ       = 50.0
NOMINAL_GRID_VOLTAGE = 400.0  # LV three-phase bus at point of common coupling
NOMINAL_GRID_FREQ    = 50.0
CYCLE_SECONDS      = 60      # 1 full day cycle in 60 seconds (demo visibility)
INVERTER_ONLINE_THRESHOLD_W = 20.0   # real string inverters need a minimum DC bus
                                      # power before grid-connecting (anti-islanding/
                                      # startup check) — avoids flickering "online" at
                                      # dawn/dusk on every tiny noise fluctuation


# PURPOSE: Safely coerces a value to float, falling back to a default on failure
def _safe_float(val, default):
    try:
        return float(val)
    except (TypeError, ValueError):
        return float(default)


# PURPOSE: Initialises HIL physical values and starts the irradiance/electrical/grid sim threads
def logic(physical_values):
    physical_values["voltage_ac"]        = NOMINAL_VOLTAGE
    physical_values["current_ac"]        = 0.0
    physical_values["active_power"]      = 0.0
    physical_values["frequency"]         = NOMINAL_FREQ
    physical_values["solar_irradiance"]  = 0.0
    physical_values["panel_temperature"] = 25.0
    physical_values["inverter_status"]   = False
    physical_values["fault_alarm"]       = False
    physical_values["grid_connected"]    = True
    physical_values["grid_voltage"]      = NOMINAL_GRID_VOLTAGE
    physical_values["grid_frequency"]    = NOMINAL_GRID_FREQ
    # hil.py pre-initialises all physical_values to "" before calling logic(),
    # so setdefault does nothing. Explicitly set safe defaults for inputs.
    physical_values["inverter_enable"]   = 1
    physical_values["power_curtailment"] = 100.0

    Thread(target=_irradiance_sim, args=(physical_values,), daemon=True).start()
    Thread(target=_electrical_sim, args=(physical_values,), daemon=True).start()
    Thread(target=_grid_sim,       args=(physical_values,), daemon=True).start()


# PURPOSE: Simulates a 24h solar irradiance/temperature cycle in a background thread
def _irradiance_sim(pv):
    # Map cycle position (0..CYCLE_SECONDS) → hour of day (0..24) regardless of cycle length.
    # Peak irradiance at hour=12, zero outside 6 h – 18 h window.
    while True:
        t = time.time()
        hour = (t % CYCLE_SECONDS) / CYCLE_SECONDS * 24.0

        # Bell curve centred at noon (hour=12), width σ≈2 h → realistic daylight envelope
        raw = 1000.0 * math.exp(-0.5 * ((hour - 12.0) / 2.5) ** 2)
        if hour < 6.0 or hour > 18.0:
            # Night: no photovoltaic effect without light — exactly 0, no noise.
            # (A real irradiance sensor reads a flat near-zero floor at night;
            # applying daytime noise here would fabricate phantom power after dark.)
            irradiance = 0.0
        else:
            noise = random.gauss(0, 5.0)  # σ = 5 W/m²
            irradiance = max(0.0, raw + noise)
        pv["solar_irradiance"] = round(irradiance, 2)

        # Temperature: ambient 25 °C + proportional rise from irradiance
        pv["panel_temperature"] = round(25.0 + (irradiance / 1000.0) * 30.0 + random.gauss(0, 0.3), 2)

        time.sleep(1.0)


# PURPOSE: Simulates inverter electrical output (power/voltage/current/frequency/fault),
#          sized for ~100 kW peak per inverter
def _electrical_sim(pv):
    voltage = NOMINAL_VOLTAGE
    while True:
        irradiance      = pv["solar_irradiance"]
        inverter_enable = bool(int(_safe_float(pv.get("inverter_enable", 1), 1)))
        curtailment_pct = _safe_float(pv.get("power_curtailment", 100.0), 100.0)
        curtailment_pct = max(0.0, min(100.0, curtailment_pct))

        # Active power from solar model (sized for ~100 kW peak per inverter)
        gross_power = irradiance * PANEL_EFFICIENCY * PANEL_AREA_M2  # Watts
        curtailed   = gross_power * (curtailment_pct / 100.0)
        active_power = curtailed if inverter_enable else 0.0

        # Voltage: random walk, clamped ±10 V from nominal
        voltage += random.gauss(0, 1.0)
        voltage  = max(NOMINAL_VOLTAGE - 10.0, min(NOMINAL_VOLTAGE + 10.0, voltage))

        # Current derived from power
        current = (active_power / voltage) if voltage > 0 else 0.0

        # Frequency: Gaussian noise around 50 Hz
        frequency = NOMINAL_FREQ + random.gauss(0, 0.02)

        # Status and fault
        inverter_status = active_power > INVERTER_ONLINE_THRESHOLD_W
        fault_alarm     = abs(voltage - NOMINAL_VOLTAGE) > 8.0

        pv["active_power"]    = round(active_power, 2)
        pv["voltage_ac"]      = round(voltage, 3)
        pv["current_ac"]      = round(current, 4)
        pv["frequency"]       = round(frequency, 4)
        pv["inverter_status"] = inverter_status
        pv["fault_alarm"]     = fault_alarm

        time.sleep(1.0)


# PURPOSE: Simulates point-of-common-coupling grid measurements (connected/voltage/frequency)
def _grid_sim(pv):
    # Point-of-common-coupling measurements — independent of inverter-side
    # voltage_ac/frequency above, modelling the stiffer LV grid bus.
    while True:
        pv["grid_connected"] = True
        pv["grid_voltage"]   = round(NOMINAL_GRID_VOLTAGE + random.gauss(0, 2.0), 3)
        pv["grid_frequency"] = round(NOMINAL_GRID_FREQ + random.gauss(0, 0.01), 4)

        time.sleep(1.0)
