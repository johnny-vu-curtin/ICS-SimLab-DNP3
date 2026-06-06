#!/usr/bin/env python3

# -----------------------------------------------------------------------------
# Project: Curtin ICS-SimLab
# File: hil.py
#
# Copyright (c) 2025 Jaxson Brown, Curtin University
#
# Licensed under the MIT License. You may obtain a copy of the License at:
#     https://opensource.org/licenses/MIT
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# This work is supported by a Cross-Campus Cyber Security Research Project
# funded by **Curtin University**
#
# Author: Jaxson Brown
# Organisation: Curtin University
# Last Modified: 2025-08-27
# -----------------------------------------------------------------------------

# FILE PURPOSE: Simulates the physical layer. Data is written to the 
#               SQLite database to represent physical data collection

import asyncio
import sqlite3
import logging
import time
import utils
from threading import Thread

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# here we import the defined logic
# the logic will always be in a python file called logic.py, which gets copied to the container
try:
    import logic # type: ignore
except ModuleNotFoundError:
    logging.error("Could not import logic for HIL component")



# FUNCTION: output_data
# PURPOSE:  Handles writing the physical values into the SQLite database. Only used with output
#           interactions.
def output_data(configs, physical_values):
    # connect to hardware SQLite database
    conn = sqlite3.connect("physical_interactions.db")
    cursor = conn.cursor()
    hil = configs["database"]["table"]

    while True:
        for physical_value in configs["database"]["physical_values"]:
            table = physical_value["name"]
            if physical_value["io"] == "output":
                cursor.execute(f"INSERT INTO {table}(value, hil) VALUES(?, ?)", (physical_values[physical_value['name']], hil))
                conn.commit()
        time.sleep(0.3)



# FUNCTION: input_data
# PURPOSE:  Monitors physical database interactions for input interactions.
def input_data(configs, physical_values):
    # connect to hardware SQLite database
    conn = sqlite3.connect("physical_interactions.db")
    cursor = conn.cursor()

    while True:
        for physical_value in configs["database"]["physical_values"]:
            table = physical_value["name"]
            if physical_value["io"] == "input":
                cursor.execute(f"SELECT value FROM {table} ORDER BY timestamp DESC LIMIT 1")
                value = cursor.fetchone()
                conn.commit()

                if value and value[0] not in (None, ""):
                    physical_values[physical_value['name']] = int(float(value[0]))
        time.sleep(0.3)



# FUNCTION: main
# PURPOSE:  The main execution
async def main():
    configs = utils.retrieve_configs("config.json")
    logging.info(f"Starting HIL")

    # create a dictionary to represent the different physical values
    physical_values = {}
    for value in configs["database"]["physical_values"]:
        # initialise all physical values to just be an empty string (the key matters more)
        physical_values[value["name"]] = ""

    # begin physical logic simulation thread
    logic_thread = Thread(target=logic.logic, args=(physical_values,))
    logic_thread.daemon = True
    logic_thread.start()

    # begin the database output writing thread
    db_in_thread = Thread(target=output_data, args=(configs, physical_values))
    db_in_thread.daemon = True
    db_in_thread.start()

    # begin database input reading thread
    db_out_thread = Thread(target=input_data, args=(configs, physical_values))
    db_out_thread.daemon = True
    db_out_thread.start()

    # wait for threads
    logic_thread.join()
    db_in_thread.join()
    db_out_thread.join()
    

if __name__ == "__main__":
    asyncio.run(main())