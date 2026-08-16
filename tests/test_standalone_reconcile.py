"""Standalone reconcile worker — one full pass against a real tmp DB.

run_once(conn) is the testable core of scripts/standalone_reconcile.py (the
worker's loop is just `sleep -> run_once` forever). Env is set BEFORE the
collector modules are imported (module-level paths are resolved at import —
same pattern as tests/test_dedup_key.py), and DBHistoryProvider resolves the
DB path from RC_DB_PATH at call time, so a per-test override is enough.

Scenario pinned here: 5 spins g1..g5 recorded as source='history'
observations (the durable authority), canonical rows present except g3 —
one pass must detect the miss and, being repairable (game_id identity),
backfill g3 at its authoritative position.
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
from scripts.standalone_reconcile import run_once  # noqa: E402

REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def _color(n):
    return "Red" if n in REDS else "Black"


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    return conn


def _seed_history(conn, spins):
    """Record authoritative history observations (source='history')."""
    sid = observer.start_session(conn, source="standalone-test")
    for gid, number, ts in spins:
        observer.record_observation(
            conn, source="history", session_id=sid, game_id=gid,
            number=number, server_ts=ts, description=f"{number} {_color(number)}",
            raw_payload=f'{{"gameId":"{gid}"}}',
        )


def _insert_canonical(conn, spins):
    """Insert canonical rows (oldest-first so id order == time order)."""
    for i, (gid, number, ts) in enumerate(spins, start=1):
        conn.execute(
            "INSERT INTO roulette_spins (number, description, color, game_id, "
            "server_ts, captured_at, sequence_no, dedup_key) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (number, f"{number} {_color(number)}", _color(number), gid, ts, ts,
             i, f"gid:{gid}"),
        )
    conn.commit()


# spins g1..g5 (g1 oldest .. g5 newest) — shared by the scenarios below
_SPINS = [(f"g{i}", i, f"2026-08-15T00:00:0{i}Z") for i in range(1, 6)]


def test_run_once_detects_missing_and_repairs(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("RC_DB_PATH", str(db_path))
    conn = _connect(db_path)
    try:
        _seed_history(conn, _SPINS)
        # canonical rows MISSING g3 — the silent WS miss scenario
        _insert_canonical(conn, [s for s in _SPINS if s[0] != "g3"])

        details = run_once(conn)

        # detection: the miss is in the returned details
        assert details["missing"] == 0      # after re-verify, no longer missing
        assert details["window"] == 5
        assert details["ok"] is True        # re-verify: repaired -> verified
        assert details["repairable"] is True
        assert details["message"] == "verified"

        # repair applied: g3 now exists with the authoritative number
        # and its exact position in the window (sequence_no 3 of 5)
        row = conn.execute(
            "SELECT number, sequence_no FROM roulette_spins WHERE game_id='g3'"
        ).fetchone()
        assert row is not None
        assert row["number"] == 3
        assert row["sequence_no"] == 3
        assert conn.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0] == 5

        # audit trail: a RECONCILIATION event + a repair_events row
        evs = conn.execute(
            "SELECT event_type FROM integrity_events ORDER BY id").fetchall()
        assert [e["event_type"] for e in evs] == ["RECONCILIATION"]
        assert conn.execute(
            "SELECT COUNT(*) FROM repair_events").fetchone()[0] >= 1
    finally:
        conn.close()


def test_run_once_clean_pass_verifies(tmp_path, monkeypatch):
    db_path = tmp_path / "clean.db"
    monkeypatch.setenv("RC_DB_PATH", str(db_path))
    conn = _connect(db_path)
    try:
        _seed_history(conn, _SPINS)
        _insert_canonical(conn, _SPINS)  # nothing missing

        details = run_once(conn)

        assert details["ok"] is True
        assert details["missing"] == 0
        assert details["corrections"] == 0
        assert details["duplicates"] == 0
        assert details["reordered"] == 0
        assert details["extras"] == 0
        assert details["message"] == "verified"
        # no repairs were needed
        assert conn.execute(
            "SELECT COUNT(*) FROM repair_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_run_once_empty_db_skips_gracefully(tmp_path, monkeypatch):
    db_path = tmp_path / "empty.db"
    monkeypatch.setenv("RC_DB_PATH", str(db_path))
    conn = _connect(db_path)
    try:
        details = run_once(conn)
        assert details["ok"] is True
        assert details["window"] == 0
        assert "nothing to reconcile" in details["message"]
        # still observable: the pass was logged
        evs = conn.execute(
            "SELECT event_type FROM integrity_events ORDER BY id").fetchall()
        assert [e["event_type"] for e in evs] == ["RECONCILIATION"]
    finally:
        conn.close()
