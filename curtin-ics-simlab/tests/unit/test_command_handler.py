"""
Unit tests for SolarCommandHandler (dnp3_outstation.py).
Tests command routing, SQLite writes, and table name validation.
pydnp3 is mocked via conftest.py — no DNP3 library required.
"""
import sys
import os
import sqlite3
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src/components"))
import dnp3_outstation as ost

opendnp3 = sys.modules["pydnp3"].opendnp3
ControlRelayOutputBlock = opendnp3.ControlRelayOutputBlock
AnalogOutputFloat32     = opendnp3.AnalogOutputFloat32
AnalogOutputDouble64    = opendnp3.AnalogOutputDouble64
ControlCode             = opendnp3.ControlCode
CommandStatus           = opendnp3.CommandStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
CONFIGS = {
    "hil": "solar_hil",
    "binary_outputs":  [{"index": 0, "physical_value": "inverter_enable"}],
    "analogue_outputs": [{"index": 0, "physical_value": "power_curtailment"}],
    "analogue_inputs": [],
    "binary_inputs":   [],
}


@pytest.fixture
def db(tmp_path):
    """Minimal SQLite DB with tables matching CONFIGS."""
    path = str(tmp_path / "physical_interactions.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE hils (name TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO hils VALUES ('solar_hil')")
    conn.execute("CREATE TABLE inverter_enable (value TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, hil TEXT)")
    conn.execute("CREATE TABLE power_curtailment (value TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, hil TEXT)")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def handler(db):
    return ost.SolarCommandHandler(CONFIGS, db)


def _last_row(db_path, table):
    conn = sqlite3.connect(db_path)
    row = conn.execute(f"SELECT value, hil FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------------------
# Binary output tests
# ---------------------------------------------------------------------------
def test_latch_on_writes_1(handler, db):
    cmd = ControlRelayOutputBlock(ControlCode.LATCH_ON)
    result = handler._handle_binary_output(0, cmd)
    assert result == CommandStatus.SUCCESS
    row = _last_row(db, "inverter_enable")
    assert row is not None
    assert int(row[0]) == 1
    assert row[1] == "solar_hil"


def test_latch_off_writes_0(handler, db):
    cmd = ControlRelayOutputBlock(ControlCode.LATCH_OFF)
    result = handler._handle_binary_output(0, cmd)
    assert result == CommandStatus.SUCCESS
    row = _last_row(db, "inverter_enable")
    assert int(row[0]) == 0


def test_close_writes_1(handler, db):
    cmd = ControlRelayOutputBlock(ControlCode.CLOSE)
    result = handler._handle_binary_output(0, cmd)
    assert result == CommandStatus.SUCCESS
    row = _last_row(db, "inverter_enable")
    assert int(row[0]) == 1


def test_binary_output_invalid_index(handler):
    cmd = ControlRelayOutputBlock(ControlCode.LATCH_ON)
    result = handler._handle_binary_output(99, cmd)
    assert result == CommandStatus.NOT_SUPPORTED


# ---------------------------------------------------------------------------
# Analogue output tests
# ---------------------------------------------------------------------------
def test_analogue_output_writes_value(handler, db):
    result = handler._handle_analogue_output(0, 75.0)
    assert result == CommandStatus.SUCCESS
    row = _last_row(db, "power_curtailment")
    assert row is not None
    assert float(row[0]) == pytest.approx(75.0)


def test_analogue_output_zero(handler, db):
    result = handler._handle_analogue_output(0, 0.0)
    assert result == CommandStatus.SUCCESS
    row = _last_row(db, "power_curtailment")
    assert float(row[0]) == pytest.approx(0.0)


def test_analogue_output_invalid_index(handler):
    result = handler._handle_analogue_output(99, 50.0)
    assert result == CommandStatus.NOT_SUPPORTED


# ---------------------------------------------------------------------------
# Table name validation (_is_valid_table)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", [
    "inverter_enable",
    "power_curtailment",
    "voltage_ac",
    "_private",
    "a",
    "A1_b2",
])
def test_valid_table_names_accepted(name):
    assert ost._is_valid_table(name) is True


@pytest.mark.parametrize("bad_name", [
    "'; DROP TABLE hils; --",
    "voltage ac",            # space
    "1starts_with_digit",
    "tab\x00le",             # null byte
    "",
    "a" * 65,                # too long (> 64 chars)
    "table-name",            # hyphen
])
def test_invalid_table_names_rejected(bad_name):
    assert ost._is_valid_table(bad_name) is False


def test_write_to_db_rejects_invalid_table(handler, db):
    """_write_to_db must not execute SQL for a malformed table name."""
    handler._write_to_db("'; DROP TABLE hils; --", 1)
    # hils table must still exist
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='hils'").fetchone()
    conn.close()
    assert row is not None
