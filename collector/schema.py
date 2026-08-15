"""Integrity-layer schema: additive, idempotent, safe on the live collector DB.

The collector (single writer) calls ensure_schema() at startup. On an existing
dataset this adds the new tables and expands roulette_spins with the canonical
columns (guarded ALTERs — never rewrites data). On a fresh DB it creates the
full schema including the expanded canonical table.

DB path is env-overridable (RC_DB_PATH) so tests and the dashboard can point
elsewhere; default matches the live collector DB.
"""

import os
import pathlib
import sqlite3


def default_db_path() -> str:
    """DB path: RC_DB_PATH env override, else per-OS default.

    Windows: %USERPROFILE%\\.roulette2\
oulette2_spins.db (a private data dir,
    mirroring the Linux /home/wa convention). The dir is created lazily by
    connect() so read-only consumers (dashboard) never fail on import.
    """
    env = os.environ.get("RC_DB_PATH")
    if env:
        return env
    if os.name == "nt":
        return os.path.join(os.path.expanduser("~"), ".roulette2", "roulette2_spins.db")
    return "/home/wa/roulette2_spins.db"


DB_PATH = default_db_path()

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
    verification_version INTEGER DEFAULT 0,
    dedup_key           TEXT,
    observed_at         TEXT,
    committed_at        TEXT,
    capture_latency     REAL,
    commit_latency      REAL
);
"""

# Run AFTER column migration — idx_spin_seq references sequence_no, which does
# not exist on a legacy table until the guarded ALTERs above have run.
CANONICAL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_spin_number ON roulette_spins(number);
CREATE INDEX IF NOT EXISTS idx_spin_color  ON roulette_spins(color);
CREATE INDEX IF NOT EXISTS idx_spin_ts     ON roulette_spins(server_ts);
CREATE INDEX IF NOT EXISTS idx_spin_seq    ON roulette_spins(sequence_no);
-- Storage-level uniqueness beyond game_id (PRD: game_id alone is
-- insufficient if malformed/missing ids occur). dedup_key is the canonical
-- identity: 'gid:<game_id>' when the id is valid, else 'tsn:<server_ts>|<n>'
-- (same spin arriving under different/malformed ids still dedupes). Rows
-- whose identity cannot be established (None) are never inserted canonically.
CREATE UNIQUE INDEX IF NOT EXISTS idx_spin_dedup
    ON roulette_spins(dedup_key) WHERE dedup_key IS NOT NULL;
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
    ("dedup_key", "TEXT"),
    ("observed_at", "TEXT"),
    ("committed_at", "TEXT"),
    ("capture_latency", "REAL"),
    ("commit_latency", "REAL"),
]


def canonical_dedup_key(game_id, server_ts=None, number=None) -> str | None:
    """Storage-level identity for a canonical spin (dedup_key).

    game_id when valid (non-empty after strip) — the strongest identity;
    else server_ts+number — catches the same spin arriving under a
    malformed/empty id; else None — identity cannot be established, and the
    caller must NOT insert a canonical row (the spin stays an observation
    and is surfaced, never silently dropped or guessed).
    """
    gid = str(game_id or "").strip()
    if gid:
        return f"gid:{gid}"
    if server_ts is not None and number is not None:
        return f"tsn:{server_ts}|{number}"
    return None


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
    # Backfill dedup_key for legacy rows BEFORE the unique index is created:
    # game_id is NOT NULL UNIQUE, so 'gid:'||game_id keys are already unique.
    conn.execute(
        "UPDATE roulette_spins SET dedup_key = 'gid:' || game_id "
        "WHERE dedup_key IS NULL AND game_id IS NOT NULL"
    )
    conn.executescript(CANONICAL_INDEXES)
    conn.commit()


def connect() -> sqlite3.Connection:
    """Writer connection (WAL, busy timeout) for the collector / tests."""
    pathlib.Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
