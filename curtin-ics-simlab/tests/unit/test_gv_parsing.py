"""
Unit tests for _parse_gv() in dnp3_master.py.
No DNP3 library required — tests pure string parsing logic.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src/components"))
from dnp3_master import _parse_gv


# ---------------------------------------------------------------------------
# Normal GroupVariation strings (long form from pydnp3 repr)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("gv_str, expected_type, expected_float", [
    # Analogue static
    ("GroupVariation.Group30Var1",  "analogue_input", True),
    ("GroupVariation.Group30Var2",  "analogue_input", True),
    ("GroupVariation.Group30Var5",  "analogue_input", True),
    # Analogue events
    ("GroupVariation.Group32Var1",  "analogue_input", True),
    ("GroupVariation.Group32Var6",  "analogue_input", True),
    # Binary static
    ("GroupVariation.Group1Var1",   "binary_input",   False),
    ("GroupVariation.Group1Var2",   "binary_input",   False),
    # Binary events
    ("GroupVariation.Group2Var1",   "binary_input",   False),
    # Binary output status
    ("GroupVariation.Group10Var2",  "binary_output",  False),
    # Analogue output status
    ("GroupVariation.Group40Var1",  "analogue_output", True),
    ("GroupVariation.Group42Var5",  "analogue_output", True),
])
def test_parse_gv_long_form(gv_str, expected_type, expected_float):
    data_type, is_float = _parse_gv(gv_str)
    assert data_type == expected_type
    assert is_float  == expected_float


# ---------------------------------------------------------------------------
# Short form — no leading "GroupVariation." prefix
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("gv_str, expected_type", [
    ("Group30Var1",  "analogue_input"),
    ("Group1Var2",   "binary_input"),
    ("Group10Var2",  "binary_output"),
    ("Group40Var1",  "analogue_output"),
])
def test_parse_gv_short_form(gv_str, expected_type):
    data_type, _ = _parse_gv(gv_str)
    assert data_type == expected_type


# ---------------------------------------------------------------------------
# Unknown / malformed inputs return (None, None) without raising
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_input", [
    "GroupVariation.Group99Var1",   # unrecognised group number
    "Group99Var1",
    "NotAGroup",
    "",
    "GroupVariation.",
    "Group",
    None,
    42,
])
def test_parse_gv_unknown_returns_none(bad_input):
    data_type, is_float = _parse_gv(bad_input)
    assert data_type is None
    assert is_float  is None
