"""
Mock pydnp3 at sys.modules level before any module under test is imported.
This allows unit tests to run on the host without dnp3-python installed.

The mock provides:
  - Real base classes for SolarCommandHandler, SolarSOEHandler, etc. to inherit
  - Concrete ControlCode / CommandStatus constants for value comparisons
  - Stub ControlRelayOutputBlock and AnalogOutput* so isinstance() dispatch works
"""
import sys
from unittest.mock import MagicMock


def _build_pydnp3_mock():
    mock = MagicMock()
    opendnp3 = mock.opendnp3

    # --- Base classes (must be real Python classes for inheritance) ----------
    class _Base:
        def __init__(self):
            pass

    opendnp3.ICommandHandler    = _Base
    opendnp3.ISOEHandler        = _Base
    opendnp3.IMasterApplication = _Base
    opendnp3.IOutstationApplication = _Base
    opendnp3.ICommandCallback   = _Base

    # --- ControlCode constants -----------------------------------------------
    class _ControlCode:
        LATCH_ON  = "LATCH_ON"
        LATCH_OFF = "LATCH_OFF"
        CLOSE     = "CLOSE"
        TRIP      = "TRIP"
        NUL       = "NUL"

    opendnp3.ControlCode = _ControlCode

    # --- CommandStatus constants ----------------------------------------------
    class _CommandStatus:
        SUCCESS      = "SUCCESS"
        NOT_SUPPORTED = "NOT_SUPPORTED"
        FORMAT_ERROR  = "FORMAT_ERROR"

    opendnp3.CommandStatus = _CommandStatus

    # --- Command types (real classes so isinstance dispatch works) -----------
    class ControlRelayOutputBlock:
        def __init__(self, functionCode=_ControlCode.NUL, tcc=None,
                     clear=False, count=1, onTimeMS=100, offTimeMS=100):
            self.functionCode = functionCode

    class AnalogOutputInt16:
        def __init__(self, value=0):
            self.value = value

    class AnalogOutputInt32:
        def __init__(self, value=0):
            self.value = value

    class AnalogOutputFloat32:
        def __init__(self, value=0.0):
            self.value = value

    class AnalogOutputDouble64:
        def __init__(self, value=0.0):
            self.value = value

    opendnp3.ControlRelayOutputBlock = ControlRelayOutputBlock
    opendnp3.AnalogOutputInt16       = AnalogOutputInt16
    opendnp3.AnalogOutputInt32       = AnalogOutputInt32
    opendnp3.AnalogOutputFloat32     = AnalogOutputFloat32
    opendnp3.AnalogOutputDouble64    = AnalogOutputDouble64

    return mock


_pydnp3_mock = _build_pydnp3_mock()
sys.modules["pydnp3"]           = _pydnp3_mock
sys.modules["pydnp3.opendnp3"]  = _pydnp3_mock.opendnp3
sys.modules["pydnp3.asiodnp3"]  = _pydnp3_mock.asiodnp3
sys.modules["pydnp3.asiopal"]   = _pydnp3_mock.asiopal
sys.modules["pydnp3.openpal"]   = _pydnp3_mock.openpal
