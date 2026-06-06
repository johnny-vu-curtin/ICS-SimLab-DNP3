#!/usr/bin/env python3

# FILE PURPOSE: DNP3 Master component — models a SCADA controller.
# Connects to one or more DNP3 Outstations (solar inverters) over TCP.
# Performs periodic Class 0 integrity polls and receives unsolicited responses.
# Stores current values per outstation and exposes them via REST API on port 1111
# for the Streamlit dashboard.

import json
import time
import logging
import threading
from flask import Flask, jsonify

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

# Index: outstation_address -> outstation_name (for routing SOE callbacks)
_addr_to_name = {}


# ---------------------------------------------------------------------------
# SOE Handler — receives measurement data from outstations (Group 30/32, 1/2)
# ---------------------------------------------------------------------------
class SolarSOEHandler(opendnp3.ISOEHandler):

    def __init__(self, outstation_name):
        super().__init__()
        self._name = outstation_name

    def BeginFragment(self, result):
        pass

    def EndFragment(self, result):
        pass

    def Process(self, info, values):
        # Dispatch to the correct type handler based on opendnp3 measurement type
        try:
            # values is iterable; each element has .value and .index
            for v in values:
                self._store(info, v.value, v.index)
        except Exception as e:
            logging.debug(f"SOE Process error [{self._name}]: {e}")

    def _store(self, info, measurement, index):
        group = info.gv.group
        # Group 30/32 → Analogue Input
        if group in (30, 32):
            self._update_by_index("analogue_input", index, float(measurement.value))
        # Group 1/2 → Binary Input
        elif group in (1, 2):
            self._update_by_index("binary_input", index, bool(measurement.value))
        # Group 10 → Binary Output Status
        elif group == 10:
            self._update_by_index("binary_output", index, bool(measurement.value))
        # Group 40 → Analogue Output Status
        elif group == 40:
            self._update_by_index("analogue_output", index, float(measurement.value))

    def _update_by_index(self, data_type, index, value):
        with _data_lock:
            outstation_data = _data_points.get(self._name, {})
            for pv, entry in outstation_data.items():
                if entry["type"] == data_type and entry["address"] == index:
                    entry["value"] = value
                    break

    # Required overloads for all measurement types — route to Process
    def Process_1(self, info, values): self.Process(info, values)
    def Process_2(self, info, values): self.Process(info, values)
    def Process_3(self, info, values): self.Process(info, values)
    def Process_4(self, info, values): self.Process(info, values)
    def Process_5(self, info, values): self.Process(info, values)
    def Process_6(self, info, values): self.Process(info, values)
    def Process_7(self, info, values): self.Process(info, values)
    def Process_8(self, info, values): self.Process(info, values)
    def Process_9(self, info, values): self.Process(info, values)
    def Process_10(self, info, values): self.Process(info, values)
    def Process_11(self, info, values): self.Process(info, values)
    def Process_12(self, info, values): self.Process(info, values)
    def Process_13(self, info, values): self.Process(info, values)
    def Process_14(self, info, values): self.Process(info, values)
    def Process_15(self, info, values): self.Process(info, values)
    def Process_16(self, info, values): self.Process(info, values)


# ---------------------------------------------------------------------------
# Master Application callbacks
# ---------------------------------------------------------------------------
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
        if iin.LSB.DEVICE_RESTART:
            logging.warning(f"Outstation {self._name} restarted — will re-enable unsolicited")

    def Now(self):
        return opendnp3.DNPTime(int(time.time() * 1000))

    def GetTaskChangeHandler(self):
        return opendnp3.ITaskCallback.Create()


# ---------------------------------------------------------------------------
# Flask REST API — used by Streamlit dashboard
# ---------------------------------------------------------------------------
@app.route("/registers", methods=["GET"])
def get_registers():
    with _data_lock:
        # Flatten all outstations into a single dict for the dashboard
        flat = {}
        for name, entries in _data_points.items():
            for pv, entry in entries.items():
                flat[f"{name}.{pv}"] = entry
        return jsonify(flat)


@app.route("/registers/<outstation_name>", methods=["GET"])
def get_outstation_registers(outstation_name):
    with _data_lock:
        data = _data_points.get(outstation_name, {})
        return jsonify(dict(data))


def run_flask():
    app.run(host="0.0.0.0", port=1111)


# ---------------------------------------------------------------------------
# Build a single master connection to one outstation
# ---------------------------------------------------------------------------
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
    integrity_scan = master.AddClassScan(
        opendnp3.ClassField.AllClasses(),
        openpal.TimeDuration().Seconds(int(poll_interval_s))
    )

    master.Enable()

    logging.info(
        f"DNP3 Master connected to {name} at {ip}:{port} "
        f"(master={master_addr}, outstation={outstation_addr})"
    )
    return master, channel


# ---------------------------------------------------------------------------
# Initialise _data_points for all outstations based on config
# ---------------------------------------------------------------------------
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

    manager = asiodnp3.DNP3Manager(1, asiodnp3.ConsoleLogger().Create())

    masters = []
    channels = []
    for outstation_entry in configs.get("outstations", []):
        master, channel = build_master_connection(
            manager, configs, outstation_entry, poll_interval_s
        )
        masters.append(master)
        channels.append(channel)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
