"""Integrity-layer schema: additive, idempotent, safe on the live collector DB.

The collector (single writer) calls ensure_schema() at startup. On an existing
dataset this adds the new tables and expands roulette_spins with the canonical
columns (guarded ALTERs — never rewrites data). On a fresh DB it creates the
full schema including the expanded canonical table.

DB path is env-overridable (RC_DB_PATH) so tests and the dashboard can point
elsewhere; default matches the live collector DB.
"""

import os
import sqlite3

DB_PATH = os.environ.get("RC_DB_PATH", "/home/wa/roulette2_spins.db")

# --------------------------------------------------------------------------
# Full canonical table for NEW installs. Live DBs are migrated instead.
# --------------------------------------------------------------------------
CANONICAL_DDL = """
CREATE TABLE IF NOT EXISTS roulette_spins (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    number              INTEGER NOT NULL,
    description         TEXT NOT NULL,
    color               TEXT NOT NULL CHECK(color IN ('Red', 'Black', 'Green')),
    game_id             TEXT NOT NULL UNIQUE,
    server_ts           TEXT NOT NULL,
    captured_at         TEXT NOT NULL,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sequence_no         INTEGER,
    source              TEXT DEFAULT 'websocket',
    confidence          REAL DEFAULT 0.95,
    status              TEXT DEFAULT 'VALID',
    first_seen_at       TEXT,
    last_verified_at    TEXT,
    verification_version INTEGER DEFAULT 0
);
"""

# Run AFTER column migration — idx_spin_seq references sequence_no, which does
# not exist on a legacy table until the guarded ALTERs above have run.
CANONICAL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_spin_number ON roulette_spins(number);
CREATE INDEX IF NOT EXISTS idx_spin_color  ON roulette_spins(color);
CREATE INDEX IF NOT EXISTS idx_spin_ts     ON roulette_spins(server_ts);
CREATE INDEX IF NOT EXISTS idx_spin_seq    ON roulette_spins(sequence_no);
"""

# --------------------------------------------------------------------------
# New tables — immutable raw evidence, sessions, event audit, repair queue.
# --------------------------------------------------------------------------
NEW_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS collector_sessions (
    id              TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    status          TEXT NOT NULL DEFAULT 'ACTIVE',
    spins_captured  INTEGER DEFAULT 0,
    source          TEXT,
    window_achieved INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON collector_sessions(started_at);

CREATE TABLE IF NOT EXISTS spin_observations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at       TEXT NOT NULL,
    source            TEXT NOT NULL
                      CHECK(source IN ('websocket','dom','history',
                                       'reconciled','backfilled','manual')),
    session_id        TEXT NOT NULL,
    game_id           TEXT,
    number            INTEGER,
    description       TEXT,
    server_ts         TEXT,
    payload_hash      TEXT NOT NULL,
    raw_payload       TEXT,
    sequence_hint     INTEGER,
    validation_status TEXT NOT NULL DEFAULT 'PENDING',
    UNIQUE (source, payload_hash)
);
CREATE INDEX IF NOT EXISTS idx_obs_game ON spin_observations(game_id);
CREATE INDEX IF NOT EXISTS idx_obs_ts   ON spin_observations(observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_sess ON spin_observations(session_id);

CREATE TABLE IF NOT EXISTS integrity_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity   TEXT NOT NULL DEFAULT 'INFO',
    game_id    TEXT,
    details    TEXT,
    root_cause TEXT
);
CREATE INDEX IF NOT EXISTS idx_ie_ts   ON integrity_events(created_at);
CREATE INDEX IF NOT EXISTS idx_ie_type ON integrity_events(event_type);

CREATE TABLE IF NOT EXISTS repair_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    incident_type   TEXT NOT NULL,
    start_game_id   TEXT,
    end_game_id     TEXT,
    affected_count  INTEGER,
    status          TEXT NOT NULL DEFAULT 'OPEN',
    attempts        INTEGER DEFAULT 0,
    last_attempt_at TEXT,
    resolved_at     TEXT,
    resolution      TEXT,
    details         TEXT
);
CREATE INDEX IF NOT EXISTS idx_re_status ON repair_events(status);
"""

# Columns added to an EXISTING roulette_spins (additive migration).
CANONICAL_ADD_COLUMNS = [
    ("sequence_no", "INTEGER"),
    ("source", "TEXT DEFAULT 'websocket'"),
    ("confidence", "REAL DEFAULT 0.95"),
    ("status", "TEXT DEFAULT 'VALID'"),
    ("first_seen_at", "TEXT"),
    ("last_verified_at", "TEXT"),
    ("verification_version", "INTEGER DEFAULT 0"),
]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create new tables + indexes and expand roulette_spins. Idempotent."""
    conn.executescript(CANONICAL_DDL)
    conn.executescript(NEW_TABLES_DDL)
    for col, ddl in CANONICAL_ADD_COLUMNS:
        if not _column_exists(conn, "roulette_spins", col):
            conn.execute(f"ALTER TABLE roulette_spins ADD COLUMN {col} {ddl}")
    conn.executescript(CANONICAL_INDEXES)
    conn.commit()


def connect() -> sqlite3.Connection:
    """Writer connection (WAL, busy timeout) for the collector / tests."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
