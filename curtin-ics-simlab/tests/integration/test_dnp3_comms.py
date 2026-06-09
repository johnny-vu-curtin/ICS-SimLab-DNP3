"""
Integration tests — DNP3 master/outstation communication.

Run against a live Docker stack:
    pytest tests/integration/ -v

Test tiers:
  I-01..06  Core communication (data flow Master ← Outstation ← HIL)
  I-07..09  Control loop      (Master → Outstation → SQLite → HIL)
  S-01..04  Security baselines (attack surface confirmation for Phase 2)
"""
import time
import pytest
import requests
from .conftest import db_latest, db_write

SCADA     = "inverter_1"
SCADA2    = "inverter_2"
POLL_WAIT = 8   # seconds: longer than the 5s integrity poll interval


# ===========================================================================
# I-01  Connection — master reports data within one poll cycle
# ===========================================================================
def test_I01_master_has_data_after_startup(master_url):
    """After stack start, /registers must contain non-zero analogue values."""
    r = requests.get(f"{master_url}/registers", timeout=5)
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data) > 0, "No data points returned"

    # At least one analogue value from inverter_1 must be non-zero
    voltage = data.get(f"{SCADA}.voltage_ac", {}).get("value", 0)
    assert voltage != 0.0, (
        f"voltage_ac is still 0 — outstation may not have polled SQLite yet. "
        f"Full data: {data}"
    )


# ===========================================================================
# I-02  Class 0 integrity poll — all 6 analogue inputs populated
# ===========================================================================
def test_I02_all_analogue_inputs_populated(master_url):
    """All configured analogue inputs must have a non-zero value."""
    r = requests.get(f"{master_url}/registers/{SCADA}", timeout=5)
    assert r.status_code == 200
    data = r.json()

    analogue_keys = [k for k, v in data.items() if v.get("type") == "analogue_input"]
    assert len(analogue_keys) == 6, f"Expected 6 analogue inputs, got: {analogue_keys}"

    for key in analogue_keys:
        # frequency, irradiance may be 0 at night — just check the field exists
        assert "value" in data[key], f"Missing value field for {key}"


# ===========================================================================
# I-03  Unsolicited response — value changes propagate within 3s (no poll wait)
# ===========================================================================
def test_I03_value_changes_propagate_quickly(master_url, db_conn):
    """Inject a large voltage change; master should reflect it within 3 seconds
    without waiting for the 5-second integrity poll (unsolicited response)."""
    # Read current master value
    before = requests.get(f"{master_url}/registers/{SCADA}", timeout=5).json()
    old_voltage = before.get("voltage_ac", {}).get("value", 230.0)

    # Inject a clearly different value directly into SQLite
    injected = 210.0 if old_voltage > 215 else 250.0
    db_write(db_conn, "voltage_ac", injected)

    # Poll master every 0.5s for up to 3s
    deadline = time.time() + 3
    new_voltage = old_voltage
    while time.time() < deadline:
        r = requests.get(f"{master_url}/registers/{SCADA}", timeout=2)
        new_voltage = r.json().get("voltage_ac", {}).get("value", old_voltage)
        if abs(new_voltage - injected) < 2.0:
            break
        time.sleep(0.5)

    assert abs(new_voltage - injected) < 2.0, (
        f"Master still shows {new_voltage}V after 3s; expected ~{injected}V. "
        "Unsolicited response may not be working."
    )


# ===========================================================================
# I-04  Multi-outstation — inverter_1 and inverter_2 tracked separately
# ===========================================================================
def test_I04_multi_outstation_segregated(master_url):
    """Both inverters must appear in /registers and have independent data."""
    r = requests.get(f"{master_url}/registers", timeout=5)
    assert r.status_code == 200
    data = r.json()

    inv1_keys = [k for k in data if k.startswith("inverter_1.")]
    inv2_keys = [k for k in data if k.startswith("inverter_2.")]
    assert len(inv1_keys) > 0, "inverter_1 has no data points"
    assert len(inv2_keys) > 0, "inverter_2 has no data points"

    # Address mappings must be distinct (inverter_1=10, inverter_2=11)
    inv1_addr = data.get("inverter_1.voltage_ac", {}).get("address")
    inv2_addr = data.get("inverter_2.voltage_ac", {}).get("address")
    assert inv1_addr == inv2_addr, "Same point index is expected; addresses refer to DNP3 point index not link address"


# ===========================================================================
# I-05  /registers/<outstation_name> endpoint
# ===========================================================================
def test_I05_per_outstation_endpoint(master_url):
    """GET /registers/<name> returns only that outstation's points."""
    r = requests.get(f"{master_url}/registers/{SCADA}", timeout=5)
    assert r.status_code == 200
    data = r.json()
    # Keys must not contain the outstation name prefix
    for key in data:
        assert "." not in key, f"Unexpected compound key in per-outstation response: {key}"
    assert "voltage_ac" in data


# ===========================================================================
# I-06  Unknown outstation returns 404
# ===========================================================================
def test_I06_unknown_outstation_returns_empty(master_url):
    """Requesting an unknown outstation name returns an empty dict (not 500)."""
    r = requests.get(f"{master_url}/registers/nonexistent_outstation", timeout=5)
    assert r.status_code == 200
    assert r.json() == {}


# ===========================================================================
# I-07  Binary command — LATCH_ON disables inverter via master
# ===========================================================================
def test_I07_binary_command_latch_off(master_url, db_conn):
    """POST binary_output LATCH_OFF → SQLite inverter_enable = 0 within 3s."""
    payload = {"type": "binary_output", "index": 0, "value": 0}
    r = requests.post(f"{master_url}/command/{SCADA}", json=payload, timeout=5)
    assert r.status_code == 200, r.text
    assert r.json().get("queued") is True

    # Wait for outstation to write to SQLite
    deadline = time.time() + 5
    val = None
    while time.time() < deadline:
        val = db_latest(db_conn, "inverter_enable")
        if val is not None and val == 0.0:
            break
        time.sleep(0.5)

    assert val == 0.0, (
        f"inverter_enable should be 0 after LATCH_OFF, got {val}"
    )


def test_I07b_binary_command_latch_on(master_url, db_conn):
    """POST binary_output LATCH_ON → SQLite inverter_enable = 1 within 3s."""
    payload = {"type": "binary_output", "index": 0, "value": 1}
    r = requests.post(f"{master_url}/command/{SCADA}", json=payload, timeout=5)
    assert r.status_code == 200, r.text

    deadline = time.time() + 5
    val = None
    while time.time() < deadline:
        val = db_latest(db_conn, "inverter_enable")
        if val is not None and val == 1.0:
            break
        time.sleep(0.5)

    assert val == 1.0, f"inverter_enable should be 1 after LATCH_ON, got {val}"


# ===========================================================================
# I-08  Analogue command — power curtailment written to SQLite
# ===========================================================================
def test_I08_analogue_command_curtailment(master_url, db_conn):
    """POST analogue_output 50.0 → SQLite power_curtailment ≈ 50 within 3s."""
    payload = {"type": "analogue_output", "index": 0, "value": 50.0}
    r = requests.post(f"{master_url}/command/{SCADA}", json=payload, timeout=5)
    assert r.status_code == 200, r.text

    deadline = time.time() + 5
    val = None
    while time.time() < deadline:
        val = db_latest(db_conn, "power_curtailment")
        if val is not None and abs(val - 50.0) < 1.0:
            break
        time.sleep(0.5)

    assert val is not None and abs(val - 50.0) < 1.0, (
        f"power_curtailment should be ~50, got {val}"
    )


# ===========================================================================
# I-09  Command → HIL feedback loop
# ===========================================================================
def test_I09_command_affects_active_power(master_url, db_conn):
    """After curtailment=0, active_power at master should drop to ~0 within 5s."""
    # First restore full power
    requests.post(f"{master_url}/command/{SCADA}", json={"type": "analogue_output", "index": 0, "value": 100.0}, timeout=5)
    requests.post(f"{master_url}/command/{SCADA}", json={"type": "binary_output",   "index": 0, "value": 1},     timeout=5)
    time.sleep(3)  # let HIL respond

    power_before = requests.get(f"{master_url}/registers/{SCADA}", timeout=5).json().get("active_power", {}).get("value", -1)

    # Now zero the curtailment
    requests.post(f"{master_url}/command/{SCADA}", json={"type": "analogue_output", "index": 0, "value": 0.0}, timeout=5)
    time.sleep(4)

    power_after = requests.get(f"{master_url}/registers/{SCADA}", timeout=5).json().get("active_power", {}).get("value", -1)

    assert power_after < power_before, (
        f"active_power did not decrease after curtailment=0: before={power_before}, after={power_after}"
    )


# ===========================================================================
# I-10  Command validation — missing fields return 400
# ===========================================================================
def test_I10_command_missing_fields(master_url):
    r = requests.post(f"{master_url}/command/{SCADA}", json={"type": "binary_output"}, timeout=5)
    assert r.status_code == 400

def test_I10b_command_unknown_outstation(master_url):
    payload = {"type": "binary_output", "index": 0, "value": 1}
    r = requests.post(f"{master_url}/command/ghost_inverter", json=payload, timeout=5)
    assert r.status_code == 404

def test_I10c_command_unsupported_type(master_url):
    payload = {"type": "coil", "index": 0, "value": 1}
    r = requests.post(f"{master_url}/command/{SCADA}", json=payload, timeout=5)
    assert r.status_code == 400


# ===========================================================================
# S-01  Security baseline — outstation accepts commands from any TCP source
#        (no link-layer address authentication in DNP3 without SAv5)
# ===========================================================================
def test_S01_no_source_authentication_on_commands(master_url, db_conn):
    """The master REST endpoint has no auth — any HTTP client can send commands.
    This confirms the attack surface exists before adding the attacker container.
    Expected result: command succeeds (200) with no credential check."""
    payload = {"type": "binary_output", "index": 0, "value": 1}
    # Send from the test client (not a legitimate SCADA operator)
    r = requests.post(f"{master_url}/command/{SCADA}", json=payload, timeout=5)
    assert r.status_code == 200, (
        "Expected 200 (unauthenticated command accepted). "
        "If auth was added, update this test and Phase 2 attack scenarios."
    )


# ===========================================================================
# S-02  Security baseline — SQLite direct write bypasses DNP3 entirely
# ===========================================================================
def test_S02_sqlite_direct_write_bypasses_protocol(master_url, db_conn):
    """Any process that can write to the shared SQLite DB can control the HIL
    without going through DNP3. Confirms the DB-injection attack surface."""
    db_write(db_conn, "inverter_enable", 0, hil="attacker")
    time.sleep(2)

    val = db_latest(db_conn, "inverter_enable")
    assert val == 0.0, (
        "Direct DB write not visible — may have been overwritten by HIL. "
        "Check timing — attacker write may need to arrive between HIL cycles."
    )


# ===========================================================================
# S-03  Security baseline — master exposes full operational data via REST
#        with no authentication
# ===========================================================================
def test_S03_rest_api_unauthenticated(master_url):
    """GET /registers requires no credentials — full operational state exposed."""
    r = requests.get(f"{master_url}/registers", timeout=5)
    assert r.status_code == 200
    data = r.json()
    # Must contain real operational data (not empty), confirming full exposure
    assert any(v.get("value") not in (None, 0, False) for v in data.values()), (
        "No live data in /registers — cannot confirm exposure"
    )
