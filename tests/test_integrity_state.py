"""Phase 5 — state machine, health score, telemetry, source agreement."""

import pytest

from collector.integrity_state import (
    DataHealthScore,
    IntegrityState,
    RecoveryStateMachine,
    Telemetry,
    score_band,
)
from collector.source_agreement import cross_check, cross_check_and_log
from collector import observer, schema
import sqlite3


# ---------------------------------------------------------------------------
# Data-health score (PRD §22)
# ---------------------------------------------------------------------------
def test_perfect_score():
    s = DataHealthScore()
    assert s.compute() == 100.0
    assert s.band(100.0) == "VERIFIED"


def test_score_weights_component():
    s = DataHealthScore()
    # reconciliation broken (0.0) -> lose its 25% weight
    assert s.compute(reconciliation=0.0) == 75.0
    # everything broken
    assert s.compute(freshness=0, sequence=0, reconciliation=0,
                     duplicates=0, timestamps=0, source_agreement=0) == 0.0


def test_bands():
    assert score_band(100) == "VERIFIED"
    assert score_band(98) == "VERIFIED"
    assert score_band(97) == "HEALTHY"
    assert score_band(93) == "DEGRADED"
    assert score_band(80) == "WARNING"
    assert score_band(50) == "CRITICAL"


def test_custom_weights():
    s = DataHealthScore(weights={"sequence": 1.0})
    assert s.compute(sequence=0.5) == 50.0


# ---------------------------------------------------------------------------
# State machine (PRD §21 — process health != data health)
# ---------------------------------------------------------------------------
def test_state_machine_healthy_cycle():
    sm = RecoveryStateMachine()
    assert sm.state == IntegrityState.HEALTHY
    assert sm.observe(reconciled_ok=True) == IntegrityState.HEALTHY


def test_state_machine_degrades_on_failed_reconcile():
    sm = RecoveryStateMachine()
    sm.observe(reconciled_ok=False)
    assert sm.state == IntegrityState.DEGRADED


def test_state_machine_repair_then_recover():
    sm = RecoveryStateMachine()
    sm.observe(reconciled_ok=False, repairing=True)
    assert sm.state == IntegrityState.RECONCILING
    sm.observe(reconciled_ok=True)
    assert sm.state == IntegrityState.HEALTHY


def test_state_machine_recovering():
    sm = RecoveryStateMachine()
    sm.observe(reconciled_ok=True, recovering=True)
    assert sm.state == IntegrityState.RECOVERING


def test_state_transitions_recorded():
    sm = RecoveryStateMachine()
    sm.observe(reconciled_ok=False)
    sm.observe(reconciled_ok=True)
    h = sm.history()
    assert len(h) == 2
    assert h[0][0] == "HEALTHY" and h[0][1] == "DEGRADED"


# ---------------------------------------------------------------------------
# Telemetry (PRD §41)
# ---------------------------------------------------------------------------
def test_telemetry_counters():
    t = Telemetry()
    t.inc("ws_frames_received", 10)
    t.inc("ws_spins_accepted", 2)
    t.inc("dom_polls")
    snap = t.snapshot()
    assert snap["ws_frames_received"] == 10
    assert snap["ws_spins_accepted"] == 2
    assert snap["dom_polls"] == 1
    assert snap["ws_spin_candidates"] == 0  # untouched default


def test_telemetry_ignores_unknown():
    t = Telemetry()
    t.inc("bogus_counter")
    assert "bogus_counter" not in t.snapshot()


def test_telemetry_merge():
    t = Telemetry()
    t.merge({"ws_frames_received": 5, "repairs": 2})
    assert t.snapshot()["ws_frames_received"] == 5
    assert t.snapshot()["repairs"] == 2


# ---------------------------------------------------------------------------
# Source agreement (PRD §20)
# ---------------------------------------------------------------------------
def test_agreement_verified():
    ws = {"number": 23}
    dom = {"number": 23}
    assert cross_check(None, ws, dom)["status"] == "VERIFIED"


def test_agreement_conflict():
    ws = {"number": 23}
    dom = {"number": 18}
    assert cross_check(None, ws, dom)["status"] == "CONFLICT"


def test_agreement_unverified_missing():
    assert cross_check(None, {"number": 23}, {})["status"] == "UNVERIFIED"
    assert cross_check(None, None, {"number": 23})["status"] == "UNVERIFIED"


def test_agreement_logs_conflict(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    res = cross_check_and_log(conn, {"number": 23}, {"number": 18}, game_id="g1")
    assert res["status"] == "CONFLICT"
    events = conn.execute(
        "SELECT event_type, severity, game_id FROM integrity_events").fetchall()
    assert events[0]["event_type"] == "SOURCE_DISAGREEMENT"
    assert events[0]["severity"] == "WARNING"
    assert events[0]["game_id"] == "g1"
    conn.close()


def test_agreement_does_not_log_when_verified(tmp_path):
    conn = sqlite3.connect(tmp_path / "t2.db")
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    cross_check_and_log(conn, {"number": 23}, {"number": 23}, game_id="g1")
    assert conn.execute("SELECT COUNT(*) FROM integrity_events").fetchone()[0] == 0
    conn.close()
