"""Per-spin fast validation wiring (PRD §31 — the fastest interval).

The validator module existed but was never CALLED by the collector — dead
code in the live path. These tests pin that every new canonical spin now
passes through validate_new_spin: clean spins log nothing, SUSPECT/INVALID
spins log integrity events with severity, the counter bumps, and it never
raises (capture must never depend on validation).

Importing collector.roulette2_collector triggers its credential guard at
module import, so dummy env creds are set before import (harmless — they
are never used here).
"""

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SUNBET_USER", "test")
os.environ.setdefault("SUNBET_PASS", "test")

import pytest  # noqa: E402

from collector import observer  # noqa: E402
import collector.roulette2_collector as rc  # noqa: E402


def _spin(number, gid, ts=None, desc=None):
    if ts is None:
        ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    return {"number": number,
            "description": desc or f"{number} X",
            "gameId": gid, "timestamp": ts,
            "captured_at": datetime.now(timezone.utc).isoformat()}


def test_clean_spin_logs_nothing(monkeypatch):
    logged = []
    monkeypatch.setattr(observer, "log_event",
                        lambda *a, **k: logged.append(k) or 1)
    state = {"validation_issues": 0, "session_id": "s1"}
    rc.validate_new_spin(state, _spin(17, "g1", desc="17 Black"), None)
    assert logged == []
    assert state["validation_issues"] == 0


def test_invalid_spin_logs_critical(monkeypatch):
    logged = []
    monkeypatch.setattr(observer, "log_event",
                        lambda *a, **k: logged.append(k) or 1)
    state = {"validation_issues": 0, "session_id": "s1"}
    rc.validate_new_spin(state, _spin(99, "g2"), None)  # out of range
    assert logged and logged[0]["severity"] == "CRITICAL"
    assert logged[0]["game_id"] == "g2"
    assert state["validation_issues"] == 1


def test_suspect_spin_logs_warning(monkeypatch):
    """A cadence gap on a VALID spin is SUSPECT (warning), not invalid."""
    logged = []
    monkeypatch.setattr(observer, "log_event",
                        lambda *a, **k: logged.append(k) or 1)
    state = {"validation_issues": 0, "session_id": "s1"}
    prev = _spin(17, "g1", ts=(datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat(),
                 desc="17 Black")
    curr = _spin(0, "g2", ts=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                 desc="0 Green")  # 3-min gap >= 120s -> cadence gap
    rc.validate_new_spin(state, curr, prev)
    assert logged and logged[0]["severity"] == "WARNING"
    assert any("cadence gap" in str(p) for p in logged[0].get("details", {}).get("problems", []))
    assert state["validation_issues"] == 1


def test_never_raises(monkeypatch):
    """Validation failure must never break capture."""
    monkeypatch.setattr(observer, "log_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    state = {"validation_issues": 0, "session_id": "s1"}
    rc.validate_new_spin(state, _spin(99, "g2"), None)  # would log -> raises
    assert state["validation_issues"] == 1              # counter still bumped


def test_color_contradiction_flagged_end_to_end(monkeypatch):
    """PRD §17: a source observation '17 Red' is flagged (never silently
    normalized to '17 Black' by the save path)."""
    logged = []
    monkeypatch.setattr(observer, "log_event",
                        lambda *a, **k: logged.append(k) or 1)
    state = {"validation_issues": 0, "session_id": "s1"}
    # 17 is Black; the source says Red -> contradiction
    rc.validate_new_spin(state, _spin(17, "g1", desc="17 Red"), None)
    assert state["validation_issues"] == 1
    assert logged
    problems = logged[0].get("details", {}).get("problems", [])
    assert any("color contradiction" in p for p in problems)
    # severity SUSPECT (the number itself is valid — the contradiction is a
    # source-integrity flag, not structural invalidity)
    assert logged[0]["severity"] in ("WARNING", "CRITICAL")


def test_latency_tracker_fed_per_spin():
    """PRD §19: validate_new_spin feeds the session latency tracker."""
    from datetime import datetime, timedelta, timezone
    from collector.validator import LatencyTracker
    tr = LatencyTracker()
    state = {"validation_issues": 0, "session_id": "s1",
             "latency_tracker": tr, "latency_last_alert": 0.0}
    now = datetime.now(timezone.utc)
    for i in range(5):
        ts = (now - timedelta(seconds=10 - i)).isoformat()
        rc.validate_new_spin(state, _spin(17, f"g{i}", ts=ts,
                                          desc="17 Black"), None)
    stats = tr.stats()
    assert stats["n"] == 5
    assert stats["p50"] is not None


def test_observation_latency_persisted():
    """PRD §18/§19: every observation row carries its capture_latency."""
    import sqlite3
    from collector import observer, schema
    db = os.path.join(os.path.dirname(os.path.dirname(__file__)), "obs_lat.db")
    if os.path.exists(db):
        os.remove(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    sid = observer.start_session(conn)
    observer.record_observation(
        conn, source="websocket", session_id=sid, game_id="g1", number=17,
        description="17 Black", server_ts="2026-08-15T23:00:00Z",
        capture_latency=0.6, commit_latency=0.4,
    )
    row = conn.execute(
        "SELECT capture_latency, commit_latency FROM spin_observations"
    ).fetchone()
    assert row["capture_latency"] == 0.6
    assert row["commit_latency"] == 0.4
    conn.close()
    os.remove(db)


def test_score_components_real_inputs():
    """§22: the six components reflect real per-pass data."""
    from collector.reconciler import ReconciliationResult, RepairPlan
    # a broken pass: 2 renumbered, 1 conflict, reconciliation failed
    plan = RepairPlan(
        renumber=[("b", 2), ("c", 3)],
        duplicates=["a"],
        duplicate_kinds={"a": "CONFLICT"},
        window_achieved=500, authoritative=True, repairable=True,
    )
    result = ReconciliationResult(
        ok=False, window=500, message="conflict",
        plan=plan, missing_count=0, correction_count=0,
        duplicate_count=1, reorder_count=2, extra_count=0, repairable=True,
    )
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    spins = [{"captured_at": (now - timedelta(seconds=30)).isoformat(),
              "timestamp": (now - timedelta(seconds=31)).isoformat(),
              "number": 17} for _ in range(50)]
    state = {"spins": spins}
    comps = rc._score_components(state, result)
    assert comps["reconciliation"] == 0.0      # failed
    assert comps["duplicates"] == 0.0          # CONFLICT
    assert comps["sequence"] < 1.0             # renumbered
    assert comps["freshness"] == 1.0           # fresh spins
    assert comps["timestamps"] == 1.0          # clean timestamps
