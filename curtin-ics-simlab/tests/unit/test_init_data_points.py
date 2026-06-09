"""
Unit tests for init_data_points() in both master and outstation.
Verifies that config → internal _data_points mapping is correct.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src/components"))
import dnp3_master    as mst
import dnp3_outstation as ost


OUTSTATION_CONFIG = {
    "analogue_inputs":  [
        {"index": 0, "physical_value": "voltage_ac",       "deadband": 1.0},
        {"index": 1, "physical_value": "current_ac",       "deadband": 0.1},
        {"index": 2, "physical_value": "active_power",     "deadband": 10.0},
        {"index": 3, "physical_value": "frequency",        "deadband": 0.05},
        {"index": 4, "physical_value": "solar_irradiance", "deadband": 5.0},
        {"index": 5, "physical_value": "panel_temperature","deadband": 0.5},
    ],
    "binary_inputs": [
        {"index": 0, "physical_value": "inverter_status"},
        {"index": 1, "physical_value": "fault_alarm"},
    ],
    "binary_outputs": [
        {"index": 0, "physical_value": "inverter_enable"},
    ],
    "analogue_outputs": [
        {"index": 0, "physical_value": "power_curtailment"},
    ],
}

MASTER_CONFIG = {
    "master_address": 1,
    "outstations": [
        {"name": "inverter_1", "ip": "192.168.0.20", "outstation_address": 10, "port": 20000},
        {"name": "inverter_2", "ip": "192.168.0.21", "outstation_address": 11, "port": 20000},
    ],
    "poll_interval_s": 5,
}

OUTSTATION_DETAILS = [
    {"name": "inverter_1", **OUTSTATION_CONFIG},
    {"name": "inverter_2", **OUTSTATION_CONFIG},
]


# ---------------------------------------------------------------------------
# Outstation init_data_points
# ---------------------------------------------------------------------------
class TestOutstationInitDataPoints:
    def setup_method(self):
        ost._data_points.clear()
        ost.init_data_points(OUTSTATION_CONFIG)

    def test_all_analogue_inputs_present(self):
        for ai in OUTSTATION_CONFIG["analogue_inputs"]:
            assert ai["physical_value"] in ost._data_points

    def test_analogue_input_type_and_address(self):
        entry = ost._data_points["voltage_ac"]
        assert entry["type"]    == "analogue_input"
        assert entry["address"] == 0
        assert entry["value"]   == 0.0

    def test_all_binary_inputs_present(self):
        for bi in OUTSTATION_CONFIG["binary_inputs"]:
            assert bi["physical_value"] in ost._data_points

    def test_binary_input_initial_value(self):
        assert ost._data_points["inverter_status"]["value"] is False

    def test_binary_output_present(self):
        assert "inverter_enable" in ost._data_points
        assert ost._data_points["inverter_enable"]["type"] == "binary_output"

    def test_analogue_output_present(self):
        assert "power_curtailment" in ost._data_points
        assert ost._data_points["power_curtailment"]["type"] == "analogue_output"

    def test_total_point_count(self):
        expected = (
            len(OUTSTATION_CONFIG["analogue_inputs"]) +
            len(OUTSTATION_CONFIG["binary_inputs"]) +
            len(OUTSTATION_CONFIG["binary_outputs"]) +
            len(OUTSTATION_CONFIG["analogue_outputs"])
        )
        assert len(ost._data_points) == expected


# ---------------------------------------------------------------------------
# Master init_data_points
# ---------------------------------------------------------------------------
class TestMasterInitDataPoints:
    def setup_method(self):
        mst._data_points.clear()
        mst.init_data_points(MASTER_CONFIG, OUTSTATION_DETAILS)

    def test_both_outstations_present(self):
        assert "inverter_1" in mst._data_points
        assert "inverter_2" in mst._data_points

    def test_inverter_1_has_all_analogue_inputs(self):
        for ai in OUTSTATION_CONFIG["analogue_inputs"]:
            assert ai["physical_value"] in mst._data_points["inverter_1"]

    def test_inverter_1_address_mapping(self):
        entry = mst._data_points["inverter_1"]["voltage_ac"]
        assert entry["address"] == 0
        assert entry["type"]    == "analogue_input"

    def test_both_outstations_independent(self):
        # Mutating inverter_1 must not affect inverter_2
        mst._data_points["inverter_1"]["voltage_ac"]["value"] = 999.0
        assert mst._data_points["inverter_2"]["voltage_ac"]["value"] != 999.0

    def test_missing_outstation_detail_gives_empty_dict(self):
        mst._data_points.clear()
        config_with_unknown = {
            "master_address": 1,
            "outstations": [{"name": "unknown_outstation", "ip": "10.0.0.1",
                              "outstation_address": 99, "port": 20000}],
        }
        mst.init_data_points(config_with_unknown, OUTSTATION_DETAILS)
        assert mst._data_points.get("unknown_outstation") == {}
