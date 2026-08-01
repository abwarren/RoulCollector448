"""Fixture DB + env setup. Runs before test modules import backend.app."""

import datetime
import os
import pathlib
import sqlite3

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"
FIXTURE_DIR.mkdir(exist_ok=True)
FIXTURE_DB = FIXTURE_DIR / "fixture.db"
if FIXTURE_DB.exists():
    FIXTURE_DB.unlink()

REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}

conn = sqlite3.connect(FIXTURE_DB)
conn.execute(
    """CREATE TABLE roulette_spins (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        number      INTEGER NOT NULL,
        description TEXT NOT NULL,
        color       TEXT NOT NULL,
        game_id     TEXT NOT NULL UNIQUE,
        server_ts   TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )"""
)

# 30 full cycles of 0..36 = 1110 spins, deterministic (hits = 30 each).
# Last spin is 36; timestamps end ~44s ago so health reports alive.
now = datetime.datetime.now()
for i in range(1110):
    n = i % 37
    color = "Green" if n == 0 else ("Red" if n in REDS else "Black")
    ts = (now - datetime.timedelta(seconds=(1110 - i) * 44)).isoformat()
    conn.execute(
        "INSERT INTO roulette_spins (number, description, color, game_id, server_ts, captured_at) "
        "VALUES (?,?,?,?,?,?)",
        (n, f"{n} {color}", color, f"g{i}", ts, ts),
    )
conn.commit()
conn.close()

os.environ["RC_DB_PATH"] = str(FIXTURE_DB)
