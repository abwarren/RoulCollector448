"""Ordering (OUT_OF_ORDER) — same-set reorder detection & repair.

The gap: a reorder/swap (same game_id set, different order) was invisible
when it happened at the NEWEST position — the walk broke at i=0 with no
match, `repairable` stayed False, and the reorder was never detected. And
when it DID fire (anchored swap), it renumbered the WHOLE divergent region
(including correctly-placed records) with wrong absolute positions.

These tests pin the corrected semantics:
  * repairable = remote carries game_id identity (a swap is precisely when
    indices DON'T match), not "an index matched"
  * only the MINIMAL out-of-order suffix is renumbered (longest common
    prefix in oldest-first order is untouched)
  * absolute positions come from the remote index (len(remote) - rpos) —
    correct for newest AND middle swaps
"""

import sqlite3

import pytest

from collector import schema
from collector.reconciler import HistoryRecord, RepairPlan, compare_windows, reconcile
from collector.repairer import Repairer
from collector.history import StaticHistoryProvider


def canon(gid, num, ts):
    return {"game_id": gid, "number": num, "server_ts": ts}


def rem(gid, num, ts):
    return HistoryRecord(game_id=gid, number=num, server_ts=ts)


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "order.db")
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    yield conn
    conn.close()


def _insert(conn, gid, num, ts, seq):
    color = "Green" if num == 0 else ("Red" if num in {
        1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36} else "Black")
    conn.execute(
        "INSERT INTO roulette_spins (number, description, color, game_id, "
        "server_ts, captured_at, sequence_no) VALUES (?,?,?,?,?,?,?)",
        (num, f"{num} {color}", color, gid, ts, ts, seq),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def test_newest_swap_detected():
    """E,D vs D,E at the newest position — the walk breaks at i=0 but the
    same-set reorder must STILL be detected (set equality proves identity)."""
    local = [canon("E", 5, "t4"), canon("D", 4, "t3"),
             canon("B", 2, "t2"), canon("A", 1, "t1")]
    remote = [rem("D", 4, "t4"), rem("E", 5, "t3"),
              rem("B", 2, "t2"), rem("A", 1, "t1")]
    plan = compare_windows(local, remote)
    assert plan.repairable
    assert plan.reorder == ["E", "D"]
    assert plan.renumber == [("E", 3), ("D", 4)]
    assert plan.reorder_start == 3


def test_middle_swap_minimal_suffix():
    """D,B,C,A vs D,C,B,A (newest-first): A and D are correctly placed; only
    the minimal B,C suffix is renumbered, at their TRUE positions 2,3."""
    local = [canon("D", 4, "t4"), canon("B", 3, "t3"),
             canon("C", 2, "t2"), canon("A", 1, "t1")]
    remote = [rem("D", 4, "t4"), rem("C", 3, "t3"),
              rem("B", 2, "t2"), rem("A", 1, "t1")]
    plan = compare_windows(local, remote)
    assert plan.reorder == ["B", "C"]
    assert plan.renumber == [("B", 2), ("C", 3)]
    assert plan.reorder_start == 2


def test_identical_order_no_reorder():
    """Same order -> no reorder flagged (longest common prefix = everything)."""
    local = [canon("D", 4, "t4"), canon("C", 3, "t3"),
             canon("B", 2, "t2"), canon("A", 1, "t1")]
    remote = [rem("D", 4, "t4"), rem("C", 3, "t3"),
              rem("B", 2, "t2"), rem("A", 1, "t1")]
    plan = compare_windows(local, remote)
    assert plan.reorder == []
    assert plan.renumber == []


# ---------------------------------------------------------------------------
# Repair — end to end through the repairer
# ---------------------------------------------------------------------------
def test_reorder_repair_end_to_end(db):
    """Local A B D E (sequence 1-4), remote A B E D: the repair renumbers
    E->3, D->4, leaving A=1,B=2 untouched — the affected suffix only."""
    _insert(db, "A", 1, "t1", 1)
    _insert(db, "B", 2, "t2", 2)
    _insert(db, "D", 4, "t3", 3)
    _insert(db, "E", 5, "t4", 4)

    # authoritative remote — NEWEST-first (the provider contract)
    remote = [rem("D", 4, "t4"), rem("E", 5, "t3"),
              rem("B", 2, "t2"), rem("A", 1, "t1")]
    result = reconcile(
        [{"game_id": r, "number": n, "server_ts": t} for r, n, t in
         [("A", 1, "t1"), ("B", 2, "t2"), ("D", 4, "t3"), ("E", 5, "t4")]],
        StaticHistoryProvider(remote), window=10,
    )
    assert not result.ok
    assert result.reorder_count == 2

    summary = Repairer(db).apply_plan(result.plan)
    assert summary["reordered"] == 2

    rows = db.execute(
        "SELECT game_id, sequence_no FROM roulette_spins ORDER BY sequence_no"
    ).fetchall()
    assert [(r["game_id"], r["sequence_no"]) for r in rows] == \
        [("A", 1), ("B", 2), ("E", 3), ("D", 4)]

    # OUT_OF_ORDER incident recorded in the repair queue
    ev = db.execute(
        "SELECT incident_type, status, affected_count, start_game_id, "
        "end_game_id FROM repair_events WHERE incident_type='OUT_OF_ORDER'"
    ).fetchone()
    assert ev is not None
    assert ev["status"] == "RESOLVED"
    assert ev["affected_count"] == 2
    assert ev["start_game_id"] == "E"
    assert ev["end_game_id"] == "D"
