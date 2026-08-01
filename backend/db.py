"""Read-only SQLite access to the collector DB."""

import os
import sqlite3

DB_PATH = os.environ.get("RC_DB_PATH", "/home/wa/roulette2_spins.db")


def connect() -> sqlite3.Connection:
    """Open a read-only connection. Collector remains the single writer."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
