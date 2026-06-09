"""
Integration test configuration.
Requires the solar_plant Docker stack to be running:

    python3 main.py config/solar_plant
    docker compose build
    docker compose up -d

Tests use the SCADA master REST API (port 1111) and the shared SQLite DB.
"""
import os
import sqlite3
import time
import pytest
import requests

MASTER_BASE   = os.environ.get("MASTER_URL",  "http://localhost:1111")
OUTSTATION_1  = os.environ.get("OS1_URL",     "http://localhost:1112")  # if exposed
DB_PATH       = os.environ.get("DB_PATH",
    os.path.join(os.path.dirname(__file__),
                 "../../simulation/communications/physical_interactions.db"))


def wait_for_service(url, timeout=30):
    """Block until the REST endpoint responds or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{url}/registers", timeout=2)
            if r.status_code == 200:
                return True
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(1)
    return False


@pytest.fixture(scope="session")
def master_url():
    if not wait_for_service(MASTER_BASE, timeout=30):
        pytest.skip("SCADA master not reachable — start Docker stack first")
    return MASTER_BASE


@pytest.fixture(scope="session")
def db_conn():
    if not os.path.exists(DB_PATH):
        pytest.skip(f"SQLite DB not found at {DB_PATH} — run main.py first")
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 3000;")
    yield conn
    conn.close()


def db_latest(conn, table):
    """Return the most recent value from a physical_value table."""
    row = conn.execute(
        f"SELECT value FROM {table} ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    return float(row[0]) if row and row[0] not in (None, "") else None


def db_write(conn, table, value, hil="solar_hil"):
    """Inject a value directly into the DB (simulates HIL or attacker write)."""
    conn.execute(
        f"INSERT INTO {table}(value, hil) VALUES(?, ?)", (str(value), hil)
    )
    conn.commit()
