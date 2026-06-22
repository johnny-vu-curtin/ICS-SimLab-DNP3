#!/usr/bin/env python3

# FILE PURPOSE: DNP3 Outstation component — models a solar inverter/RTU.
# Reads physical values from SQLite (written by HIL), exposes them as DNP3
# Analogue/Binary Inputs. Receives control commands (Binary/Analogue Output)
# from a DNP3 Master and writes them back to SQLite for the HIL to consume.
# Also exposes a REST API on port 1111 for the Streamlit dashboard.

import json
import re
import time
import logging
import sqlite3
import threading
from flask import Flask, jsonify

# dnp3-python (pip install dnp3-python) imports as pydnp3 internally
try:
    from pydnp3 import asiodnp3, asiopal, opendnp3, openpal
    DNP3_AVAILABLE = True
except ImportError:
    logging.error("pydnp3 not found — install dnp3-python package")
    DNP3_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)

# Table names come from config physical_value fields. Validate before use in SQL
# to prevent injection if config is ever supplied from an untrusted source.
_VALID_TABLE_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]{0,63}$')


# Author: Van Sanh Vu, Purpose: DNP3 development
# PURPOSE: Validates a physical_value name before using it as a SQL table name
def _is_valid_table(name: str) -> bool:
    return bool(_VALID_TABLE_RE.match(name))

# Shared state — populated by DNP3 callbacks and SQLite reads
_data_points = {}   # { physical_value: {"type": ..., "index": ..., "value": ...} }
_data_lock = threading.Lock()

# Outstation reference for applying updates
_outstation = None


# ---------------------------------------------------------------------------
# DNP3 Command Handler — receives Binary Output and Analogue Output from master
# ---------------------------------------------------------------------------
# Author: Van Sanh Vu, Purpose: DNP3 development
# PURPOSE: Handles incoming Binary/Analogue Output commands from the DNP3 master
class SolarCommandHandler(opendnp3.ICommandHandler):

    def __init__(self, configs, db_path):
        super().__init__()
        self._configs = configs
        self._db_path = db_path
        self._bo_map = {bo["index"]: bo["physical_value"] for bo in configs.get("binary_outputs", [])}
        self._ao_map = {ao["index"]: ao["physical_value"] for ao in configs.get("analogue_outputs", [])}

    def Begin(self):
        pass

    def End(self):
        pass

    def Select(self, command, index):
        return opendnp3.CommandStatus.SUCCESS

    def Operate(self, command, index, op_type):
        if isinstance(command, opendnp3.ControlRelayOutputBlock):
            return self._handle_binary_output(index, command)
        elif isinstance(command, opendnp3.AnalogOutputInt16):
            return self._handle_analogue_output(index, float(command.value))
        elif isinstance(command, opendnp3.AnalogOutputInt32):
            return self._handle_analogue_output(index, float(command.value))
        elif isinstance(command, opendnp3.AnalogOutputFloat32):
            return self._handle_analogue_output(index, float(command.value))
        elif isinstance(command, opendnp3.AnalogOutputDouble64):
            return self._handle_analogue_output(index, float(command.value))
        return opendnp3.CommandStatus.NOT_SUPPORTED

    def _handle_binary_output(self, index, command):
        physical_value = self._bo_map.get(index)
        if physical_value is None:
            return opendnp3.CommandStatus.NOT_SUPPORTED

        code = command.functionCode
        value = 1 if code in (opendnp3.ControlCode.LATCH_ON, opendnp3.ControlCode.CLOSE) else 0

        self._write_to_db(physical_value, value)
        with _data_lock:
            if physical_value in _data_points:
                _data_points[physical_value]["value"] = bool(value)

        logging.info(f"Binary Output [{index}] {physical_value} = {value}")
        return opendnp3.CommandStatus.SUCCESS

    def _handle_analogue_output(self, index, value):
        physical_value = self._ao_map.get(index)
        if physical_value is None:
            return opendnp3.CommandStatus.NOT_SUPPORTED

        self._write_to_db(physical_value, value)
        with _data_lock:
            if physical_value in _data_points:
                _data_points[physical_value]["value"] = value

        logging.info(f"Analogue Output [{index}] {physical_value} = {value}")
        return opendnp3.CommandStatus.SUCCESS

    def _write_to_db(self, table, value):
        if not _is_valid_table(table):
            logging.warning(f"Rejected DB write — invalid table name: {table!r}")
            return
        try:
            conn = sqlite3.connect(self._db_path)
            hil = self._configs.get("hil", "solar_hil")
            conn.execute(f"INSERT INTO {table}(value, hil) VALUES(?, ?)", (value, hil))
            conn.commit()
            conn.close()
        except Exception as e:
            logging.error(f"DB write error ({table}={value}): {e}")


# ---------------------------------------------------------------------------
# DNP3 Outstation Application callbacks
# ---------------------------------------------------------------------------
# Author: Van Sanh Vu, Purpose: DNP3 development
# PURPOSE: Outstation lifecycle callbacks required by opendnp3.IOutstationApplication
class SolarOutstationApplication(opendnp3.IOutstationApplication):

    def SupportsWriteAbsoluteTime(self):
        return True

    def WriteAbsoluteTime(self, time):
        logging.debug(f"Time sync received: {time}")
        return True

    def SupportsAssignClass(self):
        return False

    def MeasurementControl(self, is_restart):
        pass

    def WarmRestartSupport(self):
        return opendnp3.RestartMode.UNSUPPORTED

    def ColdRestartSupport(self):
        return opendnp3.RestartMode.UNSUPPORTED

    def WarmRestart(self):
        return 0xFFFF

    def ColdRestart(self):
        return 0xFFFF


# ---------------------------------------------------------------------------
# SQLite polling — reads physical values and pushes them to DNP3 database
# ---------------------------------------------------------------------------
# Author: Van Sanh Vu, Purpose: DNP3 development
# PURPOSE: Polls SQLite for physical values and pushes deadband-gated updates to DNP3
def poll_sqlite(configs, db_path, outstation, poll_interval=1.0):
    ai_map      = {ai["physical_value"]: ai["index"]               for ai in configs.get("analogue_inputs", [])}
    ai_deadband = {ai["physical_value"]: float(ai.get("deadband", 0.0)) for ai in configs.get("analogue_inputs", [])}
    bi_map      = {bi["physical_value"]: bi["index"]               for bi in configs.get("binary_inputs", [])}

    # Validate all table names once at startup; skip any that are malformed.
    ai_map = {pv: idx for pv, idx in ai_map.items() if _is_valid_table(pv)}
    bi_map = {pv: idx for pv, idx in bi_map.items() if _is_valid_table(pv)}

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 2000;")  # wait up to 2s if hil.py holds the lock

    _last_ai = {}   # pv → last value pushed to DNP3; used for deadband gating

    while True:
        try:
            builder = asiodnp3.UpdateBuilder()
            updated = False

            for pv, idx in ai_map.items():
                row = conn.execute(
                    f"SELECT value FROM {pv} ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
                if row and row[0] not in (None, ""):
                    val = float(row[0])
                    last = _last_ai.get(pv)
                    # Only push update when change exceeds configured deadband
                    if last is None or abs(val - last) >= ai_deadband.get(pv, 0.0):
                        builder.Update(
                            opendnp3.Analog(val, opendnp3.Flags(opendnp3.AnalogQuality.ONLINE)),
                            idx,
                            opendnp3.EventMode.Detect
                        )
                        _last_ai[pv] = val
                        with _data_lock:
                            if pv in _data_points:
                                _data_points[pv]["value"] = round(val, 4)
                        updated = True

            for pv, idx in bi_map.items():
                row = conn.execute(
                    f"SELECT value FROM {pv} ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
                if row and row[0] not in (None, ""):
                    val = bool(int(float(row[0])))
                    builder.Update(
                        opendnp3.Binary(val, opendnp3.Flags(opendnp3.BinaryQuality.ONLINE)),
                        idx,
                        opendnp3.EventMode.Detect
                    )
                    with _data_lock:
                        if pv in _data_points:
                            _data_points[pv]["value"] = val
                    updated = True

            if updated and outstation:
                outstation.Apply(builder.Build())

        except Exception as e:
            logging.error(f"SQLite poll error: {e}")

        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Flask REST API — used by Streamlit dashboard
# ---------------------------------------------------------------------------
# Author: Van Sanh Vu, Purpose: DNP3 development
# PURPOSE: Returns current values for this outstation
@app.route("/registers", methods=["GET"])
def get_registers():
    with _data_lock:
        return jsonify(dict(_data_points))


# Author: Van Sanh Vu, Purpose: DNP3 development
# PURPOSE: Runs the Flask REST API server on port 1111
def run_flask():
    app.run(host="0.0.0.0", port=1111)


# ---------------------------------------------------------------------------
# Setup DNP3 outstation
# ---------------------------------------------------------------------------
# Author: Van Sanh Vu, Purpose: DNP3 development
# PURPOSE: Builds the DNP3-TCP server channel and outstation object from config
def build_outstation(configs):
    global _outstation

    outstation_addr = configs["outstation_address"]
    master_addr = configs["master_address"]
    port = configs.get("port", 20000)

    n_ai = len(configs.get("analogue_inputs", []))
    n_bi = len(configs.get("binary_inputs", []))
    n_bo = len(configs.get("binary_outputs", []))
    n_ao = len(configs.get("analogue_outputs", []))

    manager = asiodnp3.DNP3Manager(1, asiodnp3.ConsoleLogger().Create())

    channel = manager.AddTCPServer(
        "server",
        opendnp3.levels.NORMAL,
        asiopal.ChannelRetry().Default(),
        "0.0.0.0",
        port,
        asiodnp3.PrintingChannelListener().Create()
    )

    db_sizes = opendnp3.DatabaseSizes(
        numBinary=n_bi,
        numDoubleBinary=0,
        numAnalog=n_ai,
        numCounter=0,
        numFrozenCounter=0,
        numBinaryOutputStatus=n_bo,
        numAnalogOutputStatus=n_ao,
        numTimeAndInterval=0
    )

    stack_config = asiodnp3.OutstationStackConfig(db_sizes)
    stack_config.outstation.eventBufferConfig = opendnp3.EventBufferConfig.AllTypes(50)
    stack_config.outstation.params.allowUnsolicited = True
    stack_config.link.LocalAddr = outstation_addr
    stack_config.link.RemoteAddr = master_addr

    db_path = "/src/physical_interactions.db"
    command_handler = SolarCommandHandler(configs, db_path)
    application = SolarOutstationApplication()

    _outstation = channel.AddOutstation(
        "outstation",
        command_handler,
        application,
        stack_config
    )
    _outstation.Enable()

    logging.info(
        f"DNP3 Outstation started — address={outstation_addr}, "
        f"master={master_addr}, port={port}"
    )
    return _outstation, manager


# ---------------------------------------------------------------------------
# Initialise _data_points for REST API
# ---------------------------------------------------------------------------
# Author: Van Sanh Vu, Purpose: DNP3 development
# PURPOSE: Pre-populates _data_points with empty entries for every configured point
def init_data_points(configs):
    with _data_lock:
        for ai in configs.get("analogue_inputs", []):
            _data_points[ai["physical_value"]] = {
                "type": "analogue_input",
                "address": ai["index"],
                "count": 1,
                "value": 0.0
            }
        for bi in configs.get("binary_inputs", []):
            _data_points[bi["physical_value"]] = {
                "type": "binary_input",
                "address": bi["index"],
                "count": 1,
                "value": False
            }
        for bo in configs.get("binary_outputs", []):
            _data_points[bo["physical_value"]] = {
                "type": "binary_output",
                "address": bo["index"],
                "count": 1,
                "value": False
            }
        for ao in configs.get("analogue_outputs", []):
            _data_points[ao["physical_value"]] = {
                "type": "analogue_output",
                "address": ao["index"],
                "count": 1,
                "value": 0.0
            }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
# Author: Van Sanh Vu, Purpose: DNP3 development
# PURPOSE: Loads config, starts the outstation, the SQLite poll loop, and the REST API
def main():
    if not DNP3_AVAILABLE:
        logging.error("Cannot start — dnp3-python not installed")
        return

    with open("config.json") as f:
        configs = json.load(f)

    logging.info(f"Starting DNP3 Outstation: {configs.get('outstation_address')}")

    init_data_points(configs)

    outstation, manager = build_outstation(configs)

    db_path = "/src/physical_interactions.db"
    poll_thread = threading.Thread(
        target=poll_sqlite,
        args=(configs, db_path, outstation),
        daemon=True
    )
    poll_thread.start()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
