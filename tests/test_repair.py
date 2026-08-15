"""Phase 4 — repairer: deterministic atomic repairs + audit trail.

PRD §24/§25/§33/§34/§35: safe-to-auto-repair rules, atomicity (no partial
repairs), immutable raw evidence, full audit (old/new/why/source/result).
"""

import sqlite3

import pytest

from collector import observer, schema
from collector.repairer import Repairer
from collector.reconciler import HistoryRecord, RepairPlan


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    yield conn
    conn.close()


def _spin(conn, game_id, number, ts="2026-08-15T00:00:00Z", status="VALID"):
    color = "Green" if number == 0 else ("Red" if number in {
        1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36} else "Black")
    conn.execute(
        "INSERT INTO roulette_spins (number, description, color, game_id, "
        "server_ts, captured_at, status) VALUES (?,?,?,?,?,?,?)",
        (number, f"{number} {color}", color, game_id, ts, ts, status),
    )
    conn.commit()


def _count(conn, game_id):
    return conn.execute("SELECT COUNT(*) FROM roulette_spins WHERE game_id=?",
                        (game_id,)).fetchone()[0]


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------
def test_backfill_missing(db):
    r = Repairer(db)
    rid = r.backfill_missing("g99", 17, "2026-08-15T00:05:00Z")
    row = db.execute("SELECT * FROM roulette_spins WHERE id=?", (rid,)).fetchone()
    assert row["game_id"] == "g99"
    assert row["number"] == 17
    assert row["status"] == "REPAIRED"
    assert row["confidence"] == 1.0
    assert row["source"] == "backfilled"


def test_backfill_requires_game_id(db):
    with pytest.raises(ValueError):
        Repairer(db).backfill_missing(None, 17, None)


# ---------------------------------------------------------------------------
# Correct — wrong value with authoritative identity, raw evidence preserved
# ---------------------------------------------------------------------------
def test_correct_value(db):
    _spin(db, "g1", 17)
    r = Repairer(db)
    changed = r.correct_value("g1", 23, evidence="remote-history")
    assert changed
    row = db.execute("SELECT * FROM roulette_spins WHERE game_id='g1'").fetchone()
    assert row["number"] == 23
    assert row["status"] == "REPAIRED"
    assert row["confidence"] == 1.0


def test_correct_unchanged_returns_false(db):
    _spin(db, "g1", 17)
    assert not Repairer(db).correct_value("g1", 17)


def test_correct_missing_row_returns_false(db):
    assert not Repairer(db).correct_value("nope", 17)


# ---------------------------------------------------------------------------
# Collapse duplicates — canonical collapsed, observations retained
# ---------------------------------------------------------------------------
def test_collapse_duplicate(tmp_path):
    """PRD §16: conflicting duplicate (same game_id, different number) is a
    critical incident. Canonical UNIQUE normally prevents this, so build the
    malformed case (legacy DB without the constraint) and collapse it."""
    conn = sqlite3.connect(tmp_path / "dup.db")
    conn.row_factory = sqlite3.Row
    # legacy table WITHOUT unique game_id — the malformed-data scenario
    conn.execute(
        """CREATE TABLE roulette_spins (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            number      INTEGER NOT NULL,
            description TEXT NOT NULL,
            color       TEXT NOT NULL,
            game_id     TEXT NOT NULL,
            server_ts   TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    schema.ensure_schema(conn)   # adds integrity tables + canonical columns
    conn.execute("INSERT INTO roulette_spins (number, description, color, game_id, server_ts, captured_at) "
                 "VALUES (17,'17 Black','Black','g1','t','t')")
    conn.execute("INSERT INTO roulette_spins (number, description, color, game_id, server_ts, captured_at) "
                 "VALUES (23,'23 Red','Red','g1','t','t')")
    conn.commit()
    r = Repairer(conn)
    removed = r.collapse_duplicate("g1", keep_number=23)
    assert removed == 1
    assert _count(conn, "g1") == 1
    row = conn.execute("SELECT * FROM roulette_spins WHERE game_id='g1'").fetchone()
    assert row["number"] == 23   # kept the authoritative value
    conn.close()


# ---------------------------------------------------------------------------
# Reorder
# ---------------------------------------------------------------------------
def test_reorder_window(db):
    for i, gid in enumerate(["a", "b", "c"]):
        _spin(db, gid, i)
    n = Repairer(db).reorder_window(["c", "b", "a"])
    assert n == 3
    rows = db.execute("SELECT game_id, sequence_no FROM roulette_spins "
                      "ORDER BY sequence_no").fetchall()
    assert [r["game_id"] for r in rows] == ["c", "b", "a"]


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
def test_record_repair_audit(db):
    ev = Repairer(db).record_repair(
        "MISSING_SPIN", start_game_id="g1", end_game_id="g3",
        affected_count=3, resolution="BACKFILLED",
        details={"old": None, "new": [1, 2, 3], "source": "remote-history"},
    )
    row = db.execute("SELECT * FROM repair_events WHERE id=?", (ev,)).fetchone()
    assert row["incident_type"] == "MISSING_SPIN"
    assert row["status"] == "RESOLVED"
    assert row["affected_count"] == 3
    assert '"source": "remote-history"' in row["details"]


# ---------------------------------------------------------------------------
# apply_plan — the atomic reconcile->repair->verify loop
# ---------------------------------------------------------------------------
def test_apply_plan_backfills_and_corrects(db):
    _spin(db, "a", 1)
    _spin(db, "b", 2)
    plan = RepairPlan(
        missing=[HistoryRecord(game_id="c", number=3)],
        corrections=[("b", 2, 9)],     # remote says b=9
        window_achieved=3, authoritative=True,
    )
    r = Repairer(db)
    summary = r.apply_plan(plan)
    assert summary["backfilled"] == 1
    assert summary["corrected"] == 1
    assert _count(db, "c") == 1
    assert db.execute("SELECT number FROM roulette_spins WHERE game_id='b'"
                      ).fetchone()[0] == 9
    # audit row written
    assert db.execute("SELECT COUNT(*) FROM repair_events").fetchone()[0] == 1


def test_apply_plan_refuses_without_authority(db):
    plan = RepairPlan(missing=[HistoryRecord(game_id="c", number=3)],
                      authoritative=False)
    with pytest.raises(ValueError):
        Repairer(db).apply_plan(plan)
    assert _count(db, "c") == 0   # nothing applied


def test_apply_plan_rolls_back_on_verify_failure(db):
    """PRD §33: if verify fails, the ENTIRE repair rolls back — no partial
    repair of a sequence."""
    _spin(db, "a", 1)
    plan = RepairPlan(
        missing=[HistoryRecord(game_id="b", number=2)],
        corrections=[("a", 1, 7)],
        window_achieved=2, authoritative=True,
    )
    r = Repairer(db)

    def _verify_fails(conn):
        raise RuntimeError("verification failed")

    with pytest.raises(RuntimeError):
        r.apply_plan(plan, verify_fn=_verify_fails)
    # rolled back: no backfill, no correction
    assert _count(db, "b") == 0
    assert db.execute("SELECT number FROM roulette_spins WHERE game_id='a'"
                      ).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM repair_events").fetchone()[0] == 0


def test_apply_plan_verifies_ok(db):
    _spin(db, "a", 1)
    plan = RepairPlan(
        missing=[HistoryRecord(game_id="b", number=2)],
        window_achieved=2, authoritative=True,
    )
    r = Repairer(db)
    seen = {}

    def _verify(conn):
        seen["b_present"] = _count(conn, "b") == 1

    r.apply_plan(plan, verify_fn=_verify)
    assert seen["b_present"] is True
    assert _count(db, "b") == 1
