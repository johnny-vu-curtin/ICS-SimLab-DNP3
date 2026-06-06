# DNP3 Library Notes

## Decision: use dnp3-python only

| Library       | Status      | Python support     | Install method          | Verdict     |
|---------------|-------------|-------------------|-------------------------|-------------|
| `pydnp3`      | Dead (2018) | 2.7, 3.5, 3.6 only| Source build — **fails**| Do NOT use  |
| `dnp3-python` | Active      | >= 3.8            | `pip install dnp3-python`| **Use this**|

### Why pydnp3 fails

Verified on Ubuntu 24.04 + Python 3.12:
- Requires `cmake` and C++14 toolchain to build from source.
- C++ source hardcodes `python2.7/Python.h` — build fails with `No such file or directory`.
- No pre-built wheels exist for Python >= 3.7.
- Last commit: 2018. No active maintenance.

### dnp3-python (VOLTTRON/PNNL)

- PyPI package: `dnp3-python`
- Maintainer: Pacific Northwest National Laboratory (PNNL) / VOLTTRON team
- GitHub: https://github.com/VOLTTRON/dnp3-python
- Wraps the same `opendnp3` C++ stack via pre-built wheels.
- Supports both Master and Outstation roles.
- Supports unsolicited responses, time synchronisation, event classes.
- SAv5 support: partial (available in underlying opendnp3, exposed via library API).

---

## Docker base image constraint

`dnp3-python` pre-built wheels are tested against:
- Ubuntu 20.04 + Python 3.8/3.9
- Ubuntu 22.04 + Python 3.10/3.11

**Not tested on Ubuntu 24.04 or Python 3.12.**

On Ubuntu 24.04 + Python 3.12: `pip install dnp3-python` returns `No matching distribution found`.

### Required Dockerfile base for DNP3 containers

```dockerfile
FROM ubuntu:22.04

RUN apt-get update && apt-get install -y \
    python3.11 python3.11-dev python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install dnp3-python
```

The existing Modbus component Dockerfile (`src/docker-files/component/Dockerfile`) uses a different base. DNP3 containers need a **separate Dockerfile** at `src/docker-files/dnp3/Dockerfile`.

---

## Verify installation in Docker

Run before implementing to confirm the library works in the target environment:

```bash
docker run --rm ubuntu:22.04 bash -c "
  apt-get update -q &&
  apt-get install -y -q python3-pip &&
  pip install dnp3-python &&
  python3 -c 'import pydnp3; print(pydnp3.__version__)'
"
```

Expected: version string printed. If it fails, check for a newer wheel release on PyPI.

---

## Basic API usage (dnp3-python)

### Outstation

```python
from pydnp3 import asiodnp3, asiopal, opendnp3, openpal

manager = asiodnp3.DNP3Manager(1, asiodnp3.ConsoleLogger().Create())
channel = manager.AddTCPServer("server", ..., "0.0.0.0", 20000, ...)
outstation = channel.AddOutstation("outstation", command_handler, app, config)
outstation.Enable()

# Update analogue input
builder = asiodnp3.UpdateBuilder()
builder.Update(opendnp3.Analog(value, opendnp3.Flags(opendnp3.AnalogQuality.ONLINE)), index)
outstation.Apply(builder.Build())
```

### Master

```python
master = channel.AddMaster("master", soe_handler, app, config)
master.Enable()

# Integrity poll (Class 0)
master.ScanAllObjects(opendnp3.GroupVariationID(60, 1), opendnp3.TaskConfig().Default())

# Direct Operate — Binary Output
command = opendnp3.ControlRelayOutputBlock(opendnp3.ControlCode.LATCH_ON)
master.DirectOperate(command, index, callback, opendnp3.TaskConfig().Default())
```

Full API reference: https://github.com/VOLTTRON/dnp3-python/tree/main/docs

---

## Known issues

- `dnp3-python` is still in beta (`0.3.0b2` as of Nov 2024). API may change between minor versions. Pin the version in requirements: `dnp3-python==0.3.0b2`.
- The library imports as `pydnp3` (not `dnp3_python`) — this is intentional; it wraps the same opendnp3 internal namespace.
- Time synchronisation (Group 50) requires the master to call `PerformTimeSync()` after connection — not automatic.
- Unsolicited responses must be explicitly enabled by the master after the initial integrity poll.
