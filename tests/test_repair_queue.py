"""PRD §22 (configurable thresholds) + §23 (repair queue) + §24 (auto-repair
rules) verification.

The repair_events table already had the §23 schema; these tests pin the
semantics: per-incident-type queue entries, attempts/retry, and that
auto-repairs preserve raw observations (wrong-value correction + duplicate
collapse never touch spin_observations).
"""

import json
import sqlite3

import pytest

from collector import observer, schema
from collector.integrity_state import score_band, score_bands
from collector.repairer import Repairer, plan_incident_types
from collector.reconciler import HistoryRecord, RepairPlan

REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "queue.db")
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    yield conn
    conn.close()


def _spin(conn, game_id, number, ts="2026-08-15T00:00:00Z", status="VALID"):
    color = "Green" if number == 0 else ("Red" if number in REDS else "Black")
    conn.execute(
        "INSERT INTO roulette_spins (number, description, color, game_id, "
        "server_ts, captured_at, status) VALUES (?,?,?,?,?,?,?)",
        (number, f"{number} {color}", color, game_id, ts, ts, status),
    )
    conn.commit()


def _observe(conn, sid, source, number, game_id=None, server_ts="t"):
    observer.record_observation(
        conn, source=source, session_id=sid, game_id=game_id,
        number=number, description=f"{number} X", server_ts=server_ts,
    )


# ---------------------------------------------------------------------------
# §22 — configurable thresholds
# ---------------------------------------------------------------------------
def test_score_bands_defaults():
    assert score_band(100) == "VERIFIED"
    assert score_band(98) == "VERIFIED"
    assert score_band(97) == "HEALTHY"
    assert score_band(95) == "HEALTHY"
    assert score_band(94) == "DEGRADED"
    assert score_band(90) == "DEGRADED"
    assert score_band(89) == "WARNING"
    assert score_band(75) == "WARNING"
    assert score_band(74) == "CRITICAL"


def test_score_bands_configurable(monkeypatch):
    # tighten: HEALTHY only at 97+, so 96.5 drops to DEGRADED
    monkeypatch.setenv("RC_SCORE_HEALTHY", "97")
    assert score_band(97) == "HEALTHY"
    assert score_band(96.5) == "DEGRADED"
    # and WARNING floor at 80
    monkeypatch.setenv("RC_SCORE_WARNING", "80")
    assert score_band(80) == "WARNING"
    assert score_band(79) == "CRITICAL"
    # VERIFIED still 98 default
    assert score_band(98) == "VERIFIED"


def test_score_bands_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv("RC_SCORE_HEALTHY", "not-a-number")
    assert score_bands() == [
        (98, "VERIFIED"), (95, "HEALTHY"), (90, "DEGRADED"),
        (75, "WARNING"), (0, "CRITICAL"),
    ]


def test_score_bands_unset_uses_defaults(monkeypatch):
    for v in ("RC_SCORE_VERIFIED", "RC_SCORE_HEALTHY", "RC_SCORE_DEGRADED",
              "RC_SCORE_WARNING"):
        monkeypatch.delenv(v, raising=False)
    assert score_bands()[0][0] == 98


# ---------------------------------------------------------------------------
# §23 — repair queue: per-incident-type events
# ---------------------------------------------------------------------------
def test_plan_incident_types_mapping():
    plan = RepairPlan(
        missing=[HistoryRecord(game_id="g1", number=5),
                 HistoryRecord(game_id="g2", number=9)],
        corrections=[("g3", 17, 13)],
        duplicates=["g4"],
        duplicate_kinds={"g4": "CONFLICT"},
        renumber=[("g5", 3), ("g6", 4)],
    )
    types = plan_incident_types(plan)
    by_type = {t["type"]: t for t in types}
    assert by_type["MISSING_SPIN"]["count"] == 2
    assert by_type["MISSING_SPIN"]["start"] == "g1"
    assert by_type["MISSING_SPIN"]["end"] == "g2"
    assert by_type["WRONG_VALUE"]["count"] == 1
    assert by_type["CONFLICT"]["start"] == "g4"     # CONFLICT kind, not DUPLICATE
    assert by_type["OUT_OF_ORDER"]["count"] == 2
    assert "DUPLICATE" not in by_type


def test_apply_plan_writes_specific_events(db):
    _spin(db, "g1", 7)      # will be corrected: remote says 13
    _spin(db, "g2", 2)
    plan = RepairPlan(
        missing=[HistoryRecord(game_id="g3", number=21)],
        corrections=[("g1", 7, 13)],
        window_achieved=10, authoritative=True, repairable=True,
    )
    summary = Repairer(db).apply_plan(plan)
    assert summary["backfilled"] == 1
    assert summary["corrected"] == 1

    rows = db.execute(
        "SELECT incident_type, status, attempts, affected_count, "
        "start_game_id, end_game_id, resolution, details "
        "FROM repair_events ORDER BY incident_type"
    ).fetchall()
    types = {r["incident_type"]: r for r in rows}
    assert set(types) == {"MISSING_SPIN", "WRONG_VALUE"}
    for r in rows:
        assert r["status"] == "RESOLVED"
        assert r["attempts"] == 1
        assert r["resolution"] == "REPAIRED"
        assert r["affected_count"] >= 1
    assert types["MISSING_SPIN"]["start_game_id"] == "g3"
    assert types["WRONG_VALUE"]["start_game_id"] == "g1"
    # audit trail: old->new recorded in details
    det = json.loads(types["WRONG_VALUE"]["details"])
    assert ["g1", 7, 13] in det["corrections"]


def test_apply_plan_retry_increments_attempts(db):
    """§23 attempts: a failed repair stays in the queue; the next successful
    pass increments attempts on the SAME incident (retry semantics)."""
    _spin(db, "g1", 7)
    plan = RepairPlan(
        corrections=[("g1", 7, 13)],
        window_achieved=10, authoritative=True, repairable=True,
    )
    # first attempt FAILS verification -> FAILED event, attempts=1
    def _bad_verify(conn):
        raise RuntimeError("verify failed")

    with pytest.raises(RuntimeError):
        Repairer(db).apply_plan(plan, verify_fn=_bad_verify)
    ev = db.execute(
        "SELECT status, attempts FROM repair_events WHERE incident_type='WRONG_VALUE'"
    ).fetchone()
    assert ev["status"] == "FAILED" and ev["attempts"] == 1
    # canonical unchanged (rolled back)
    assert db.execute(
        "SELECT number FROM roulette_spins WHERE game_id='g1'").fetchone()["number"] == 7

    # second attempt succeeds -> SAME event, attempts=2, RESOLVED
    Repairer(db).apply_plan(plan)
    ev = db.execute(
        "SELECT status, attempts, resolved_at FROM repair_events "
        "WHERE incident_type='WRONG_VALUE'"
    ).fetchone()
    assert ev["status"] == "RESOLVED" and ev["attempts"] == 2
    assert ev["resolved_at"] is not None


# ---------------------------------------------------------------------------
# §24 — auto-repair rules preserve raw observations
# ---------------------------------------------------------------------------
def test_wrong_value_preserves_raw_observation(db):
    """Remote says 13, local captured 17 (same game_id): canonical becomes
    13 (REPAIRED), the raw 17 observation is preserved immutably."""
    sid = observer.start_session(db)
    _spin(db, "GAME123", 17)
    _observe(db, sid, "websocket", 17, game_id="GAME123", server_ts="t1")
    plan = RepairPlan(
        corrections=[("GAME123", 17, 13)],
        window_achieved=5, authoritative=True, repairable=True,
    )
    Repairer(db).apply_plan(plan)
    row = db.execute(
        "SELECT number, status FROM roulette_spins WHERE game_id='GAME123'"
    ).fetchone()
    assert row["number"] == 13 and row["status"] == "REPAIRED"
    obs = db.execute(
        "SELECT number FROM spin_observations WHERE source='websocket'"
    ).fetchall()
    assert [o["number"] for o in obs] == [17]   # raw evidence untouched


def test_duplicate_collapse_retains_observations(tmp_path):
    """§24 duplicate: two canonical rows (legacy table WITHOUT unique
    game_id) collapse to one; BOTH raw observations are retained (never
    destroyed)."""
    conn = sqlite3.connect(tmp_path / "dup2.db")
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    # legacy malformed table without UNIQUE game_id
    conn.execute("DROP TABLE roulette_spins")
    conn.execute(
        """CREATE TABLE roulette_spins (
            id INTEGER PRIMARY KEY AUTOINCREMENT, number INTEGER NOT NULL,
            description TEXT NOT NULL, color TEXT NOT NULL,
            game_id TEXT NOT NULL, server_ts TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
    )
    schema.ensure_schema(conn)
    sid = observer.start_session(conn)
    _spin(conn, "GAME123", 17)
    _spin(conn, "GAME123", 17)   # duplicate canonical row (legacy table)
    _observe(conn, sid, "websocket", 17, game_id="GAME123", server_ts="t1")
    _observe(conn, sid, "websocket", 17, game_id="GAME123", server_ts="t2")
    plan = RepairPlan(
        duplicates=["GAME123"], duplicate_kinds={"GAME123": "EXACT"},
        window_achieved=5, authoritative=True, repairable=True,
    )
    summary = Repairer(conn).apply_plan(plan)
    assert summary["collapsed"] == 1
    n_canonical = conn.execute(
        "SELECT COUNT(*) FROM roulette_spins WHERE game_id='GAME123'"
    ).fetchone()[0]
    n_obs = conn.execute(
        "SELECT COUNT(*) FROM spin_observations WHERE source='websocket'"
    ).fetchone()[0]
    assert n_canonical == 1
    assert n_obs == 2   # both observations retained
    conn.close()


def test_missing_inserts_authoritative(db):
    """§24 missing: remote has g99:17, local doesn't -> insert (REPAIRED),
    no observation deleted."""
    sid = observer.start_session(db)
    _observe(db, sid, "history", 17, game_id="g99", server_ts="t1")
    plan = RepairPlan(
        missing=[HistoryRecord(game_id="g99", number=17, server_ts="t1")],
        missing_seq={"g99": 1},
        window_achieved=5, authoritative=True, repairable=True,
    )
    summary = Repairer(db).apply_plan(plan)
    assert summary["backfilled"] == 1
    row = db.execute(
        "SELECT number, status, sequence_no FROM roulette_spins WHERE game_id='g99'"
    ).fetchone()
    assert row["number"] == 17 and row["status"] == "REPAIRED"
    assert row["sequence_no"] == 1
