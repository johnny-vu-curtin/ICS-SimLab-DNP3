#!/usr/bin/env python3

# Author: Van Sanh Vu, Purpose: DNP3 development
# FILE PURPOSE: DNP3 Outstation component — models a solar inverter/RTU.
# Reads physical values from SQLite (written by HIL), exposes them as DNP3
# Analogue/Binary Inputs. Receives control commands (Binary/Analogue Output)
# from a DNP3 Master and writes them back to SQLite for the HIL to consume.
# Also exposes a REST API on port 1111 for the Streamlit dashboard.

import os
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


# PURPOSE: Validates a physical_value name before using it as a SQL table name
def _is_valid_table(name: str) -> bool:
    return bool(_VALID_TABLE_RE.match(name))

# Shared state — populated by DNP3 callbacks and SQLite reads
_data_points = {}   # { physical_value: {"type": ..., "index": ..., "value": ...} }
_data_lock = threading.Lock()

# Outstation reference for applying updates
_outstation = None

# Strong references to objects whose virtual methods C++ dispatches into. pybind11 keeps
# only weak refs to the command handler and outstation application, so without this they
# are garbage-collected after setup returns and the next virtual call ("ICommandHandler::
# Start") aborts the process. Only surfaced in Activity 2 — the first path to send a
# control command, which is the first thing to invoke ICommandHandler at all.
_keep_alive = []


# ---------------------------------------------------------------------------
# DNP3 Command Handler — receives Binary Output and Analogue Output from master
# ---------------------------------------------------------------------------
# PURPOSE: Handles incoming Binary/Analogue Output commands from the DNP3 master
class SolarCommandHandler(opendnp3.ICommandHandler):

    def __init__(self, configs, db_path):
        super().__init__()
        self._configs = configs
        self._db_path = db_path
        self._bo_map = {bo["index"]: bo["physical_value"] for bo in configs.get("binary_outputs", [])}
        self._ao_map = {ao["index"]: ao["physical_value"] for ao in configs.get("analogue_outputs", [])}

    # ICommandHandler's pure virtuals are Start()/End() — the previous Begin() left
    # Start() unoverridden, which aborted the outstation ("pure virtual call") the first
    # time a control command was actually processed. Only surfaced in Activity 2, which
    # is the first path to exercise FC 0x03 DirectOperate end-to-end.
    def Start(self):
        pass

    def End(self):
        pass

    def Select(self, command, index):
        return opendnp3.CommandStatus.SUCCESS

    def Operate(self, command, index, op_type):
        # Any exception raised here propagates through the C++ virtual call as
        # pybind11::error_already_set and aborts the whole outstation process. Catch
        # everything so a malformed/malicious command (which Activity 2 injects on
        # purpose) degrades to a DOWNSTREAM_FAIL status instead of killing the container.
        try:
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
        except Exception as e:
            logging.error(f"Operate failed for index {index}: {e}")
            return opendnp3.CommandStatus.DOWNSTREAM_FAIL

    def _handle_binary_output(self, index, command):
        physical_value = self._bo_map.get(index)
        if physical_value is None:
            return opendnp3.CommandStatus.NOT_SUPPORTED

        # "On" control codes → 1. ControlCode has no bare CLOSE in dnp3-python 0.3.0b2;
        # the close/energise variants are CLOSE_PULSE_ON / PULSE_ON (plus LATCH_ON).
        code = command.functionCode
        on_codes = (opendnp3.ControlCode.LATCH_ON,
                    opendnp3.ControlCode.CLOSE_PULSE_ON,
                    opendnp3.ControlCode.PULSE_ON)
        value = 1 if code in on_codes else 0

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
# Minimum interval between requesting a time sync. Real deployments resync far less
# often (minutes to hours); short here so the behaviour is observable in a live demo —
# see Group 50 note in CLAUDE.md for why this never fired before this was added.
TIME_SYNC_INTERVAL_S = 30.0


# PURPOSE: Outstation lifecycle callbacks required by opendnp3.IOutstationApplication
class SolarOutstationApplication(opendnp3.IOutstationApplication):

    def __init__(self):
        super().__init__()
        # None until first sync. GetApplicationIIN() below reads this every time the
        # stack composes a response — that's the master's only trigger to ever run its
        # LAN time-sync task (master.timeSyncMode=LAN only acts on IINBit.NEED_TIME).
        self._last_sync_time = None

    def SupportsWriteAbsoluteTime(self):
        return True

    # PURPOSE: Receives the synchronised time from a master-initiated Group 50 Write;
    # clears the NEED_TIME condition read by GetApplicationIIN() until the interval elapses again.
    def WriteAbsoluteTime(self, timestamp):
        logging.info(f"Time sync received: {timestamp}")
        self._last_sync_time = time.time()
        return True

    # PURPOSE: Reports NEED_TIME (IIN1.4) to the master once per TIME_SYNC_INTERVAL_S —
    # without this, master.timeSyncMode=LAN never has a reason to run its sync task at all.
    def GetApplicationIIN(self):
        iin = opendnp3.ApplicationIIN()
        iin.needTime = (
            self._last_sync_time is None
            or (time.time() - self._last_sync_time) > TIME_SYNC_INTERVAL_S
        )
        return iin

    def SupportsAssignClass(self):
        return False

    def MeasurementControl(self, is_restart):
        pass

    def WarmRestartSupport(self):
        return opendnp3.RestartMode.SUPPORTED_DELAY_COARSE

    def ColdRestartSupport(self):
        return opendnp3.RestartMode.SUPPORTED_DELAY_COARSE

    # PURPOSE: Handles FC 0x0E Warm Restart — the DNP3 application layer's own sequence
    # counters and event buffer are owned and reset internally by the opendnp3 stack once
    # this returns; there is no app-level state here that needs separate resetting.
    def WarmRestart(self):
        logging.warning("Warm restart requested (FC 0x0E)")
        return 2  # seconds until restart complete (SUPPORTED_DELAY_COARSE unit)

    # PURPOSE: Handles FC 0x0D Cold Restart — exits the process after a short delay (long
    # enough for the stack to send this response first) so Docker's restart policy brings
    # the container back up with a fully fresh process, matching real cold-restart
    # behaviour. Requires `restart: unless-stopped` on this container (src/setup.py).
    def ColdRestart(self):
        logging.warning("Cold restart requested (FC 0x0D) — exiting in 2s for Docker to restart this container")
        threading.Timer(2.0, lambda: os._exit(0)).start()
        return 2  # seconds until restart complete (SUPPORTED_DELAY_COARSE unit)


# ---------------------------------------------------------------------------
# SQLite polling — reads physical values and pushes them to DNP3 database
# ---------------------------------------------------------------------------
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
# PURPOSE: Returns current values for this outstation
@app.route("/registers", methods=["GET"])
def get_registers():
    with _data_lock:
        return jsonify(dict(_data_points))


# PURPOSE: Runs the Flask REST API server on port 1111
def run_flask():
    app.run(host="0.0.0.0", port=1111)


# ---------------------------------------------------------------------------
# Setup DNP3 outstation
# ---------------------------------------------------------------------------
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
    # Pin strong refs — C++ dispatches virtual calls into these (see _keep_alive note).
    _keep_alive.extend([command_handler, application])

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
