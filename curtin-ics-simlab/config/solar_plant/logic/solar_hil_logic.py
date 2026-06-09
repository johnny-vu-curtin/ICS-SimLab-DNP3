import time
import math
import random
import numpy as np
from threading import Thread

# Solar plant HIL logic.
#
# Physical model:
#   irradiance  — sinusoidal 24 h cycle (peak at solar noon) + Gaussian noise σ=5 W/m²
#   temperature — ambient 25 °C base, rises ~30 °C at peak irradiance
#   active_power — irradiance × panel efficiency × area, capped by power_curtailment,
#                  zeroed when inverter_enable=False
#   voltage_ac  — 230 V nominal + random walk (σ=1 V/step, clamped ±10 V)
#   current_ac  — derived from power / voltage
#   frequency   — 50 Hz ± 0.02 Hz Gaussian noise
#   inverter_status — True when active_power > 0
#   fault_alarm — True when voltage deviates > 8 V from nominal
#
# Noise is intentional: flat/deterministic values make anomalies trivially detectable,
# defeating IDS evaluation (attack #9 requirement).


PANEL_EFFICIENCY = 0.18   # 18 % monocrystalline
PANEL_AREA_M2    = 50.0   # total panel area per inverter
NOMINAL_VOLTAGE  = 230.0
NOMINAL_FREQ     = 50.0
CYCLE_SECONDS    = 60     # 1 full day cycle in 60 seconds (demo visibility)


def _safe_float(val, default):
    try:
        return float(val)
    except (TypeError, ValueError):
        return float(default)


def logic(physical_values):
    physical_values["voltage_ac"]        = NOMINAL_VOLTAGE
    physical_values["current_ac"]        = 0.0 # ample
    physical_values["active_power"]      = 0.0
    physical_values["frequency"]         = NOMINAL_FREQ
    physical_values["solar_irradiance"]  = 0.0
    physical_values["panel_temperature"] = 25.0
    physical_values["inverter_status"]   = False
    physical_values["fault_alarm"]       = False
    # hil.py pre-initialises all physical_values to "" before calling logic(),
    # so setdefault does nothing. Explicitly set safe defaults for inputs.
    physical_values["inverter_enable"]   = 1
    physical_values["power_curtailment"] = 100.0

    Thread(target=_irradiance_sim,  args=(physical_values,), daemon=True).start()
    Thread(target=_electrical_sim,  args=(physical_values,), daemon=True).start()


def _irradiance_sim(pv):
    # Map cycle position (0..CYCLE_SECONDS) → hour of day (0..24) regardless of cycle length.
    # Peak irradiance at hour=12, zero outside 6 h – 18 h window.
    while True:
        t = time.time()
        hour = (t % CYCLE_SECONDS) / CYCLE_SECONDS * 24.0

        # Bell curve centred at noon (hour=12), width σ≈2 h → realistic daylight envelope
        raw = 1000.0 * math.exp(-0.5 * ((hour - 12.0) / 2.5) ** 2)
        # Night: force zero when < 6 h or > 18 h
        if hour < 6.0 or hour > 18.0:
            raw = 0.0

        noise = random.gauss(0, 5.0)  # σ = 5 W/m²
        irradiance = max(0.0, raw + noise)
        pv["solar_irradiance"] = round(irradiance, 2)

        # Temperature: ambient 25 °C + proportional rise from irradiance
        pv["panel_temperature"] = round(25.0 + (irradiance / 1000.0) * 30.0 + random.gauss(0, 0.3), 2)

        time.sleep(1.0)


def _electrical_sim(pv):
    voltage = NOMINAL_VOLTAGE
    while True:
        irradiance      = pv["solar_irradiance"]
        inverter_enable = bool(int(_safe_float(pv.get("inverter_enable", 1), 1)))
        curtailment_pct = _safe_float(pv.get("power_curtailment", 100.0), 100.0)
        curtailment_pct = max(0.0, min(100.0, curtailment_pct))

        # Active power from solar model
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
        inverter_status = active_power > 0.0
        fault_alarm     = abs(voltage - NOMINAL_VOLTAGE) > 8.0

        pv["active_power"]    = round(active_power, 2)
        pv["voltage_ac"]      = round(voltage, 3)
        pv["current_ac"]      = round(current, 4)
        pv["frequency"]       = round(frequency, 4)
        pv["inverter_status"] = inverter_status
        pv["fault_alarm"]     = fault_alarm

        time.sleep(1.0)
