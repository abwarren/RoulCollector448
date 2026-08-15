"""Phase 1 — integrity schema + observation store + session lifecycle.

Covers: fresh-schema creation, additive migration of an existing dataset,
session start/end + SESSION events, immutable observation recording with
content-hash dedup (restart replay, cross-source retention).
"""

import sqlite3

import pytest

from collector import observer, schema


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    yield conn
    conn.close()


def _old_schema_conn(tmp_path):
    """A pre-integrity collector DB (original 7-column roulette_spins)."""
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
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
    for i in range(3):
        conn.execute(
            "INSERT INTO roulette_spins (number, description, color, game_id, "
            "server_ts, captured_at) VALUES (?,?,?,?,?,?)",
            (i, f"{i} {['Green','Red','Black'][i]}", ["Green", "Red", "Black"][i],
             f"g{i}", "2026-08-15T00:00:00", "2026-08-15T00:00:00"),
        )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def test_fresh_schema_creates_all_tables(db):
    tables = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"roulette_spins", "spin_observations", "collector_sessions",
            "integrity_events", "repair_events"} <= tables


def test_fresh_spins_has_new_columns(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(roulette_spins)").fetchall()}
    for c in ("sequence_no", "source", "confidence", "status",
              "first_seen_at", "last_verified_at", "verification_version"):
        assert c in cols


def test_migration_preserves_existing_rows(tmp_path):
    conn = _old_schema_conn(tmp_path)
    schema.ensure_schema(conn)  # the migration path
    rows = conn.execute("SELECT * FROM roulette_spins ORDER BY id").fetchall()
    assert len(rows) == 3                      # data intact
    assert rows[0]["game_id"] == "g0"
    assert rows[0]["status"] == "VALID"        # defaults applied
    assert rows[0]["confidence"] == 0.95
    cols = {r[1] for r in conn.execute("PRAGMA table_info(roulette_spins)").fetchall()}
    assert "sequence_no" in cols
    # idempotent: a second pass changes nothing
    schema.ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0] == 3


def test_observation_table_enforces_sources(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO spin_observations (observed_at, source, session_id) "
            "VALUES ('2026-08-15', 'bogus', 's1')"
        )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def test_session_lifecycle(db):
    sid = observer.start_session(db, source="cdp-ws")
    row = db.execute("SELECT * FROM collector_sessions WHERE id=?",
                     (sid,)).fetchone()
    assert row["status"] == "ACTIVE"
    assert row["source"] == "cdp-ws"

    observer.end_session(db, sid, spins_captured=123, status="ENDED")
    row = db.execute("SELECT * FROM collector_sessions WHERE id=?",
                     (sid,)).fetchone()
    assert row["status"] == "ENDED"
    assert row["spins_captured"] == 123
    assert row["ended_at"] is not None


def test_session_ids_unique():
    ids = {observer.new_session_id() for _ in range(200)}
    assert len(ids) == 200


def test_session_events_logged(db):
    sid = observer.start_session(db)
    observer.log_event(db, "SESSION_START", details={"resumed_spins": 5})
    observer.log_event(db, "SESSION_END", details={"total": 5, "ok": True})
    events = db.execute(
        "SELECT event_type, details FROM integrity_events ORDER BY id").fetchall()
    assert [e["event_type"] for e in events] == ["SESSION_START", "SESSION_END"]
    assert '"resumed_spins": 5' in events[0]["details"]


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------
def _obs(session="s1", game="g100", number=17, ts="2026-08-15T04:00:00Z",
         source="websocket", **overrides):
    d = {"source": source, "session_id": session, "game_id": game,
         "number": number, "description": f"{number} Black", "server_ts": ts,
         "raw_payload": '{"gameId":"g100","code":17}'}
    d.update(overrides)
    return d


def test_observation_roundtrip(db):
    sid = observer.start_session(db)
    oid = observer.record_observation(db, **_obs(session=sid))
    assert oid is not None
    row = db.execute("SELECT * FROM spin_observations WHERE id=?", (oid,)).fetchone()
    assert row["game_id"] == "g100"
    assert row["number"] == 17
    assert row["source"] == "websocket"
    assert row["session_id"] == sid
    assert row["validation_status"] == "PENDING"
    assert len(row["payload_hash"]) == 64


def test_observation_content_dedup(db):
    """Identical content from the same source is recorded once."""
    sid = observer.start_session(db)
    first = observer.record_observation(db, **_obs(session=sid))
    second = observer.record_observation(db, **_obs(session=sid))
    assert first is not None
    assert second is None                     # deduped
    assert db.execute("SELECT COUNT(*) FROM spin_observations").fetchone()[0] == 1


def test_restart_replay_dedup(db):
    """A new session replaying the same content (WS snapshot after restart)
    must not double-record."""
    s1 = observer.start_session(db)
    observer.record_observation(db, **_obs(session=s1))
    s2 = observer.start_session(db)
    again = observer.record_observation(db, **_obs(session=s2))
    assert again is None
    assert db.execute("SELECT COUNT(*) FROM spin_observations").fetchone()[0] == 1


def test_cross_source_both_kept(db):
    """The same spin seen via websocket AND dom is two observations —
    the cross-validation evidence the integrity engine needs."""
    sid = observer.start_session(db)
    observer.record_observation(db, **_obs(session=sid))
    observer.record_observation(db, **_obs(session=sid, source="dom",
                                           game_id=None, ts=None,
                                           raw_payload=".result-number: 17"))
    rows = db.execute("SELECT source FROM spin_observations ORDER BY id").fetchall()
    assert [r["source"] for r in rows] == ["websocket", "dom"]


def test_invalid_source_rejected(db):
    with pytest.raises(ValueError):
        observer.record_observation(db, source="magic", session_id="s1")


def test_flush_observations(db):
    sid = observer.start_session(db)
    buf = [_obs(session=sid), _obs(session=sid, game="g101", number=0)]
    written = observer.flush_observations(db, buf)
    assert written == 2
    assert buf == []                          # buffer drained
    assert db.execute("SELECT COUNT(*) FROM spin_observations").fetchone()[0] == 2


def test_payload_hash_stable_across_sessions():
    h1 = observer.payload_hash("websocket", "g100", 17, "2026-08-15T04:00:00Z")
    h2 = observer.payload_hash("websocket", "g100", 17, "2026-08-15T04:00:00Z")
    assert h1 == h2
    assert observer.payload_hash("dom", None, 17, None) != h1


def test_event_log_fields(db):
    eid = observer.log_event(db, "TIMESTAMP_ANOMALY", severity="WARNING",
                             game_id="g100", details={"latency_s": 612},
                             root_cause="NETWORK")
    row = db.execute("SELECT * FROM integrity_events WHERE id=?", (eid,)).fetchone()
    assert row["event_type"] == "TIMESTAMP_ANOMALY"
    assert row["severity"] == "WARNING"
    assert row["game_id"] == "g100"
    assert row["root_cause"] == "NETWORK"
    assert '"latency_s": 612' in row["details"]
