#!/usr/bin/env python3

# Author: Van Sanh Vu, Purpose: DNP3 development
# FILE PURPOSE: DNP3 Master component — models a SCADA controller.
# Connects to one or more DNP3 Outstations (solar inverters) over TCP.
# Performs periodic Class 0 integrity polls and receives unsolicited responses.
# Stores current values per outstation and exposes them via REST API on port 1111
# for the Streamlit dashboard.

import json
import time
import logging
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

# Lookup: outstation_name → master object (for command dispatch)
_masters = {}


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
# Command callback — logs DirectOperate result asynchronously.
# dnp3-python's DirectOperate() takes a plain callable (Callable[[ICommandTaskResult],
# None]), not a subclass of an ICommandCallback interface — that interface does not
# exist in this library version.
# ---------------------------------------------------------------------------
# PURPOSE: Builds a plain callable that logs a DirectOperate result asynchronously
def _make_command_callback(label):
    def _on_complete(result):
        logging.info(f"DirectOperate result [{label}]: {result.summary}")
    return _on_complete


# PURPOSE: Sends a DNP3 DirectOperate command (binary/analogue output) to an outstation
@app.route("/command/<outstation_name>", methods=["POST"])
def send_command(outstation_name):
    """Send a DNP3 DirectOperate command to an outstation.

    Body JSON:
      { "type": "binary_output"|"analogue_output", "index": <int>, "value": <number> }

    Returns 200 immediately (fire-and-forget); result is logged in container stdout.
    """
    body = request.get_json(force=True, silent=True) or {}
    cmd_type = body.get("type")
    index    = body.get("index")
    value    = body.get("value")

    if cmd_type is None or index is None or value is None:
        return jsonify({"error": "required fields: type, index, value"}), 400

    master = _masters.get(outstation_name)
    if master is None:
        return jsonify({"error": f"unknown outstation: {outstation_name}"}), 404

    label = f"{outstation_name}:{cmd_type}[{index}]={value}"
    try:
        if cmd_type == "binary_output":
            code = opendnp3.ControlCode.LATCH_ON if int(value) else opendnp3.ControlCode.LATCH_OFF
            master.DirectOperate(
                opendnp3.ControlRelayOutputBlock(code),
                int(index),
                _make_command_callback(label)
            )
        elif cmd_type == "analogue_output":
            master.DirectOperate(
                opendnp3.AnalogOutputDouble64(float(value)),
                int(index),
                _make_command_callback(label)
            )
        else:
            return jsonify({"error": f"unsupported type: {cmd_type}"}), 400
    except Exception as e:
        logging.error(f"DirectOperate failed [{label}]: {e}")
        return jsonify({"error": str(e)}), 500

    logging.info(f"DirectOperate queued: {label}")
    return jsonify({"queued": True, "outstation": outstation_name,
                    "type": cmd_type, "index": index, "value": value})


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
    stack_config.master.timeSyncMode = opendnp3.TimeSyncMode.LAN
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

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
