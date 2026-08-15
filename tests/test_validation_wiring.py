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
