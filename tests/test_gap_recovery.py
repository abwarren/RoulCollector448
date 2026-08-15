"""§26-new gap lifecycle — the dashboard distinguishes a REPAIRED gap from an
UNVERIFIED (permanent) one.

The new semantics: a detected gap is no longer just displayed. It:
  1. is recorded OPEN (GAP repair event)
  2. attempts recovery: query the authoritative rolling history, repair
     (backfill) if possible — identity-gated (§24/§25 respected)
  3. re-runs validation
  4. resolves RESOLVED/REPAIRED (repaired gap) or UNVERIFIED (permanent)

recover_gaps() is the loop; record_gap()/resolve_gap() are its lifecycle
primitives. The dashboard renders gap_events from /api/integrity.
"""

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
from scripts.standalone_reconcile import recover_gaps  # noqa: E402

REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def _color(n):
    return "Green" if n == 0 else ("Red" if n in REDS else "Black")


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "gap.db")
    c.row_factory = sqlite3.Row
    schema.ensure_schema(c)
    yield c
    c.close()


def _spin(c, gid, num, seq, status="VALID"):
    c.execute(
        "INSERT INTO roulette_spins (number, description, color, game_id, "
        "server_ts, captured_at, sequence_no, status) VALUES (?,?,?,?,?,?,?,?)",
        (num, f"{num} {_color(num)}", _color(num), gid, f"t{seq}", f"t{seq}",
         seq, status),
    )
    c.commit()


def _seed_history(c, spins):
    """Authoritative history observations (source='history') for recovery."""
    sid = observer.start_session(c, source="gap-test")
    for gid, number, ts in spins:
        observer.record_observation(
            c, source="history", session_id=sid, game_id=gid,
            number=number, server_ts=ts,
            description=f"{number} {_color(number)}",
            raw_payload=f'{{"gameId":"{gid}"}}',
        )


# ---------------------------------------------------------------------------
# lifecycle primitives
# ---------------------------------------------------------------------------
def test_record_gap_open(conn):
    _spin(conn, "a", 1, 1)
    _spin(conn, "b", 2, 3)   # sequence 2 missing -> a 1-gap
    ev = Repairer(conn).record_gap(start_seq=2, end_seq=2, size=1)
    assert ev > 0
    row = conn.execute(
        "SELECT incident_type, start_game_id, end_game_id, affected_count, "
        "status, resolution FROM repair_events WHERE id=?", (ev,)
    ).fetchone()
    assert row["incident_type"] == "GAP"
    assert row["start_game_id"] == "seq:2" and row["end_game_id"] == "seq:2"
    assert row["affected_count"] == 1
    assert row["status"] == "OPEN"
    assert row["resolution"] is None


def test_resolve_gap_repaired(conn):
    ev = Repairer(conn).record_gap(start_seq=4, end_seq=5, size=2)
    Repairer(conn).resolve_gap(ev, status="RESOLVED", resolution="REPAIRED",
                               details={"start": 4, "end": 5, "size": 2})
    row = conn.execute(
        "SELECT status, resolution, resolved_at FROM repair_events WHERE id=?",
        (ev,)).fetchone()
    assert row["status"] == "RESOLVED"
    assert row["resolution"] == "REPAIRED"
    assert row["resolved_at"] is not None


def test_resolve_gap_unverified(conn):
    ev = Repairer(conn).record_gap(start_seq=9, end_seq=9, size=1)
    Repairer(conn).resolve_gap(ev, status="UNVERIFIED", resolution="UNVERIFIED",
                               details={"note": "permanent"})
    row = conn.execute(
        "SELECT status, resolution FROM repair_events WHERE id=?", (ev,)
    ).fetchone()
    assert row["status"] == "UNVERIFIED"
    assert row["resolution"] == "UNVERIFIED"


# ---------------------------------------------------------------------------
# recover_gaps — the recovery loop
# ---------------------------------------------------------------------------
def test_recover_gap_repaired_when_history_available(conn):
    """A gap whose missing spin exists in the authoritative history is
    backfilled -> RESOLVED/REPAIRED (a repaired gap)."""
    # canonical: 1..5 with seq 3 missing (g3 absent)
    _seed_history(conn, [(f"g{i}", i, f"2026-08-15T00:00:0{i}Z")
                         for i in range(1, 6)])
    for i in (1, 2, 4, 5):
        _spin(conn, f"g{i}", i, i)
    outcomes = recover_gaps(conn)
    assert len(outcomes) == 1
    assert outcomes[0]["start"] == 3
    assert outcomes[0]["size"] == 1
    assert outcomes[0]["status"] == "RESOLVED"
    assert outcomes[0]["resolution"] == "REPAIRED"
    # the gap is actually gone (g3 backfilled at seq 3)
    row = conn.execute(
        "SELECT number, sequence_no FROM roulette_spins WHERE game_id='g3'"
    ).fetchone()
    assert row is not None and row["sequence_no"] == 3
    # the GAP event resolved REPAIRED
    ev = conn.execute(
        "SELECT status, resolution FROM repair_events WHERE incident_type='GAP'"
    ).fetchone()
    assert ev["status"] == "RESOLVED" and ev["resolution"] == "REPAIRED"


def test_recover_gap_unverified_when_no_authority(conn):
    """A gap with NO authoritative history to backfill from -> UNVERIFIED
    (permanent/unverified gap) — never guessed (PRD §5)."""
    for i in (1, 2, 4, 5):
        _spin(conn, f"g{i}", i, i)   # seq 3 missing, no history rows
    outcomes = recover_gaps(conn)
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "UNVERIFIED"
    assert outcomes[0]["resolution"] == "UNVERIFIED"
    ev = conn.execute(
        "SELECT status, resolution FROM repair_events WHERE incident_type='GAP'"
    ).fetchone()
    assert ev["status"] == "UNVERIFIED" and ev["resolution"] == "UNVERIFIED"
    # nothing was fabricated
    assert conn.execute(
        "SELECT COUNT(*) FROM roulette_spins WHERE sequence_no=3"
    ).fetchone()[0] == 0


def test_no_gaps_no_events(conn):
    for i in range(1, 6):
        _spin(conn, f"g{i}", i, i)
    outcomes = recover_gaps(conn)
    assert outcomes == []
    assert conn.execute(
        "SELECT COUNT(*) FROM repair_events WHERE incident_type='GAP'"
    ).fetchone()[0] == 0


def test_multiple_gaps_each_resolved(conn):
    """Two separate gaps -> two GAP events, each resolved independently."""
    for i in (1, 2, 5, 6, 9, 10):
        _spin(conn, f"g{i}", i, i)   # gaps at 3-4 and 7-8
    outcomes = recover_gaps(conn)
    assert len(outcomes) == 2
    # both UNVERIFIED (no history) — but each is its own event
    evs = conn.execute(
        "SELECT affected_count, status FROM repair_events "
        "WHERE incident_type='GAP' ORDER BY id").fetchall()
    assert [(e["affected_count"], e["status"]) for e in evs] == \
        [(2, "UNVERIFIED"), (2, "UNVERIFIED")]


# ---------------------------------------------------------------------------
# API contract — /api/integrity carries gap_events (dashboard distinction)
# ---------------------------------------------------------------------------
def test_api_integrity_gap_events(tmp_path, monkeypatch):
    """GET /api/integrity -> gap_events block with status/resolution so the
    dashboard can render 'Repaired N-spin gap' vs 'Unverified N-spin gap'."""
    import backend.db
    from collector import schema
    from fastapi.testclient import TestClient
    from backend.app import app

    db_path = tmp_path / "gaps.db"
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    schema.ensure_schema(c)
    for i in (1, 2, 4):
        _spin(c, f"g{i}", i, i)
    r = Repairer(c)
    ev_rep = r.record_gap(start_seq=3, end_seq=3, size=1)
    r.resolve_gap(ev_rep, status="RESOLVED", resolution="REPAIRED",
                  details={"start": 3, "end": 3, "size": 1})
    ev_unv = r.record_gap(start_seq=5, end_seq=5, size=1)
    r.resolve_gap(ev_unv, status="UNVERIFIED", resolution="UNVERIFIED",
                  details={"start": 5, "end": 5, "size": 1})
    c.close()

    monkeypatch.setattr(backend.db, "DB_PATH", str(db_path))
    client = TestClient(app)
    d = client.get("/api/integrity").json()
    gaps = d.get("gap_events")
    assert gaps is not None and len(gaps) == 2
    by_res = {g["resolution"]: g for g in gaps}
    assert by_res["REPAIRED"]["status"] == "RESOLVED"
    assert by_res["UNVERIFIED"]["status"] == "UNVERIFIED"
    assert by_res["REPAIRED"]["size"] == 1
