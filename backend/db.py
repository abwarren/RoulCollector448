"""Read-only SQLite access to the collector DB."""

import os
import pathlib
import sqlite3


def default_db_path() -> str:
    """DB path: RC_DB_PATH env override, else per-OS default (mirror of
    collector/schema.py — kept independent so the backend can run standalone)."""
    env = os.environ.get("RC_DB_PATH")
    if env:
        return env
    if os.name == "nt":
        return os.path.join(os.path.expanduser("~"), ".roulette2", "roulette2_spins.db")
    return "/home/wa/roulette2_spins.db"


DB_PATH = default_db_path()


def connect() -> sqlite3.Connection:
    """Open a read-only connection. Collector remains the single writer."""
    pathlib.Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
