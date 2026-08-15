"""PRD §25 — NEVER auto-repair.

The identity gate existed but refused repairs raised a bare ValueError and
NEVER marked the affected rows UNVERIFIED or recorded the refusal reason —
the "-> UNVERIFIED, surfaced" half was missing. And "multiple conflicting
sources" was not enforced as a gate at all (source agreement only logged).

These tests pin the corrected contract:
  * the four §25 conditions are explicit (RepairRefused + NEVER_AUTO_REPAIR)
  * a refused plan marks its affected canonical rows UNVERIFIED
  * a REPAIR_REFUSED repair-event entry records the reason
  * conflicting sources (WS vs DOM) refuse repairs (CONFLICTING_SOURCES)
  * a conflicting-sources gate is bypassable (check_conflict=False) — tests
    and controlled environments opt out without weakening the pipeline
"""

import json
import os
import sqlite3

import pytest

from collector import observer, schema
from collector.repairer import (
    NEVER_AUTO_REPAIR,
    RepairRefused,
    Repairer,
    refuse_repair,
)
from collector.reconciler import HistoryRecord, RepairPlan

REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "refuse.db")
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    yield conn
    conn.close()


def _spin(conn, gid, num, ts="2026-08-15T00:00:00Z", status="VALID"):
    color = "Green" if num == 0 else ("Red" if num in REDS else "Black")
    conn.execute(
        "INSERT INTO roulette_spins (number, description, color, game_id, "
        "server_ts, captured_at, status) VALUES (?,?,?,?,?,?,?)",
        (num, f"{num} {color}", color, gid, ts, ts, status),
    )
    conn.commit()


def _observe(conn, sid, source, number, game_id=None, server_ts="t"):
    observer.record_observation(
        conn, source=source, session_id=sid, game_id=game_id,
        number=number, description=f"{number} X", server_ts=server_ts,
    )


def _status(conn, gid):
    return conn.execute(
        "SELECT status FROM roulette_spins WHERE game_id=?", (gid,)
    ).fetchone()["status"]


# ---------------------------------------------------------------------------
# The four §25 conditions are explicit + enforced
# ---------------------------------------------------------------------------
def test_no_authority_refuses_and_marks_unverified(db):
    _spin(db, "g1", 7)
    plan = RepairPlan(
        corrections=[("g1", 7, 13)],        # g1 exists -> will be marked
        missing=[HistoryRecord(game_id="g2", number=9)],
        authoritative=False, repairable=False,
    )
    with pytest.raises(RepairRefused) as ei:
        Repairer(db).apply_plan(plan)
    assert ei.value.reason == NEVER_AUTO_REPAIR["NO_AUTHORITY"]
    # the affected canonical rows are marked UNVERIFIED
    assert _status(db, "g1") == "UNVERIFIED"
    # the refusal is in the repair queue with the reason
    ev = db.execute(
        "SELECT incident_type, status, resolution, details FROM repair_events"
    ).fetchone()
    assert ev["incident_type"] == "REPAIR_REFUSED"
    assert ev["status"] == "UNVERIFIED"
    assert ev["resolution"] == NEVER_AUTO_REPAIR["NO_AUTHORITY"]
    det = json.loads(ev["details"])
    assert det["reason_key"] == "NO_AUTHORITY"


def test_no_identity_refuses_and_marks_unverified(db):
    """Number-only remote (DOM text) can DETECT but never repair — the
    identity is never manufactured (PRD §5)."""
    _spin(db, "g1", 7)
    plan = RepairPlan(
        missing=[HistoryRecord(game_id=None, number=9)],   # no game_id
        corrections=[("g1", 7, 13)],                       # no identity either
        authoritative=True, repairable=False,
    )
    with pytest.raises(RepairRefused) as ei:
        Repairer(db).apply_plan(plan)
    assert ei.value.reason == NEVER_AUTO_REPAIR["NO_IDENTITY"]
    assert _status(db, "g1") == "UNVERIFIED"
    ev = db.execute(
        "SELECT resolution FROM repair_events WHERE incident_type='REPAIR_REFUSED'"
    ).fetchone()
    assert ev["resolution"] == NEVER_AUTO_REPAIR["NO_IDENTITY"]


def test_conflicting_sources_refuse(db):
    """§25 'multiple conflicting sources': a recent WS-vs-DOM disagreement
    refuses the repair — surfaced, never silently applied."""
    sid = observer.start_session(db)
    _observe(db, sid, "websocket", 23, game_id="g1", server_ts="t1")
    _observe(db, sid, "dom", 18, server_ts="t1")     # disagrees (23 vs 18)
    _spin(db, "g1", 23)
    plan = RepairPlan(
        corrections=[("g1", 23, 23)],
        window_achieved=5, authoritative=True, repairable=True,
    )
    with pytest.raises(RepairRefused) as ei:
        Repairer(db).apply_plan(plan)
    assert ei.value.reason == NEVER_AUTO_REPAIR["CONFLICTING_SOURCES"]
    assert _status(db, "g1") == "UNVERIFIED"
    ev = db.execute(
        "SELECT resolution FROM repair_events WHERE incident_type='REPAIR_REFUSED'"
    ).fetchone()
    assert ev["resolution"] == NEVER_AUTO_REPAIR["CONFLICTING_SOURCES"]


def test_conflicting_sources_gate_bypassable(db):
    """check_conflict=False opts out of the WS-vs-DOM gate (tests / controlled
    environments) without weakening the pipeline default."""
    sid = observer.start_session(db)
    _observe(db, sid, "websocket", 23, game_id="g1", server_ts="t1")
    _observe(db, sid, "dom", 18, server_ts="t1")
    _spin(db, "g1", 23)
    plan = RepairPlan(
        corrections=[("g1", 23, 23)],
        window_achieved=5, authoritative=True, repairable=True,
    )
    summary = Repairer(db).apply_plan(plan, check_conflict=False)
    assert summary["corrected"] == 0   # value already 23 — nothing to fix


def test_no_conflict_repairs_normally(db):
    """No disagreement -> the gate passes and the repair applies."""
    sid = observer.start_session(db)
    _observe(db, sid, "websocket", 13, game_id="g1", server_ts="t1")
    _observe(db, sid, "dom", 13, server_ts="t1")      # agrees
    _spin(db, "g1", 17)
    plan = RepairPlan(
        corrections=[("g1", 17, 13)],
        window_achieved=5, authoritative=True, repairable=True,
    )
    summary = Repairer(db).apply_plan(plan)
    assert summary["corrected"] == 1
    assert _status(db, "g1") == "REPAIRED"


def test_stale_unrelated_conflict_does_not_block(db):
    """A CONFLICT involving a DIFFERENT game_id (g9) must not refuse a repair
    of g1 — the §25 gate is scoped to the plan's own game_ids."""
    sid = observer.start_session(db)
    # g9 disagrees (stale, unrelated)
    _observe(db, sid, "websocket", 23, game_id="g9", server_ts="t1")
    _observe(db, sid, "dom", 18, server_ts="t1")
    # g1 agrees
    _observe(db, sid, "websocket", 13, game_id="g1", server_ts="t1")
    _observe(db, sid, "dom", 13, server_ts="t1")
    _spin(db, "g1", 17)
    plan = RepairPlan(
        corrections=[("g1", 17, 13)],
        window_achieved=5, authoritative=True, repairable=True,
    )
    summary = Repairer(db).apply_plan(plan)
    assert summary["corrected"] == 1
    assert _status(db, "g1") == "REPAIRED"


# ---------------------------------------------------------------------------
# refuse_repair helper
# ---------------------------------------------------------------------------
def test_refuse_repair_marks_and_audits(db):
    _spin(db, "a", 1)
    _spin(db, "b", 2)
    plan = RepairPlan(
        corrections=[("a", 1, 9)],
        duplicates=["b"], duplicate_kinds={"b": "EXACT"},
        window_achieved=2, authoritative=True, repairable=True,
    )
    refuse_repair(db, plan, "STATISTICAL_INFERENCE")
    assert _status(db, "a") == "UNVERIFIED"
    assert _status(db, "b") == "UNVERIFIED"
    ev = db.execute(
        "SELECT incident_type, status, affected_count, start_game_id, "
        "end_game_id, resolution FROM repair_events"
    ).fetchone()
    assert ev["incident_type"] == "REPAIR_REFUSED"
    assert ev["status"] == "UNVERIFIED"
    assert ev["affected_count"] == 2
    assert ev["start_game_id"] == "a" and ev["end_game_id"] == "b"
    assert ev["resolution"] == NEVER_AUTO_REPAIR["STATISTICAL_INFERENCE"]


def test_refuse_repair_never_raises_empty_plan(db):
    """An empty plan (no affected game_ids) refuses gracefully — nothing to
    mark, the event still records the reason."""
    plan = RepairPlan(authoritative=True, repairable=True)
    refuse_repair(db, plan, "NO_AUTHORITY")   # must not raise
    ev = db.execute(
        "SELECT resolution FROM repair_events WHERE incident_type='REPAIR_REFUSED'"
    ).fetchone()
    assert ev["resolution"] == NEVER_AUTO_REPAIR["NO_AUTHORITY"]


def test_never_auto_repair_table_complete():
    """All four §25 conditions are present and distinct."""
    assert set(NEVER_AUTO_REPAIR) == {
        "NO_AUTHORITY", "NO_IDENTITY", "CONFLICTING_SOURCES",
        "STATISTICAL_INFERENCE",
    }
    assert len(set(NEVER_AUTO_REPAIR.values())) == 4
