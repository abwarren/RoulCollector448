"""Truncated-window base bug — backfill/renumber positions must be ABSOLUTE.

The bug: reconcile() derived the window base from the PASSED list length
(base = len(local_spins) - len(local)). When the caller truncates the
dataset to the latest `window` rows (the normal case — run_once loads the
latest 500 of a growing dataset), the derived base is 0 instead of the
true number of preceding records. A backfill then lands at the WRONG
absolute sequence (window-relative), the hole shifts, re-verify fails, and
a recoverable gap reports UNVERIFIED forever.

Fixed: reconcile(base=...) takes the true base from the caller; the
derived fallback only applies to full-dataset calls.
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
from collector.reconciler import HistoryRecord, reconcile  # noqa: E402
from collector.history import StaticHistoryProvider  # noqa: E402
from scripts.standalone_reconcile import recover_gaps  # noqa: E402

REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def _iso(i):
    return f"2026-08-15T00:{i // 60:02d}:{i % 60:02d}Z"


def _color(n):
    return "Green" if n == 0 else ("Red" if n in REDS else "Black")


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    db_path = str(tmp_path / "base.db")
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
        (num, f"{num} {_color(num)}", _color(num), gid, _iso(seq), _iso(seq),
         seq, status),
    )
    c.commit()


def _seed_history(c, spins):
    sid = observer.start_session(c, source="base-test")
    for gid, number, ts in spins:
        observer.record_observation(
            c, source="history", session_id=sid, game_id=gid,
            number=number, server_ts=ts,
            description=f"{number} {_color(number)}",
            raw_payload=f'{{"gameId":"{gid}"}}',
        )


def test_reconcile_truncated_window_absolute_position(conn):
    """A 1200-row dataset, window 500, missing spin at seq 1000 (inside the
    window, base 700). With base passed, the backfill lands at 1000 — not
    at the window-relative 300."""
    for i in range(1, 1201):
        _spin(conn, f"g{i}", i % 37, i)
    _seed_history(conn, [(f"g{i}", i % 37, _iso(i)) for i in range(1, 1201)])
    # delete a spin inside the window (seq 1000; window = 701..1200)
    conn.execute("DELETE FROM roulette_spins WHERE game_id='g1000'")
    conn.commit()

    # the caller truncates to the latest 500 (the normal run_once pattern)
    rows = conn.execute(
        "SELECT game_id, number, server_ts, sequence_no FROM roulette_spins "
        "ORDER BY sequence_no IS NULL, sequence_no DESC, id DESC LIMIT 500"
    ).fetchall()
    local_oldest = list(reversed(
        [{"game_id": r[0], "number": r[1], "server_ts": r[2]} for r in rows]))
    # the authoritative remote = the latest 500 HISTORY records (id-based):
    # g1200..g701, INCLUDING g1000, EXCLUDING g700 (the local window pulls
    # g700 in because the hole shifts the sequence-based LIMIT).
    remote = [HistoryRecord(game_id=f"g{i}", number=i % 37, server_ts=_iso(i))
              for i in range(1200, 700, -1)]  # newest-first, 500 records
    # the absolute base comes from the REMOTE window's oldest record (the
    # authority): remote[-1] = g701 has sequence 701 -> base = 700.
    # g1000's relative position = len(remote) - rpos = 500 - 200 = 300
    # -> absolute 700 + 300 = 1000. Correct.
    base = 700
    result = reconcile(local_oldest, StaticHistoryProvider(remote),
                       window=500, base=base)
    # the missing g1000 is detected at its ABSOLUTE position
    assert any(r.game_id == "g1000" for r in result.plan.missing)
    assert result.plan.missing_seq.get("g1000") == 1000


def test_reconcile_without_base_is_window_relative(conn):
    """Without base, the same truncated call places the backfill
    window-relative (the OLD buggy behavior — 300, not 1000)."""
    for i in range(1, 1201):
        _spin(conn, f"g{i}", i % 37, i)
    _seed_history(conn, [(f"g{i}", i % 37, _iso(i)) for i in range(1, 1201)])
    conn.execute("DELETE FROM roulette_spins WHERE game_id='g1000'")
    conn.commit()
    rows = conn.execute(
        "SELECT game_id, number, server_ts FROM roulette_spins "
        "ORDER BY sequence_no IS NULL, sequence_no DESC, id DESC LIMIT 500"
    ).fetchall()
    local_oldest = list(reversed(
        [{"game_id": r[0], "number": r[1], "server_ts": r[2]} for r in rows]))
    remote = [HistoryRecord(game_id=f"g{i}", number=i % 37, server_ts=_iso(i))
              for i in range(1200, 700, -1)]
    result = reconcile(local_oldest, StaticHistoryProvider(remote),
                       window=500)   # NO base -> derived 0
    assert result.plan.missing_seq.get("g1000") == 300


def test_recover_gaps_truncated_repairs_absolute(conn):
    """End-to-end: recover_gaps on a truncated window backfills at the true
    absolute sequence and the gap resolves REPAIRED."""
    for i in range(1, 1201):
        _spin(conn, f"g{i}", i % 37, i)
    _seed_history(conn, [(f"g{i}", i % 37, _iso(i)) for i in range(1, 1201)])
    conn.execute("DELETE FROM roulette_spins WHERE game_id='g1000'")
    conn.commit()
    outcomes = recover_gaps(conn, window=500)
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "RESOLVED"
    assert outcomes[0]["resolution"] == "REPAIRED"
    # backfilled at the TRUE absolute position
    row = conn.execute(
        "SELECT sequence_no, status FROM roulette_spins WHERE game_id='g1000'"
    ).fetchone()
    assert row is not None
    assert row["sequence_no"] == 1000
    assert row["status"] == "REPAIRED"
    # the window is now gap-free (seq 701..1200 contiguous)
    seqs = sorted(r[0] for r in conn.execute(
        "SELECT sequence_no FROM roulette_spins WHERE sequence_no >= 700"))
    assert seqs == list(range(700, 1201))
