#!/usr/bin/env python3

# Author: Van Sanh Vu, Purpose: DNP3 development
# FILE PURPOSE: Attacker component — attack simulator for Activity 2 dataset generation.
# Runs inside the ICS-SimLab lab network as a rogue DNP3 master. On startup it loads the
# scenario's attack logic module (if present) and calls its run() entry point. Attack type
# and intensity are selected via environment variables set by src/setup.py, so switching
# attacks requires only a config change — no code change. This file is pure scaffolding:
# it contains NO attack behaviour itself; all behaviour lives in the scenario's
# attack_logic.py, loaded dynamically below.

import os
import json
import time
import logging
import importlib.util
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
LOGIC_DIR = Path(__file__).resolve().parent / "logic"


# PURPOSE: Loads this attacker's config.json (targets, master_address, attack params).
def load_config() -> dict:
    """Read the config.json written into the container by setup.py."""
    if not CONFIG_PATH.exists():
        logging.error("attacker config.json not found at %s", CONFIG_PATH)
        return {}
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


# PURPOSE: Dynamically imports the scenario's attack logic module by filename.
def load_logic_module(logic_filename: str):
    """Import logic/<logic_filename> and return the module, or None if absent/invalid."""
    if not logic_filename:
        return None
    logic_path = LOGIC_DIR / logic_filename
    if not logic_path.exists():
        logging.warning("attack logic %s not found — attacker will idle", logic_path)
        return None
    spec = importlib.util.spec_from_file_location("attack_logic", logic_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# PURPOSE: Idles the container so it stays attached to the network for inspection.
def idle():
    """Keep the process alive when there is no logic to run (scaffold / infra testing)."""
    logging.info("attacker idling — no attack logic loaded")
    while True:
        time.sleep(3600)


# PURPOSE: Component entry point — loads config + logic and dispatches to run().
def main():
    config = load_config()
    logic_filename = config.get("logic", "")
    module = load_logic_module(logic_filename)

    if module is None or not hasattr(module, "run"):
        idle()
        return

    targets = config.get("targets", [])
    master_address = config.get("master_address", 1)

    # Attack type / intensity come from the environment (set per-container by setup.py),
    # so the same logic module can produce different attacks without editing code.
    config["attack"] = {
        "type": os.environ.get("ATTACK_TYPE", config.get("attack", {}).get("type", "none")),
        "intensity": os.environ.get("ATTACK_INTENSITY", config.get("attack", {}).get("intensity", "low")),
        "start_delay_s": int(os.environ.get("ATTACK_START_DELAY_S", config.get("attack", {}).get("start_delay_s", 0))),
    }

    logging.info("attacker starting: type=%s intensity=%s targets=%d",
                 config["attack"]["type"], config["attack"]["intensity"], len(targets))

    delay = config["attack"]["start_delay_s"]
    if delay > 0:
        logging.info("waiting %ds before starting (lets benign traffic establish)", delay)
        time.sleep(delay)

    module.run(targets, master_address, config)


if __name__ == "__main__":
    main()
