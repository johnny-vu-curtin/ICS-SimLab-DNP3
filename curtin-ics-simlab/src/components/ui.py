#!/usr/bin/env python3

# -----------------------------------------------------------------------------
# Project: Curtin ICS-SimLab
# File: ui.py
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
# Last Modified: 2025-08-17
# -----------------------------------------------------------------------------

# FILE PURPOSE: Creates a streamlit dashboard to view data from all components. Data
#               is fetched from other components using a RESTful API

import requests
import json
import time
import sqlite3
import streamlit as st
import pandas as pd
import altair as alt
                  
# FUNCTION: retrieve_configs
# PURPOSE:  Retrieves the JSON configs
def retrieve_configs(filename):
    with open(filename, "r") as config_file:
        content = config_file.read()
        configs = json.loads(content)
    return configs



# FUNCTION: get_component_info
# PURPOSE:  Returns all names and info for all the components in lists
def get_component_info(configs):
    hmi_info = {}
    plc_info = {}
    sensor_info = {}
    actuator_info = {}
    hil_info = {}
    dnp3_master_info = {}
    dnp3_outstation_info = {}

    if "hmis" in configs:
        for hmi in configs["hmis"]:
            hmi_info[hmi["name"]] = {
                "ip": hmi["network"]["ip"]
            }

    if "plcs" in configs:
        for plc in configs["plcs"]:
            plc_info[plc["name"]] = {
                "ip": plc["network"]["ip"]
            }

    if "sensors" in configs:
        for sensor in configs["sensors"]:
            sensor_info[sensor["name"]] = {
                "ip": sensor["network"]["ip"],
            }

    if "actuators" in configs:
        for actuator in configs["actuators"]:
            actuator_info[actuator["name"]] = {
                "ip": actuator["network"]["ip"],
            }

    if "hils" in configs:
        for hil in configs["hils"]:
            physical_values = []
            for physical_value in hil["physical_values"]:
                # Only chart "output" values — inputs are written by external components
                # (e.g. DNP3 outstation commands) and only have data when commands are sent.
                if physical_value.get("io") == "output":
                    physical_values.append(physical_value["name"])
            hil_info[hil["name"]] = {
                "values": physical_values
            }

    if "dnp3_masters" in configs:
        for master in configs["dnp3_masters"]:
            dnp3_master_info[master["name"]] = {
                "ip": master["network"]["ip"]
            }

    if "dnp3_outstations" in configs:
        for outstation in configs["dnp3_outstations"]:
            dnp3_outstation_info[outstation["name"]] = {
                "ip": outstation["network"]["ip"]
            }

    return hmi_info, plc_info, sensor_info, actuator_info, hil_info, dnp3_master_info, dnp3_outstation_info



# Engineering unit per physical value name. DNP3 master responses prefix the key
# with the outstation name (e.g. "inverter_1.voltage_ac") — get_unit() strips that
# prefix before lookup.
PHYSICAL_VALUE_UNITS = {
    "voltage_ac":                  "V",
    "current_ac":                  "A",
    "active_power":                "W",
    "frequency":                   "Hz",
    "solar_irradiance":            "W/m²",
    "panel_temperature":           "°C",
    "power_curtailment":           "W",
    "output_voltage":              "V",
    "transformer_voltage":         "V",
    "household_power":             "W",
    "solar_power":                 "W",
    "grid_voltage":                "V",
    "grid_frequency":              "Hz",
    "tank_level_value":            "%",
    "bottle_level_value":          "%",
    "bottle_distance_to_filler_value": "mm",
}

# Keyword fallback for physical values not in PHYSICAL_VALUE_UNITS above
# (covers naming variations without needing an exact-match entry per scenario).
PHYSICAL_VALUE_UNIT_KEYWORDS = [
    ("voltage", "V"),
    ("current", "A"),
    ("power", "W"),
    ("frequency", "Hz"),
    ("irradiance", "W/m²"),
    ("temperature", "°C"),
]


# Author: Van Sanh Vu, Purpose: DNP3 development
# FUNCTION: get_unit
# PURPOSE:  Looks up the engineering unit for a physical value name. Returns ""
#           for booleans/states/positions or anything not recognised.
def get_unit(physical_value_name):
    # DNP3 master keys are "outstation_name.physical_value" — match on the suffix
    name = physical_value_name.rsplit(".", 1)[-1]
    if name in PHYSICAL_VALUE_UNITS:
        return PHYSICAL_VALUE_UNITS[name]
    for keyword, unit in PHYSICAL_VALUE_UNIT_KEYWORDS:
        if keyword in name:
            return unit
    return ""



# Author: Van Sanh Vu, Purpose: DNP3 development
# FUNCTION: create_register_table_rows
# PURPOSE:  Builds up the table rows for the component registers
def create_register_table_rows(type, address, count, value, unit, response):
    for name, register in response.items():
        type.append(register["type"])
        address.append(register["address"])
        count.append(register["count"])
        value.append(register["value"])
        unit.append(get_unit(name))



# Author: Van Sanh Vu, Purpose: DNP3 development
# FUNCTION: create_register_table
# PURPOSE:  Creates a dataframe for the component register table
def create_register_table(response):
    type = []
    address = []
    count = []
    value = []
    unit = []
    create_register_table_rows(type, address, count, value, unit, response)
    dataframe = pd.DataFrame({
            "type": type,
            "address": address,
            "count": count,
            "value": value,
            "unit": unit
        })
    return dataframe.astype(str)



# FUNCTION: main
# PURPOSE:  The main execution. Here we render everything to show on the web user interface.
def main():
    time.sleep(10)

    # render the streamlit application
    st.set_page_config(
        page_title="ICS Dashboard",
        layout="wide",
    )

    if 'config_loaded' not in st.session_state:
        # retrieve configurations from the given JSON (will be in the same directory)
        configs = retrieve_configs("config.json")

        # get all component info. for Modbus (name, ip), for physical (column_name)
        hmi_info, plc_info, sensor_info, actuator_info, hil_info, dnp3_master_info, dnp3_outstation_info = get_component_info(configs)
        st.session_state['hmi_info'] = hmi_info
        st.session_state['plc_info'] = plc_info
        st.session_state['sensor_info'] = sensor_info
        st.session_state['actuator_info'] = actuator_info
        st.session_state['hil_info'] = hil_info
        st.session_state['dnp3_master_info'] = dnp3_master_info
        st.session_state['dnp3_outstation_info'] = dnp3_outstation_info
        st.session_state['config_loaded'] = True
    else:
        hmi_info = st.session_state['hmi_info']
        plc_info = st.session_state['plc_info']
        sensor_info = st.session_state['sensor_info']
        actuator_info = st.session_state['actuator_info']
        hil_info = st.session_state['hil_info']
        dnp3_master_info = st.session_state['dnp3_master_info']
        dnp3_outstation_info = st.session_state['dnp3_outstation_info']

    # render everything first  
    st.title("Industrial Control System Dashboard")

    # show system information
    col1, _, _, _ = st.columns(4)
    with col1:
        with st.container(border=True):
            st.subheader("Devices", divider="orange")
            st.write(f"Human Machine Interfaces (HMIs): {len(hmi_info)}")
            st.write(f"Programmable Logic Controllers (PLCs): {len(plc_info)}")
            st.write(f"Sensors: {len(sensor_info)}")
            st.write(f"Actuators: {len(actuator_info)}")
            if dnp3_master_info:
                st.write(f"DNP3 Masters (SCADA): {len(dnp3_master_info)}")
            if dnp3_outstation_info:
                st.write(f"DNP3 Outstations (Inverters): {len(dnp3_outstation_info)}")
    st.divider()

    # show register devices
    st.header("Human Machine Interfaces", divider="orange")
    col1, col2, col3, col4 = st.columns(4)
    columns = {1: col1, 2: col2, 3: col3, 4: col4}
    column_switcher = 1
    st_hmis = {}
    for hmi in hmi_info:
        with columns[column_switcher].container():
            st.markdown(f"##### {hmi}")
            st_hmis[hmi] = st.empty()
            column_switcher = (column_switcher % len(columns)) + 1
            
    st.header("Programmable Logic Controllers", divider="orange")
    col1, col2, col3, col4 = st.columns(4)
    columns = {1: col1, 2: col2, 3: col3, 4: col4}
    column_switcher = 1
    st_plcs = {}
    for plc in plc_info:
        with columns[column_switcher].container():
            st.markdown(f"##### {plc}")
            st_plcs[plc] = st.empty()
            column_switcher = (column_switcher % len(columns)) + 1

    st.header("Sensors", divider="orange")
    col1, col2, col3, col4 = st.columns(4)
    columns = {1: col1, 2: col2, 3: col3, 4: col4}
    column_switcher = 1
    st_sensors = {}
    for sensor in sensor_info:
        with columns[column_switcher].container():
            st.markdown(f"##### {sensor}")
            st_sensors[sensor] = st.empty()
            column_switcher = (column_switcher % len(columns)) + 1
    
    st.header("Actuators", divider="orange")
    col1, col2, col3, col4 = st.columns(4)
    columns = {1: col1, 2: col2, 3: col3, 4: col4}
    column_switcher = 1
    st_actuators = {}
    for actuator in actuator_info:
        with columns[column_switcher].container():
            st.markdown(f"##### {actuator}")
            st_actuators[actuator] = st.empty()
            column_switcher = (column_switcher % len(columns)) + 1
    
    if dnp3_master_info:
        st.header("DNP3 Masters (SCADA)", divider="blue")
        col1, col2, col3, col4 = st.columns(4)
        columns = {1: col1, 2: col2, 3: col3, 4: col4}
        column_switcher = 1
        st_dnp3_masters = {}
        for master in dnp3_master_info:
            with columns[column_switcher].container():
                st.markdown(f"##### {master}")
                st_dnp3_masters[master] = st.empty()
                column_switcher = (column_switcher % len(columns)) + 1

    if dnp3_outstation_info:
        st.header("DNP3 Outstations (Inverters)", divider="blue")
        col1, col2, col3, col4 = st.columns(4)
        columns = {1: col1, 2: col2, 3: col3, 4: col4}
        column_switcher = 1
        st_dnp3_outstations = {}
        for outstation in dnp3_outstation_info:
            with columns[column_switcher].container():
                st.markdown(f"##### {outstation}")
                st_dnp3_outstations[outstation] = st.empty()
                column_switcher = (column_switcher % len(columns)) + 1

    # create hardware in the loop graphs
    st.divider()
    st.header("Hardware-in-the-Loops")
    st.subheader("Physical Values") 
    hils = {}
    graphs = {}

    col1, col2, col3 = st.columns(3)
    columns = {1: col1, 2: col2, 3: col3}
    column_switcher = 1

    for hil in hil_info.values():
        for physical_value in hil["values"]:
            with columns[column_switcher].container(border=True):
                hils[physical_value] = st.empty()
                graphs[physical_value] = st.empty()
            column_switcher = (column_switcher % len(columns)) + 1

    # have a single event loop for API polling (streamlit sucks for multi threaded stuff)
    while True:
        # Poll each component independently — one failure does not block the rest.
        # 2-second timeout prevents a hung container from stalling the entire loop.
        for hmi, info in hmi_info.items():
            try:
                r = requests.get(f"http://{info['ip']}:1111/registers", timeout=2).json()
                st_hmis[hmi].dataframe(create_register_table(r))
            except Exception:
                pass

        for plc, info in plc_info.items():
            try:
                r = requests.get(f"http://{info['ip']}:1111/registers", timeout=2).json()
                st_plcs[plc].dataframe(create_register_table(r))
            except Exception:
                pass

        for sensor, info in sensor_info.items():
            try:
                r = requests.get(f"http://{info['ip']}:1111/registers", timeout=2).json()
                st_sensors[sensor].dataframe(create_register_table(r))
            except Exception:
                pass

        for actuator, info in actuator_info.items():
            try:
                r = requests.get(f"http://{info['ip']}:1111/registers", timeout=2).json()
                st_actuators[actuator].dataframe(create_register_table(r))
            except Exception:
                pass

        for master, info in dnp3_master_info.items():
            try:
                r = requests.get(f"http://{info['ip']}:1111/registers", timeout=2).json()
                st_dnp3_masters[master].dataframe(create_register_table(r))
            except Exception:
                pass

        for outstation, info in dnp3_outstation_info.items():
            try:
                r = requests.get(f"http://{info['ip']}:1111/registers", timeout=2).json()
                st_dnp3_outstations[outstation].dataframe(create_register_table(r))
            except Exception:
                pass

        # poll the physical hil (through the SQLite3 database)
        conn = sqlite3.connect("/src/physical_interactions.db")
        for hil in hil_info.values():
            for physical_value in hil["values"]:
                table = physical_value
                unit = get_unit(table)
                df = pd.read_sql_query(f"SELECT value FROM {table} ORDER BY timestamp DESC LIMIT 1", conn)
                df["physical_value"] = table
                df["unit"] = unit
                hils[physical_value].dataframe(df, column_order=["physical_value", "value", "unit"])

                df = pd.read_sql_query(f"SELECT timestamp, value FROM {table} ORDER BY timestamp DESC LIMIT 100", conn)
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df["value"] = pd.to_numeric(df["value"])
                df_grouped = df.groupby('timestamp')[["value"]].mean()

                y_title = f"Value ({unit})" if unit else "Value"
                chart = alt.Chart(df_grouped.reset_index(), height=325).mark_line().encode(
                    x=alt.X("timestamp:T", title="Time", axis=alt.Axis(format="%M:%S")),
                    y=alt.Y("value:Q", title=y_title),
                )

                graphs[physical_value].altair_chart(chart)
        conn.close()

        time.sleep(1)


if __name__ == "__main__":
    main()