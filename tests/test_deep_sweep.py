"""PRD §31 'every 5 min' — the deep integrity sweep.

The collector's own reconcile loop runs every 30s/60s but dies with the
process. deep_sweep() in the standalone worker is the every-5-minutes
full-window deep integrity sweep: full-window reconciliation against
authoritative history + gap recovery + the six data-health signals —
decoupled from the collector so it runs even when the collector is down.
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
from scripts.standalone_reconcile import DEEP_SWEEP_S, deep_sweep  # noqa: E402

REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def _color(n):
    return "Green" if n == 0 else ("Red" if n in REDS else "Black")


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    # point RC_DB_PATH at THIS test's DB so run_once's DBHistoryProvider
    # (resolved at call time) reads the same history as the fixture conn
    db_path = str(tmp_path / "sweep.db")
    monkeypatch.setenv("RC_DB_PATH", db_path)
    c = sqlite3.connect(db_path)
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
    sid = observer.start_session(c, source="sweep-test")
    for gid, number, ts in spins:
        observer.record_observation(
            c, source="history", session_id=sid, game_id=gid,
            number=number, server_ts=ts,
            description=f"{number} {_color(number)}",
            raw_payload=f'{{"gameId":"{gid}"}}',
        )


def _iso(i):
    """Unique ISO timestamp per index (seconds past the hour, unique for
    i < 3600 — the tests use well under that)."""
    return f"2026-08-15T00:{i // 60:02d}:{i % 60:02d}Z"


def test_deep_sweep_healthy(conn):
    """A clean dataset WITH authoritative history -> healthy sweep
    (reconciliation ok, no gaps, no repairs, good score)."""
    _seed_history(conn, [(f"g{i}", i % 37, _iso(i))
                         for i in range(1, 501)])
    for i in range(1, 501):
        _spin(conn, f"g{i}", i % 37, i)
    sweep = deep_sweep(conn)
    assert sweep["reconciliation"]["ok"] is True
    assert sweep["gaps"] == []
    assert sweep["data_health"]["sequence_health"] is True
    assert sweep["data_health"]["repair_queue"] == 0
    assert sweep["healthy"] is True


def test_deep_sweep_reconciles_and_repairs(conn):
    """A gap whose missing spin exists in history -> the sweep repairs it
    and reports healthy (the 'every 5 min' self-healing)."""
    _seed_history(conn, [(f"g{i}", i, f"2026-08-15T00:00:0{i}Z")
                         for i in range(1, 6)])
    for i in (1, 2, 4, 5):
        _spin(conn, f"g{i}", i, i)   # seq 3 missing
    sweep = deep_sweep(conn)
    # the gap was repaired — the missing spin now exists at its sequence
    row = conn.execute(
        "SELECT number, sequence_no FROM roulette_spins WHERE game_id='g3'"
    ).fetchone()
    assert row is not None and row["sequence_no"] == 3
    # the sweep reports healthy (sequence intact after repair)
    assert sweep["data_health"]["sequence_health"] is True
    assert sweep["healthy"] is True


def test_deep_sweep_unhealthy_on_permanent_gap(conn):
    """A gap with NO history to repair from -> UNVERIFIED, sweep unhealthy
    (a permanent/unverified gap is surfaced, never guessed)."""
    for i in (1, 2, 4, 5):
        _spin(conn, f"g{i}", i, i)   # seq 3 missing, no history
    sweep = deep_sweep(conn)
    assert any(o["resolution"] == "UNVERIFIED" for o in sweep["gaps"])
    assert sweep["data_health"]["sequence_health"] is False
    assert sweep["healthy"] is False


def test_deep_sweep_decoupled_from_collector(conn):
    """The sweep runs WITHOUT the collector process — it only needs the DB
    connection (the collector may be down; the sweep still reconciles +
    evaluates data health)."""
    # the fixture conn IS the collector's DB — the sweep runs against it
    # directly, no collector process involved
    s = deep_sweep(conn)
    assert isinstance(s, dict)
    assert "reconciliation" in s and "gaps" in s and "data_health" in s


def test_deep_sweep_interval_is_5_minutes():
    assert DEEP_SWEEP_S == 300


def test_deep_sweep_reports_all_six_signals(conn):
    """The sweep's data_health carries the six PRD §29 signals."""
    _seed_history(conn, [(f"g{i}", i % 37, _iso(i))
                         for i in range(1, 501)])
    for i in range(1, 501):
        _spin(conn, f"g{i}", i % 37, i)
    sweep = deep_sweep(conn)
    dh = sweep["data_health"]
    assert set(dh) >= {"sequence_health", "reconciliation_health",
                       "repair_queue", "data_health_score", "healthy"}
    # the sweep's own run_once writes a fresh RECONCILIATION event with a
    # score — it must be a real number (present), not None
    assert dh["data_health_score"] is not None
    assert dh["sequence_health"] is True
    assert dh["healthy"] is True
