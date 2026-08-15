"""Observation store + collector session lifecycle (Phase 1).

Immutability: spin_observations rows are never updated or deleted. Repairs
(Phase 4) change canonical data only — the raw evidence stays for forensics.

Payload-hash dedup: an observation's identity is its content
(source + game_id + number + server_ts). Identical content from the same
source is recorded once — this makes restart replay and WS join-snapshots
(Phase 3) idempotent. The SAME spin seen via different sources (websocket vs
dom) is deliberately kept as two observations: that is the cross-validation
evidence the integrity engine needs.

Import strategy: this module is used both as part of the `collector` package
(tests, repo layout) and as a flat sibling of the deployed collector script.
"""

import hashlib
import json
import secrets
from datetime import datetime, timezone

try:                                    # package context (repo/tests)
    from . import schema
except ImportError:                     # flat script context (deployed box)
    import schema  # type: ignore

_VALID_SOURCES = {"websocket", "dom", "history", "reconciled", "backfilled", "manual"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_session_id() -> str:
    """e.g. 2026-08-15T04:32:01Z-7f92a3b1 — unique per collector session.

    4 hex bytes (32 bits) of entropy: 200 IDs in the same second collide
    with probability ~4e-6 (2 hex bytes flaked at ~30% in tests)."""
    return f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}-{secrets.token_hex(4)}"


def payload_hash(source: str, game_id, number, server_ts) -> str:
    """Content hash — deterministic, independent of session/observed_at."""
    canonical = json.dumps(
        [source, game_id, number, server_ts],
        sort_keys=True, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
def start_session(conn, source: str = "cdp-ws") -> str:
    """Open a collector session; returns its id."""
    sid = new_session_id()
    conn.execute(
        "INSERT INTO collector_sessions (id, started_at, status, source) "
        "VALUES (?, ?, 'ACTIVE', ?)",
        (sid, now_iso(), source),
    )
    conn.commit()
    return sid


def end_session(conn, session_id: str, spins_captured: int = 0,
                status: str = "ENDED") -> None:
    """Close a session (ENDED on clean exit, CRASHED on exception/abandon)."""
    conn.execute(
        "UPDATE collector_sessions SET ended_at = ?, status = ?, "
        "spins_captured = ? WHERE id = ?",
        (now_iso(), status, spins_captured, session_id),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Observations
# --------------------------------------------------------------------------
def record_observation(conn, *, source: str, session_id: str, game_id=None,
                       number=None, description=None, server_ts=None,
                       raw_payload=None, sequence_hint=None):
    """Persist one raw observation. Returns the row id, or None if the
    identical content was already observed (dedup). Never mutates existing
    rows."""
    if source not in _VALID_SOURCES:
        raise ValueError(f"invalid observation source: {source!r}")
    h = payload_hash(source, game_id, number, server_ts)
    cur = conn.execute(
        "INSERT OR IGNORE INTO spin_observations "
        "(observed_at, source, session_id, game_id, number, description, "
        " server_ts, payload_hash, raw_payload, sequence_hint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now_iso(), source, session_id, game_id, number, description,
         server_ts, h, raw_payload, sequence_hint),
    )
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def flush_observations(conn, buffer) -> int:
    """Batch-persist buffered observation tuples (kwargs dicts). Returns the
    number of new rows written. The collector keeps an in-memory buffer and
    flushes alongside its canonical 25-spin save, so write amplification is
    unchanged."""
    written = 0
    for obs in buffer:
        if record_observation(conn, **obs) is not None:
            written += 1
    buffer.clear()
    return written


# --------------------------------------------------------------------------
# Integrity events (audit trail)
# --------------------------------------------------------------------------
def log_event(conn, event_type: str, severity: str = "INFO", game_id=None,
              details=None, root_cause=None) -> int:
    """Append an integrity event. Returns the row id."""
    cur = conn.execute(
        "INSERT INTO integrity_events "
        "(created_at, event_type, severity, game_id, details, root_cause) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (now_iso(), event_type, severity, game_id,
         json.dumps(details) if details is not None else None, root_cause),
    )
    conn.commit()
    return cur.lastrowid
