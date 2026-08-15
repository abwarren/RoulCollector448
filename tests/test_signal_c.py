"""Signal C — authoritative recent-history comparison (PRD §12).

The rolling ~500-result history is the recovery buffer. These tests pin:

  * WS-frame history parsing (join snapshot / periodic payloads) — the
    identity-bearing source (game_id + server_ts) that gives repair authority
  * WSHistoryProvider contract
  * compare_windows on the PRD scenarios: missing middle (A B D E -> C),
    missing runs, wrong values, reordering (A B D E -> A B E D), extra local
    spins, and the identity gate (number-only history detects but never
    repairs — identity is never manufactured, PRD §5/§14/§25)
"""

import pytest

from collector.history import (
    DBHistoryProvider,
    WSHistoryProvider,
    parse_history_frame,
)
from collector.reconciler import (
    HistoryProvider,
    HistoryRecord,
    compare_windows,
    reconcile,
)


def canon(game_id, number, ts=None):
    return {"game_id": game_id, "number": number,
            "server_ts": ts or f"2026-08-15T00:00:{number:02d}Z"}


def rem(game_id, number, ts=None):
    return HistoryRecord(game_id=game_id, number=number,
                         server_ts=ts or f"2026-08-15T00:00:{number:02d}Z")


# ---------------------------------------------------------------------------
# WS frame parsing
# ---------------------------------------------------------------------------
def test_parse_history_frame_detects_list():
    """args.history = newest-first list with gameId + timestamp."""
    payload = {
        "args": {
            "code": 3,
            "history": [
                {"gameId": "g5", "number": 17, "timestamp": "2026-08-15T00:05:00Z"},
                {"gameId": "g4", "number": 0, "timestamp": "2026-08-15T00:04:00Z"},
                {"gameId": "g3", "number": 23, "timestamp": "2026-08-15T00:03:00Z"},
            ],
        }
    }
    recs = parse_history_frame(payload)
    assert recs is not None
    assert [r.game_id for r in recs] == ["g5", "g4", "g3"]  # order kept
    assert [r.number for r in recs] == [17, 0, 23]
    assert recs[0].server_ts == "2026-08-15T00:05:00Z"


def test_parse_history_frame_oldest_first_reversed():
    """Oldest-first source (timestamps ascending) is reversed to newest-first."""
    payload = {
        "history": [
            {"gameId": "g1", "number": 1, "timestamp": "2026-08-15T00:01:00Z"},
            {"gameId": "g2", "number": 2, "timestamp": "2026-08-15T00:02:00Z"},
            {"gameId": "g3", "number": 3, "timestamp": "2026-08-15T00:03:00Z"},
        ]
    }
    recs = parse_history_frame(payload)
    assert [r.game_id for r in recs] == ["g3", "g2", "g1"]


def test_parse_history_frame_spin_message_none():
    """The ordinary single-spin message is NOT history — returns None."""
    payload = {"args": {"code": 1, "description": "1 Red", "gameId": "g9",
                        "timestamp": "2026-08-15T00:00:00Z"}}
    assert parse_history_frame(payload) is None


def test_parse_history_frame_skips_garbage_entries():
    payload = {
        "records": [
            {"gameId": "g1", "number": 5},
            {"foo": "bar"},                      # not spin-shaped
            {"gameId": "g2", "number": 99},       # out of range -> skipped
            {"gameId": "g3", "value": 12},        # alternative key
        ]
    }
    recs = parse_history_frame(payload)
    assert [r.number for r in recs] == [5, 12]
    assert [r.game_id for r in recs] == ["g1", "g3"]


def test_ws_history_provider_roundtrip():
    recs = [HistoryRecord(game_id="a", number=1),
            HistoryRecord(game_id="b", number=2)]
    p = WSHistoryProvider(recs)
    assert p.max_window == 2
    got = p.fetch_recent_history(limit=1)
    assert len(got) == 1 and got[0].game_id == "a"


# ---------------------------------------------------------------------------
# Durable Signal C: DBHistoryProvider (history observations survive restarts)
# ---------------------------------------------------------------------------
def test_db_history_provider_loads_dedupes_orders(tmp_path):
    """History observations persisted by the collector (source='history')
    reload across restarts: deduped by game_id, newest-first, non-history
    observations excluded."""
    import sqlite3
    from collector import observer, schema

    conn = sqlite3.connect(tmp_path / "h.db")
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    sid = observer.start_session(conn)

    # two overlapping snapshots: g3 appears in both -> must dedupe
    for rec in [("g1", 1, "2026-08-15T00:01:00Z"),
                ("g2", 2, "2026-08-15T00:02:00Z"),
                ("g3", 3, "2026-08-15T00:03:00Z")]:
        observer.record_observation(conn, source="history", session_id=sid,
                                    game_id=rec[0], number=rec[1], server_ts=rec[2])
    # second snapshot repeats g2/g3 (overlap) + adds g4 (newest)
    for rec in [("g2", 2, "2026-08-15T00:02:00Z"),
                ("g3", 3, "2026-08-15T00:03:00Z"),
                ("g4", 4, "2026-08-15T00:04:00Z")]:
        observer.record_observation(conn, source="history", session_id=sid,
                                    game_id=rec[0], number=rec[1], server_ts=rec[2])
    # a websocket observation must NOT leak into history
    observer.record_observation(conn, source="websocket", session_id=sid,
                                game_id="w1", number=9, server_ts="2026-08-15T00:09:00Z")

    p = DBHistoryProvider(conn)
    recs = p.fetch_recent_history(limit=10)
    assert [r.game_id for r in recs] == ["g4", "g3", "g2", "g1"]  # newest-first
    assert len(recs) == 4                                          # deduped
    assert all(r.game_id != "w1" for r in recs)                    # websocket excluded
    assert p.max_window == 4
    conn.close()


def test_db_history_provider_empty_without_history(tmp_path):
    import sqlite3
    from collector import observer, schema

    conn = sqlite3.connect(tmp_path / "h2.db")
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    sid = observer.start_session(conn)
    observer.record_observation(conn, source="websocket", session_id=sid,
                                game_id="w1", number=9, server_ts="t")
    assert DBHistoryProvider(conn).fetch_recent_history() == []
    conn.close()


def test_reconcile_via_db_history_finds_missing(tmp_path):
    """End-to-end: persisted history observations drive the same repair plan
    as the in-memory buffer (restart-safe Signal C)."""
    import os
    import sqlite3
    from collector import observer, schema

    db_path = tmp_path / "h3.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    sid = observer.start_session(conn)
    # authoritative history: newest-first g5..g1 (local is missing g3)
    for gid, num, ts in [("g5", 5, "2026-08-15T00:05:00Z"),
                         ("g4", 4, "2026-08-15T00:04:00Z"),
                         ("g3", 3, "2026-08-15T00:03:00Z"),
                         ("g2", 2, "2026-08-15T00:02:00Z"),
                         ("g1", 1, "2026-08-15T00:01:00Z")]:
        observer.record_observation(conn, source="history", session_id=sid,
                                    game_id=gid, number=num, server_ts=ts)
    conn.close()  # the "restart" — only the file remains

    local = [{"game_id": "g1", "number": 1, "server_ts": "2026-08-15T00:01:00Z"},
             {"game_id": "g2", "number": 2, "server_ts": "2026-08-15T00:02:00Z"},
             {"game_id": "g4", "number": 4, "server_ts": "2026-08-15T00:04:00Z"},
             {"game_id": "g5", "number": 5, "server_ts": "2026-08-15T00:05:00Z"}]
    old = os.environ.get("RC_DB_PATH")
    os.environ["RC_DB_PATH"] = str(db_path)
    try:
        result = reconcile(local, DBHistoryProvider(), window=10)
    finally:
        if old is None:
            os.environ.pop("RC_DB_PATH", None)
        else:
            os.environ["RC_DB_PATH"] = old
    assert result.repairable
    assert [r.game_id for r in result.plan.missing] == ["g3"]
    assert result.plan.authoritative


# ---------------------------------------------------------------------------
# compare_windows — PRD scenarios
# ---------------------------------------------------------------------------
def test_missing_middle_detected():
    """A B D E -> C missing (PRD §11 game-ID continuity)."""
    local = [canon("e", 5), canon("d", 4), canon("b", 2), canon("a", 1)]  # newest-first
    remote = [rem("e", 5), rem("d", 4), rem("c", 3), rem("b", 2), rem("a", 1)]
    plan = compare_windows(local, remote)
    assert plan.authoritative and plan.repairable
    assert [r.game_id for r in plan.missing] == ["c"]
    assert not plan.corrections and not plan.extras and not plan.reorder


def test_missing_run_detected():
    local = [canon("d", 4), canon("a", 1)]
    remote = [rem("f", 6), rem("e", 5), rem("d", 4), rem("c", 3), rem("b", 2),
              rem("a", 1)]
    plan = compare_windows(local, remote)
    assert {r.game_id for r in plan.missing} == {"f", "e", "c", "b"}
    assert not plan.corrections and not plan.extras


def test_wrong_value_with_identity_corrects():
    """PRD §15: same game_id, DIFFERENT number -> same identity (level 1),
    the value difference is a correction (never a silent overwrite)."""
    local = [canon("g", 17), canon("h", 2)]
    remote = [rem("g", 23), rem("h", 2)]  # authoritative says g=23, not 17
    plan = compare_windows(local, remote)
    assert plan.repairable
    assert ("g", 17, 23) in plan.corrections
    assert not plan.missing and not plan.extras


def test_number_only_history_detects_but_never_repairs():
    """DOM-style history: numbers only, NO identity -> repairable False.
    Discrepancy still detected (missing populated) but the plan refuses
    repair (PRD §5/§25 — never manufacture identity)."""
    local = [canon("e", 5), canon("d", 4), canon("c", 3), canon("b", 2),
             canon("a", 1)]
    remote = [HistoryRecord(game_id=None, number=6),   # DOM text has no gid/ts
              HistoryRecord(game_id=None, number=5),
              HistoryRecord(game_id=None, number=4),
              HistoryRecord(game_id=None, number=3),
              HistoryRecord(game_id=None, number=2),
              HistoryRecord(game_id=None, number=1)]
    plan = compare_windows(local, remote)
    assert plan.authoritative          # history IS present
    assert not plan.repairable         # but no identity -> detection only
    assert len(plan.missing) == 1      # the 6 at the tail is detected
    assert not plan.corrections        # and never auto-corrected


def test_reorder_detected_and_absolute_start():
    """A B D E -> A B E D: same game_id set, different order -> renumber the
    MINIMAL out-of-order suffix to the AUTHORITATIVE (remote) order. The
    remote chronological order is a,d,b,e — so d→2, b→3; a is anchored."""
    local = [canon("e", 5), canon("d", 4), canon("b", 2), canon("a", 1)]  # [e,d,b,a]
    remote = [rem("e", 5), rem("b", 2), rem("d", 4), rem("a", 1)]         # [e,b,d,a]
    plan = compare_windows(local, remote)
    assert plan.repairable
    # walk: e matches, then divergent local [d,b,a] vs remote [b,d,a] — same
    # set, different order; the remote (authoritative) chronological order
    # is a,d,b,e -> only the d,b suffix is renumbered, in remote order
    assert plan.reorder == ["d", "b"]          # authoritative, oldest-first
    assert plan.renumber == [("d", 2), ("b", 3)]  # absolute positions
    assert not plan.missing and not plan.corrections and not plan.extras


def test_reorder_detected_via_reconcile():
    """Full reconcile() sets renumber + absolute sequences (base 0 here)."""
    local = [canon("a", 1), canon("b", 2), canon("d", 4), canon("e", 5)]  # oldest-first
    remote_recs = [rem("e", 5), rem("b", 2), rem("d", 4), rem("a", 1)]    # newest-first
    result = reconcile(local, _Static(remote_recs), window=10)
    assert not result.ok
    assert result.reorder_count == 2
    assert result.plan.renumber == [("d", 2), ("b", 3)]  # absolute (base 0)


def test_insertion_shift_repairs_suffix():
    """PRD §13 example: local missed the middle spin (g1024), so every NEWER
    record is misaligned. Engine detects the shift and renumbers the whole
    affected suffix to its authoritative positions.

    Local: 1021:7 1022:4 1023:19 1024:32 1025:8   (g1024 missed)
    Remote: 1021:7 1022:4 1023:19 1024:17 1025:32 1026:8
    -> 1024:17 backfilled at seq 4; 32 renumbered to 5; 8 renumbered to 6
    (with window base 1020 these are exactly 1024/1025/1026).
    """
    local = [canon("g1021", 7, "t1"), canon("g1022", 4, "t2"),
             canon("g1023", 19, "t3"), canon("g1025", 32, "t4"),
             canon("g1026", 8, "t5")]  # oldest-first
    remote_recs = [rem("g1026", 8, "t5"), rem("g1025", 32, "t4"),
                   rem("g1024", 17, "t4"), rem("g1023", 19, "t3"),
                   rem("g1022", 4, "t2"), rem("g1021", 7, "t1")]  # newest-first
    result = reconcile(local, _Static(remote_recs), window=10)
    assert not result.ok
    # the missed middle spin
    assert [r.game_id for r in result.plan.missing] == ["g1024"]
    assert result.plan.missing_seq["g1024"] == 4        # rel seq (abs 1024 w/ base 1020)
    # the affected suffix renumbered to its authoritative positions
    assert set(result.plan.renumber) == {("g1025", 5), ("g1026", 6)}  # 32->5, 8->6
    assert result.reorder_count == 2
    assert result.plan.authoritative and result.repairable


def test_extra_local_flagged_not_deleted():
    """A local spin with NO counterpart in authoritative history is flagged
    (incident), never deleted (PRD forbids data destruction)."""
    local = [canon("x", 9), canon("b", 2), canon("a", 1)]  # x = phantom extra
    remote = [rem("b", 2), rem("a", 1)]
    plan = compare_windows(local, remote)
    assert "x" in plan.extras
    assert not plan.missing and not plan.corrections
    assert not plan.reorder


class _Static(HistoryProvider):
    def __init__(self, records):
        self._records = records

    def fetch_recent_history(self, limit=500):
        return self._records[:limit]

    @property
    def max_window(self):
        return len(self._records)
