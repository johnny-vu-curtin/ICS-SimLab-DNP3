#!/usr/bin/env python3

# Author: Van Sanh Vu, Purpose: DNP3 development
# FILE PURPOSE: DNP3 Master component — models a SCADA controller.
# Connects to one or more DNP3 Outstations (solar inverters) over TCP.
# Performs periodic Class 0 integrity polls and receives unsolicited responses.
# Stores current values per outstation and exposes them via REST API on port 1111
# for the Streamlit dashboard.

import json
import sys
import time
import logging
import subprocess
import threading
from flask import Flask, jsonify, request

try:
    from pydnp3 import asiodnp3, asiopal, opendnp3, openpal
    DNP3_AVAILABLE = True
except ImportError:
    logging.error("pydnp3 not found — install dnp3-python package")
    DNP3_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)

# Shared state — { outstation_name: { physical_value: {"type":..., "address":..., "count":1, "value":...} } }
_data_points = {}
_data_lock = threading.Lock()

# Lookup: outstation_name → {ip, port, outstation_address, master_address}, used to
# spawn an isolated one-shot command worker process (see send_command / _cli_send_command
# below) — commands no longer go through the persistent polling master's own
# DirectOperate() call.
_outstation_meta = {}

# Lookup: outstation_name → live master object (for Disable()/Enable() around a command;
# see _run_isolated_command). Needed because the outstation's DNP3-TCP server only
# tolerates one live link-address-1 session at a time — see module docstring on
# _run_isolated_command for why the persistent polling connection must step aside.
_masters = {}

# One threading.Lock per outstation — serializes command dispatch so two overlapping
# POST /command requests for the same outstation can't both Disable()/Enable() the same
# master concurrently.
_command_locks = {}


# PURPOSE: Maps a GroupVariation to (data_type, is_float). Returns (None, None) if unknown.
def _parse_gv(gv) -> tuple:
    """Map a GroupVariation to (data_type, is_float). Returns (None, None) if unknown.

    Handles both 'GroupVariation.Group30Var1' and 'Group30Var1' string formats
    so the parser doesn't break if dnp3-python changes its repr in a future release.
    """
    s = str(gv)
    try:
        # rsplit strips any "Foo." prefix: "GroupVariation.Group30Var1" → "Group30Var1"
        s = s.rsplit(".", 1)[-1]
        group = int(s[5:].split("Var")[0])
    except (ValueError, IndexError):
        return None, None
    if group in (30, 32):
        return "analogue_input", True
    if group in (1, 2):
        return "binary_input", False
    if group == 10:
        return "binary_output", False
    if group in (40, 42):
        return "analogue_output", True
    return None, None


# ---------------------------------------------------------------------------
# SOE Handler — receives measurement data from outstations (Group 30/32, 1/2)
# ---------------------------------------------------------------------------
# PURPOSE: Receives measurement data (Group 30/32 analogue, 1/2 binary) from outstations
class SolarSOEHandler(opendnp3.ISOEHandler):

    def __init__(self, outstation_name):
        super().__init__()
        self._name = outstation_name

    def Start(self):
        pass

    def End(self):
        pass

    def Process(self, info, values):
        try:
            values.ForeachItem(lambda item: self._store(info, item.value, item.index))
        except Exception as e:
            logging.debug(f"SOE Process error [{self._name}]: {e}")

    def _store(self, info, measurement, index):
        data_type, is_float = _parse_gv(info.gv)
        if data_type is None:
            return
        try:
            value = float(measurement.value) if is_float else bool(measurement.value)
            self._update_by_index(data_type, index, value)
        except Exception as e:
            logging.debug(f"Store error [{self._name}]: {e}")

    def _update_by_index(self, data_type, index, value):
        with _data_lock:
            outstation_data = _data_points.get(self._name, {})
            for pv, entry in outstation_data.items():
                if entry["type"] == data_type and entry["address"] == index:
                    entry["value"] = value
                    break



# ---------------------------------------------------------------------------
# Master Application callbacks
# ---------------------------------------------------------------------------
# PURPOSE: Master lifecycle callbacks required by opendnp3.IMasterApplication
class SolarMasterApplication(opendnp3.IMasterApplication):

    def __init__(self, outstation_name):
        super().__init__()
        self._name = outstation_name

    def OnOpen(self):
        logging.info(f"Master connected to {self._name}")

    def OnClose(self):
        logging.warning(f"Master disconnected from {self._name}")

    def AssignClassDuringStartup(self):
        return False

    def OnReceiveIIN(self, iin):
        if iin.IsSet(opendnp3.IINBit.DEVICE_RESTART):
            logging.warning(f"Outstation {self._name} restarted")

    def Now(self):
        return opendnp3.DNPTime(int(time.time() * 1000))

    def OnStateChange(self, state):
        pass

    def OnTaskStart(self, type, id):
        pass

    def OnTaskComplete(self, info):
        pass

    def OnKeepAliveInitiated(self):
        pass

    def OnKeepAliveFailure(self):
        pass

    def OnKeepAliveSuccess(self):
        pass



# ---------------------------------------------------------------------------
# Flask REST API — used by Streamlit dashboard
# ---------------------------------------------------------------------------
# PURPOSE: Returns current values for every outstation, flattened for the dashboard
@app.route("/registers", methods=["GET"])
def get_registers():
    with _data_lock:
        # Flatten all outstations into a single dict for the dashboard
        flat = {}
        for name, entries in _data_points.items():
            for pv, entry in entries.items():
                flat[f"{name}.{pv}"] = entry
        return jsonify(flat)


# PURPOSE: Returns current values for a single named outstation
@app.route("/registers/<outstation_name>", methods=["GET"])
def get_outstation_registers(outstation_name):
    with _data_lock:
        data = _data_points.get(outstation_name, {})
        return jsonify(dict(data))


# PURPOSE: Runs the Flask REST API server on port 1111
def run_flask():
    app.run(host="0.0.0.0", port=1111)


# ---------------------------------------------------------------------------
# Isolated command worker — one throwaway OS process per DirectOperate
# ---------------------------------------------------------------------------
# dnp3-python 0.3.0b2 crashes the calling process when a command TASK COMPLETES — a
# native asio-thread GIL fault that happens with Python *and* C++ completion callbacks
# alike (same root cause the solar_plant_attack attacker works around — see
# config/solar_plant_attack/logic/attack_logic.py). The persistent master connections
# here already have live asio polling threads (AddClassScan), so we cannot fork() out of
# this process the way the attacker does from its thread-free parent; instead every
# command runs via subprocess.Popen (full exec into a new interpreter) so a crash on
# completion only kills the disposable worker, never the polling master or the Flask API.
#
# PURPOSE: CLI entry point for the isolated command worker (invoked as a subprocess)
def _cli_send_command(payload_json):
    """Connect fresh to one outstation, send one DirectOperate, then exit.

    Mirrors attack_logic.py's _connect/_inject_once/_inject_single: the outstation
    executes the command on frame arrival (before the master processes the response),
    so the command lands regardless of whether this worker crashes afterward.
    """
    payload = json.loads(payload_json)
    manager = asiodnp3.DNP3Manager(1, asiodnp3.ConsoleLogger().Create())
    channel = manager.AddTCPClient(
        "client", opendnp3.levels.NORMAL, asiopal.ChannelRetry().Default(),
        payload["ip"], "0.0.0.0", payload["port"],
        asiodnp3.PrintingChannelListener().Create()
    )
    stack_config = asiodnp3.MasterStackConfig()
    stack_config.master.responseTimeout = openpal.TimeDuration().Seconds(5)
    stack_config.link.LocalAddr = payload["master_address"]
    stack_config.link.RemoteAddr = payload["outstation_address"]

    # Library C++ handlers, not Python subclasses — a Python override invoked from an
    # asio worker thread races the GIL here (same reason attack_logic.py avoids them).
    soe = asiodnp3.PrintingSOEHandler().Create()
    application = asiodnp3.DefaultMasterApplication().Create()
    master = channel.AddMaster("master", soe, application, stack_config)
    master.Enable()

    time.sleep(2)  # let the link establish before issuing the command

    cmd_type, index, value = payload["type"], payload["index"], payload["value"]
    label = f"{payload['ip']}:{cmd_type}[{index}]={value}"
    callback = asiodnp3.PrintingCommandCallback.Get()
    try:
        if cmd_type == "binary_output":
            code = opendnp3.ControlCode.LATCH_ON if int(value) else opendnp3.ControlCode.LATCH_OFF
            master.DirectOperate(opendnp3.ControlRelayOutputBlock(code), int(index), callback)
        else:
            master.DirectOperate(opendnp3.AnalogOutputDouble64(float(value)), int(index), callback)
        logging.info(f"command worker: sent {label}")
    except Exception as e:
        logging.error(f"command worker: failed to send {label}: {e}")

    time.sleep(2)  # allow the frame to reach the outstation and be executed


# PURPOSE: CLI entry point for the isolated restart worker (invoked as a subprocess)
def _cli_send_restart(payload_json):
    """Connect fresh to one outstation, send one Restart (warm/cold), then exit.

    Restart()'s completion crashes the calling process exactly like DirectOperate's does
    (verified live 2026-11-10: TaskCompletion.SUCCESS on a warm restart killed scada with
    the same PyThreadState_Get GIL fault) — same isolation treatment, same reason.
    """
    payload = json.loads(payload_json)
    manager = asiodnp3.DNP3Manager(1, asiodnp3.ConsoleLogger().Create())
    channel = manager.AddTCPClient(
        "client", opendnp3.levels.NORMAL, asiopal.ChannelRetry().Default(),
        payload["ip"], "0.0.0.0", payload["port"],
        asiodnp3.PrintingChannelListener().Create()
    )
    stack_config = asiodnp3.MasterStackConfig()
    stack_config.master.responseTimeout = openpal.TimeDuration().Seconds(5)
    stack_config.link.LocalAddr = payload["master_address"]
    stack_config.link.RemoteAddr = payload["outstation_address"]

    soe = asiodnp3.PrintingSOEHandler().Create()
    application = asiodnp3.DefaultMasterApplication().Create()
    master = channel.AddMaster("master", soe, application, stack_config)
    master.Enable()

    time.sleep(2)  # let the link establish before issuing the restart

    restart_type = payload["restart_type"]
    label = f"{payload['ip']}:{restart_type}_restart"

    def _on_complete(result):
        logging.info(f"restart worker: result [{label}]: {result.summary}")

    try:
        rtype = opendnp3.RestartType.WARM if restart_type == "warm" else opendnp3.RestartType.COLD
        master.Restart(rtype, _on_complete)
        logging.info(f"restart worker: sent {label}")
    except Exception as e:
        logging.error(f"restart worker: failed to send {label}: {e}")

    time.sleep(2)  # allow the request + response to complete before this worker exits


# PURPOSE: CLI entry point for the isolated time-sync worker (invoked as a subprocess)
def _cli_send_timesync(payload_json):
    """Connect fresh to one outstation with timeSyncMode=LAN, then exit.

    There's no explicit "sync now" call to make — the moment this connection sees the
    outstation's NEED_TIME IIN bit (dnp3_outstation.py's GetApplicationIIN()), the stack
    runs its "LAN time sync" task automatically. That task crashes the calling process
    outright (confirmed live 2026-11-11: uncaught C++ `pybind11::cast_error`, `terminate()`
    called — not a catchable Python GIL fault like DirectOperate/Restart, can't be
    try/excepted at all). Hence isolating it into this disposable worker is the only way
    to exercise it without taking the persistent polling master down with it.
    """
    payload = json.loads(payload_json)
    manager = asiodnp3.DNP3Manager(1, asiodnp3.ConsoleLogger().Create())
    channel = manager.AddTCPClient(
        "client", opendnp3.levels.NORMAL, asiopal.ChannelRetry().Default(),
        payload["ip"], "0.0.0.0", payload["port"],
        asiodnp3.PrintingChannelListener().Create()
    )
    stack_config = asiodnp3.MasterStackConfig()
    stack_config.master.responseTimeout = openpal.TimeDuration().Seconds(5)
    stack_config.master.timeSyncMode = opendnp3.TimeSyncMode.LAN
    stack_config.link.LocalAddr = payload["master_address"]
    stack_config.link.RemoteAddr = payload["outstation_address"]

    soe = asiodnp3.PrintingSOEHandler().Create()
    application = asiodnp3.DefaultMasterApplication().Create()
    master = channel.AddMaster("master", soe, application, stack_config)
    master.Enable()

    logging.info(f"timesync worker: connected to {payload['ip']} with timeSyncMode=LAN — "
                 f"waiting for the automatic LAN time sync task to run")
    time.sleep(5)  # let the link establish and the automatic task run (and crash)


# PURPOSE: Waits for an isolated worker, but doesn't treat a lingering process as an
# error — the worker's own crash-on-task-completion (see _cli_send_command/_cli_send_
# restart) can take well over 10s to actually tear down (observed live: the Fatal Python
# error handler dumping every thread's stack measurably delays process exit), even though
# the DNP3 operation itself already completed and was logged by the worker. By the time
# this timeout fires the job is done either way, so a lingering worker is expected and
# gets killed outright rather than waited on indefinitely.
def _wait_or_kill(proc, label, timeout=8):
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logging.info(f"worker for [{label}] still tearing down after {timeout}s — killing it "
                     f"(the operation itself already completed; see the worker's own log line)")
        proc.kill()
        proc.wait()


# PURPOSE: Steps the persistent polling master aside, runs the isolated command worker
# alone on the wire, then resumes polling — all in a background thread.
def _run_isolated_command(outstation_name, master, payload):
    """The outstation's DNP3-TCP server only tolerates one live link-address-1 session
    at a time (confirmed live: a second simultaneous connection from the same master
    address makes both sessions flap, and queued DirectOperate frames get lost in the
    churn). So the persistent polling master must Disable() — dropping its TCP session —
    before the isolated worker (see _cli_send_command) connects alone, and Enable() again
    once the worker is done. Runs in a background thread so the HTTP response returns
    immediately; serialized per-outstation via _command_locks.
    """
    lock = _command_locks.setdefault(outstation_name, threading.Lock())
    label = f"{outstation_name}:{payload['type']}[{payload['index']}]={payload['value']}"
    with lock:
        try:
            master.Disable()
        except Exception as e:
            logging.error(f"command dispatch: Disable() failed for {outstation_name}: {e}")
        time.sleep(0.5)  # let the outstation see the disconnect before the worker connects

        try:
            proc = subprocess.Popen([sys.executable, __file__, "--send-command", json.dumps(payload)])
            _wait_or_kill(proc, label)
        except Exception as e:
            logging.error(f"command dispatch: worker failed [{label}]: {e}")

        time.sleep(0.5)  # let the worker's connection fully close before resuming polling
        try:
            master.Enable()
        except Exception as e:
            logging.error(f"command dispatch: Enable() failed for {outstation_name}: {e}")


# PURPOSE: Sends a DNP3 DirectOperate command (binary/analogue output) to an outstation
@app.route("/command/<outstation_name>", methods=["POST"])
def send_command(outstation_name):
    """Send a DNP3 DirectOperate command to an outstation.

    Body JSON:
      { "type": "binary_output"|"analogue_output", "index": <int>, "value": <number> }

    Returns 200 immediately; the command runs in a background thread (see
    _run_isolated_command) that briefly pauses this outstation's polling, runs the
    command in an isolated worker process (see _cli_send_command) so its crash-on-
    completion never reaches this process, then resumes polling.
    """
    body = request.get_json(force=True, silent=True) or {}
    cmd_type = body.get("type")
    index    = body.get("index")
    value    = body.get("value")

    if cmd_type is None or index is None or value is None:
        return jsonify({"error": "required fields: type, index, value"}), 400

    if cmd_type not in ("binary_output", "analogue_output"):
        return jsonify({"error": f"unsupported type: {cmd_type}"}), 400

    master = _masters.get(outstation_name)
    meta = _outstation_meta.get(outstation_name)
    if master is None or meta is None:
        return jsonify({"error": f"unknown outstation: {outstation_name}"}), 404

    payload = {
        "ip": meta["ip"],
        "port": meta["port"],
        "outstation_address": meta["outstation_address"],
        "master_address": meta["master_address"],
        "type": cmd_type,
        "index": int(index),
        "value": float(value),
    }
    threading.Thread(
        target=_run_isolated_command, args=(outstation_name, master, payload), daemon=True
    ).start()

    logging.info(f"DirectOperate dispatched to isolated worker: "
                 f"{outstation_name}:{cmd_type}[{index}]={value}")
    return jsonify({"queued": True, "outstation": outstation_name,
                    "type": cmd_type, "index": index, "value": value})


# PURPOSE: Steps the persistent polling master aside, runs the isolated restart worker
# alone on the wire, then resumes polling — all in a background thread. Same Disable/
# isolated-worker/Enable treatment as _run_isolated_command, and for the same reason:
# Restart()'s completion crashes the calling process just as much as DirectOperate's does
# (confirmed live 2026-11-10 — see _cli_send_restart).
def _run_isolated_restart(outstation_name, master, payload):
    lock = _command_locks.setdefault(outstation_name, threading.Lock())
    label = f"{outstation_name}:{payload['restart_type']}_restart"
    with lock:
        try:
            master.Disable()
        except Exception as e:
            logging.error(f"restart dispatch: Disable() failed for {outstation_name}: {e}")
        time.sleep(0.5)

        try:
            proc = subprocess.Popen([sys.executable, __file__, "--send-restart", json.dumps(payload)])
            _wait_or_kill(proc, label)
        except Exception as e:
            logging.error(f"restart dispatch: worker failed [{label}]: {e}")

        time.sleep(0.5)
        try:
            master.Enable()
        except Exception as e:
            logging.error(f"restart dispatch: Enable() failed for {outstation_name}: {e}")


# PURPOSE: Sends FC 0x0D Cold Restart or FC 0x0E Warm Restart to an outstation
@app.route("/restart/<outstation_name>", methods=["POST"])
def send_restart(outstation_name):
    """Send a DNP3 Restart request to an outstation.

    Body JSON: { "type": "warm"|"cold" }

    Runs via the same isolation pattern as /command — Restart()'s completion crashes
    the calling process exactly like DirectOperate's does (confirmed live), so this
    never touches the persistent polling master's own Restart() call.
    """
    body = request.get_json(force=True, silent=True) or {}
    restart_type = body.get("type")
    if restart_type not in ("warm", "cold"):
        return jsonify({"error": "required field: type ('warm' or 'cold')"}), 400

    master = _masters.get(outstation_name)
    meta = _outstation_meta.get(outstation_name)
    if master is None or meta is None:
        return jsonify({"error": f"unknown outstation: {outstation_name}"}), 404

    payload = {
        "ip": meta["ip"],
        "port": meta["port"],
        "outstation_address": meta["outstation_address"],
        "master_address": meta["master_address"],
        "restart_type": restart_type,
    }
    threading.Thread(
        target=_run_isolated_restart, args=(outstation_name, master, payload), daemon=True
    ).start()

    logging.info(f"Restart dispatched to isolated worker: {outstation_name}:{restart_type}_restart")
    return jsonify({"queued": True, "outstation": outstation_name, "type": restart_type})


# PURPOSE: Steps the persistent polling master aside, runs the isolated time-sync worker
# alone on the wire, then resumes polling — all in a background thread. Same pattern as
# _run_isolated_restart, for the same reason: the automatic LAN time sync task crashes
# whatever process runs it (confirmed live 2026-11-11 — see _cli_send_timesync).
def _run_isolated_timesync(outstation_name, master, payload):
    lock = _command_locks.setdefault(outstation_name, threading.Lock())
    label = f"{outstation_name}:timesync"
    with lock:
        try:
            master.Disable()
        except Exception as e:
            logging.error(f"timesync dispatch: Disable() failed for {outstation_name}: {e}")
        time.sleep(0.5)

        try:
            proc = subprocess.Popen([sys.executable, __file__, "--send-timesync", json.dumps(payload)])
            _wait_or_kill(proc, label)
        except Exception as e:
            logging.error(f"timesync dispatch: worker failed [{label}]: {e}")

        time.sleep(0.5)
        try:
            master.Enable()
        except Exception as e:
            logging.error(f"timesync dispatch: Enable() failed for {outstation_name}: {e}")


# PURPOSE: Triggers a DNP3 LAN time sync against an outstation in an isolated worker
@app.route("/timesync/<outstation_name>", methods=["POST"])
def send_timesync(outstation_name):
    """Trigger a Group 50 LAN time sync against an outstation.

    No body required. Runs in an isolated worker (see _run_isolated_timesync) — the
    persistent polling master NEVER runs this itself (its timeSyncMode is deliberately
    None, not LAN — see build_master_connection) because the automatic task crashes the
    calling process outright via an uncaught C++ exception, confirmed live. This endpoint
    exists only to demonstrate/exercise the feature safely in a disposable process.
    """
    master = _masters.get(outstation_name)
    meta = _outstation_meta.get(outstation_name)
    if master is None or meta is None:
        return jsonify({"error": f"unknown outstation: {outstation_name}"}), 404

    payload = {
        "ip": meta["ip"],
        "port": meta["port"],
        "outstation_address": meta["outstation_address"],
        "master_address": meta["master_address"],
    }
    threading.Thread(
        target=_run_isolated_timesync, args=(outstation_name, master, payload), daemon=True
    ).start()

    logging.info(f"Time sync dispatched to isolated worker: {outstation_name}")
    return jsonify({"queued": True, "outstation": outstation_name})


# PURPOSE: Sends FC 0x14 Enable Unsolicited or FC 0x15 Disable Unsolicited to an outstation
@app.route("/unsolicited/<outstation_name>", methods=["POST"])
def send_unsolicited_toggle(outstation_name):
    """Enable or disable unsolicited responses (Class 1/2/3) on an outstation.

    Body JSON: { "action": "enable"|"disable" }

    PerformFunction() is a plain function-code-only request with no completion task,
    so there's no command-style crash risk to isolate here either.
    """
    body = request.get_json(force=True, silent=True) or {}
    action = body.get("action")
    if action not in ("enable", "disable"):
        return jsonify({"error": "required field: action ('enable' or 'disable')"}), 400

    master = _masters.get(outstation_name)
    if master is None:
        return jsonify({"error": f"unknown outstation: {outstation_name}"}), 404

    func = opendnp3.FunctionCode.ENABLE_UNSOLICITED if action == "enable" else opendnp3.FunctionCode.DISABLE_UNSOLICITED
    headers = [
        opendnp3.Header.From(opendnp3.PointClass.Class1),
        opendnp3.Header.From(opendnp3.PointClass.Class2),
        opendnp3.Header.From(opendnp3.PointClass.Class3),
    ]
    label = f"{outstation_name}:{action}_unsolicited"
    try:
        master.PerformFunction(label, func, headers)
    except Exception as e:
        logging.error(f"PerformFunction failed [{label}]: {e}")
        return jsonify({"error": str(e)}), 500

    logging.info(f"Unsolicited toggle queued: {label}")
    return jsonify({"queued": True, "outstation": outstation_name, "action": action})


# ---------------------------------------------------------------------------
# Build a single master connection to one outstation
# ---------------------------------------------------------------------------
# PURPOSE: Builds one DNP3-TCP master channel + master object for one outstation
def build_master_connection(manager, master_config_entry, outstation_entry, poll_interval_s):
    name = outstation_entry["name"]
    ip = outstation_entry["ip"]
    outstation_addr = outstation_entry["outstation_address"]
    master_addr = master_config_entry["master_address"]
    port = outstation_entry.get("port", 20000)

    channel = manager.AddTCPClient(
        f"client_{name}",
        opendnp3.levels.NORMAL,
        asiopal.ChannelRetry().Default(),
        ip,
        "0.0.0.0",
        port,
        asiodnp3.PrintingChannelListener().Create()
    )

    stack_config = asiodnp3.MasterStackConfig()
    stack_config.master.responseTimeout = openpal.TimeDuration().Seconds(5)
    # NOT TimeSyncMode.LAN here — confirmed live 2026-11-11: the instant any outstation
    # reports IINBit.NEED_TIME (which dnp3_outstation.py now does), this persistent
    # connection's own "LAN time sync" task crashes the whole process outright via an
    # uncaught C++ exception (`terminate() ... pybind11::cast_error`) — not a Python GIL
    # fault like DirectOperate/Restart, so it can't even be try/excepted, and it fires
    # automatically with no REST call to isolate around. Time sync is only exercised via
    # the isolated /timesync endpoint below, never on this persistent connection.
    stack_config.master.timeSyncMode = getattr(opendnp3.TimeSyncMode, "None")
    stack_config.link.LocalAddr = master_addr
    stack_config.link.RemoteAddr = outstation_addr

    soe_handler = SolarSOEHandler(name)
    application = SolarMasterApplication(name)

    master = channel.AddMaster(
        f"master_{name}",
        soe_handler,
        application,
        stack_config
    )

    # Integrity poll (Class 0 — all static data)
    master.AddClassScan(
        opendnp3.ClassField.AllClasses(),
        openpal.TimeDuration.Seconds(int(poll_interval_s))
    )

    master.Enable()

    logging.info(
        f"DNP3 Master connected to {name} at {ip}:{port} "
        f"(master={master_addr}, outstation={outstation_addr})"
    )
    # Return soe_handler and application so caller keeps strong references —
    # pybind11 only holds weak refs; GC would invalidate the virtual dispatch.
    return master, channel, soe_handler, application


# ---------------------------------------------------------------------------
# Initialise _data_points for all outstations based on config
# ---------------------------------------------------------------------------
# PURPOSE: Pre-populates _data_points with empty entries for every configured outstation
def init_data_points(master_config, outstation_configs):
    with _data_lock:
        for outstation_entry in master_config.get("outstations", []):
            name = outstation_entry["name"]
            # Find detailed outstation config by name
            detail = next(
                (o for o in outstation_configs if o["name"] == name), None
            )
            if detail is None:
                _data_points[name] = {}
                continue

            entries = {}
            for ai in detail.get("analogue_inputs", []):
                entries[ai["physical_value"]] = {
                    "type": "analogue_input",
                    "address": ai["index"],
                    "count": 1,
                    "value": 0.0
                }
            for bi in detail.get("binary_inputs", []):
                entries[bi["physical_value"]] = {
                    "type": "binary_input",
                    "address": bi["index"],
                    "count": 1,
                    "value": False
                }
            for bo in detail.get("binary_outputs", []):
                entries[bo["physical_value"]] = {
                    "type": "binary_output",
                    "address": bo["index"],
                    "count": 1,
                    "value": False
                }
            for ao in detail.get("analogue_outputs", []):
                entries[ao["physical_value"]] = {
                    "type": "analogue_output",
                    "address": ao["index"],
                    "count": 1,
                    "value": 0.0
                }
            _data_points[name] = entries


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
# PURPOSE: Loads config, builds one master connection per outstation, starts the REST API
def main():
    if not DNP3_AVAILABLE:
        logging.error("Cannot start — dnp3-python not installed")
        return

    with open("config.json") as f:
        configs = json.load(f)

    # Container config is pre-flattened by setup.py:
    #   master_address, outstations, poll_interval_s, dnp3_outstations
    outstation_configs = configs.get("dnp3_outstations", [])
    poll_interval_s = configs.get("poll_interval_s", 5)

    logging.info(
        f"Starting DNP3 Master: address={configs['master_address']}, "
        f"connecting to {len(configs.get('outstations', []))} outstation(s)"
    )

    init_data_points(configs, outstation_configs)

    n_outstations = len(configs.get("outstations", []))
    manager = asiodnp3.DNP3Manager(max(1, n_outstations), asiodnp3.ConsoleLogger().Create())

    masters = []
    channels = []
    soe_handlers = []
    applications = []
    for outstation_entry in configs.get("outstations", []):
        master, channel, soe, app = build_master_connection(
            manager, configs, outstation_entry, poll_interval_s
        )
        masters.append(master)
        channels.append(channel)
        soe_handlers.append(soe)
        applications.append(app)
        _masters[outstation_entry["name"]] = master
        _outstation_meta[outstation_entry["name"]] = {
            "ip": outstation_entry["ip"],
            "port": outstation_entry.get("port", 20000),
            "outstation_address": outstation_entry["outstation_address"],
            "master_address": configs["master_address"],
        }

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    while True:
        time.sleep(1)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--send-command":
        _cli_send_command(sys.argv[2])
    elif len(sys.argv) > 2 and sys.argv[1] == "--send-restart":
        _cli_send_restart(sys.argv[2])
    elif len(sys.argv) > 2 and sys.argv[1] == "--send-timesync":
        _cli_send_timesync(sys.argv[2])
    else:
        main()
