"""Canonical-ordering reconstruction (reconstruct_ordering).

Rebuilds sequence_no 1..N from chronological evidence (server_ts ->
captured_at -> id) when the canonical ordering is broken: collisions
(duplicate sequence_no), NULLs, order violations (sequence contradicting
timestamps), and gaps (compressed to 1..N — the reconciler's game_id-based
insertion-shift detection re-opens genuine misses from history).

Pinned semantics: deterministic, idempotent (consistent dataset -> no
change, no event), audited (RECONSTRUCT_ORDER repair event when changed),
and never touches observations.
"""

import json
import os
import sqlite3
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["RC_DB_PATH"] = os.path.join(_tmp, "d.db")
os.environ["RC_STATE_FILE"] = os.path.join(_tmp, "s.json")
os.environ["RC_CSV_FILE"] = os.path.join(_tmp, "s.csv")
os.environ.setdefault("SUNBET_USER", "test")
os.environ.setdefault("SUNBET_PASS", "test")

import pytest  # noqa: E402

from collector import observer, schema  # noqa: E402
from collector.repairer import Repairer  # noqa: E402
from scripts.standalone_reconcile import run_once  # noqa: E402

REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def _color(n):
    return "Green" if n == 0 else ("Red" if n in REDS else "Black")


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "recon.db")
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    yield conn
    conn.close()


def _insert(conn, gid, num, ts, seq):
    """Insert one canonical row with explicit sequence_no."""
    conn.execute(
        "INSERT INTO roulette_spins (number, description, color, game_id, "
        "server_ts, captured_at, sequence_no) VALUES (?,?,?,?,?,?,?)",
        (num, f"{num} {_color(num)}", _color(num), gid, ts, ts, seq),
    )
    conn.commit()


def _seqs(conn):
    return [r["game_id"] for r in conn.execute(
        "SELECT game_id, sequence_no FROM roulette_spins "
        "ORDER BY sequence_no IS NULL, sequence_no, id").fetchall()]


# ---------------------------------------------------------------------------
# reconstruct_ordering — Repairer
# ---------------------------------------------------------------------------
def test_idempotent_consistent(db):
    """A consistent 1..N dataset matching the ts evidence changes nothing."""
    for i, gid in enumerate(["A", "B", "C", "D", "E"], start=1):
        _insert(db, gid, i, f"2026-08-15T00:00:0{i}Z", i)
    r = Repairer(db).reconstruct_ordering()
    assert r == {"checked": 5, "reordered": 0, "gaps_found": 0,
                 "collisions_found": 0}
    assert _seqs(db) == ["A", "B", "C", "D", "E"]
    # no audit event when nothing changed
    assert db.execute("SELECT COUNT(*) FROM repair_events").fetchone()[0] == 0


def test_collision_fixed(db):
    """Duplicate sequence_no -> rebuilt to distinct 1..N, audited."""
    _insert(db, "A", 1, "2026-08-15T00:00:01Z", 1)
    _insert(db, "B", 2, "2026-08-15T00:00:02Z", 2)
    _insert(db, "C", 3, "2026-08-15T00:00:03Z", 3)
    _insert(db, "D", 4, "2026-08-15T00:00:04Z", 3)   # collision with C
    _insert(db, "E", 5, "2026-08-15T00:00:05Z", 4)
    r = Repairer(db).reconstruct_ordering()
    assert r["collisions_found"] == 1
    assert r["reordered"] == 2          # D 3->4, E 4->5
    assert _seqs(db) == ["A", "B", "C", "D", "E"]
    ev = db.execute(
        "SELECT incident_type, status, affected_count, resolution, details "
        "FROM repair_events WHERE incident_type='RECONSTRUCT_ORDER'"
    ).fetchone()
    assert ev is not None
    assert ev["status"] == "RESOLVED" and ev["affected_count"] == 2
    assert json.loads(ev["details"])["collisions_found"] == 1


def test_order_violation_fixed_by_ts(db):
    """Sequence contradicts the timestamps (D/E swapped by server_ts) ->
    renumbered to the chronological evidence order."""
    _insert(db, "A", 1, "2026-08-15T00:00:01Z", 1)
    _insert(db, "B", 2, "2026-08-15T00:00:02Z", 2)
    _insert(db, "C", 3, "2026-08-15T00:00:03Z", 3)
    _insert(db, "D", 4, "2026-08-15T00:00:06Z", 4)   # D's ts is NEWER
    _insert(db, "E", 5, "2026-08-15T00:00:05Z", 5)   # E's ts is OLDER
    r = Repairer(db).reconstruct_ordering()
    assert r["reordered"] == 2
    # ts order A,B,C,E,D -> E=4, D=5
    assert _seqs(db) == ["A", "B", "C", "E", "D"]


def test_gap_reported_and_closed(db):
    """1,2,3,5,6 (hole at 4) -> compressed to 1..5, gap reported."""
    for seq, gid in [(1, "A"), (2, "B"), (3, "C"), (5, "D"), (6, "E")]:
        _insert(db, gid, seq, f"2026-08-15T00:00:0{seq}Z", seq)
    r = Repairer(db).reconstruct_ordering()
    assert r["gaps_found"] == 1
    assert r["reordered"] == 2          # D 5->4, E 6->5
    assert _seqs(db) == ["A", "B", "C", "D", "E"]


def test_null_sequence_assigned(db):
    """NULL sequence_no rows get assigned in chronological order."""
    _insert(db, "A", 1, "2026-08-15T00:00:01Z", 1)
    _insert(db, "B", 2, "2026-08-15T00:00:02Z", None)   # NULL
    _insert(db, "C", 3, "2026-08-15T00:00:03Z", None)   # NULL
    r = Repairer(db).reconstruct_ordering()
    assert r["reordered"] == 2
    assert _seqs(db) == ["A", "B", "C"]


def test_never_touches_observations(db):
    """Reconstruction only touches canonical sequence_no — observations are
    immutable evidence and never modified."""
    sid = observer.start_session(db)
    observer.record_observation(db, source="websocket", session_id=sid,
                                game_id="A", number=1, server_ts="t1")
    observer.record_observation(db, source="history", session_id=sid,
                                game_id="B", number=2, server_ts="t2")
    _insert(db, "A", 1, "2026-08-15T00:00:01Z", 1)
    _insert(db, "B", 2, "2026-08-15T00:00:02Z", 2)
    _insert(db, "C", 3, "2026-08-15T00:00:03Z", 3)   # collision with B
    Repairer(db).reconstruct_ordering()
    obs = db.execute(
        "SELECT game_id, number FROM spin_observations ORDER BY id").fetchall()
    assert [(o["game_id"], o["number"]) for o in obs] == [("A", 1), ("B", 2)]


# ---------------------------------------------------------------------------
# run_once integration — reconstruction runs before the reconcile pass
# ---------------------------------------------------------------------------
def _seed_history(conn, spins):
    sid = observer.start_session(conn, source="standalone-test")
    for gid, number, ts in spins:
        observer.record_observation(
            conn, source="history", session_id=sid, game_id=gid,
            number=number, server_ts=ts, description=f"{number} {_color(number)}",
            raw_payload=f'{{"gameId":"{gid}"}}',
        )


def _insert_canonical(conn, spins):
    for i, (gid, number, ts) in enumerate(spins, start=1):
        conn.execute(
            "INSERT INTO roulette_spins (number, description, color, game_id, "
            "server_ts, captured_at, sequence_no, dedup_key) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (number, f"{number} {_color(number)}", _color(number), gid, ts, ts,
             i, f"gid:{gid}"),
        )
    conn.commit()


def test_run_once_reconstructs_broken_ordering(tmp_path, monkeypatch):
    """A broken canonical ordering (collision) is reconstructed BEFORE the
    reconcile pass; the pass logs the sweep in the RECONCILIATION details."""
    db_path = tmp_path / "broken.db"
    monkeypatch.setenv("RC_DB_PATH", str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    try:
        spins = [(f"g{i}", i, f"2026-08-15T00:00:0{i}Z") for i in range(1, 6)]
        _seed_history(conn, spins)
        _insert_canonical(conn, spins)
        # introduce a collision: g3 and g4 both at sequence 3
        conn.execute(
            "UPDATE roulette_spins SET sequence_no=3 WHERE game_id='g4'")
        conn.commit()

        details = run_once(conn)

        # reconstruction fixed the ordering (details carry the sweep)
        assert details.get("reconstruct", {}).get("reordered", 0) >= 1
        seqs = [r["game_id"] for r in conn.execute(
            "SELECT game_id FROM roulette_spins ORDER BY sequence_no").fetchall()]
        assert seqs == ["g1", "g2", "g3", "g4", "g5"]
        # audited
        assert conn.execute(
            "SELECT COUNT(*) FROM repair_events "
            "WHERE incident_type='RECONSTRUCT_ORDER'").fetchone()[0] == 1
        # the reconcile pass still verified clean after the sweep
        assert details["ok"] is True
        assert details["message"] == "verified"
    finally:
        conn.close()
