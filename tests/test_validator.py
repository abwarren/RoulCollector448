"""Phase 2 — validator: per-spin checks, cadence, latency metrics.

Every failure class from the PRD (wrong number, wrong color, bad timestamp,
out-of-order, cadence gap) is a deterministic pure check here; the reconciler
(Phase 3) consumes these statuses for the reconcile -> verify loop.
"""

import datetime

import pytest

from collector.validator import (
    LatencyTracker,
    REDS,
    cadence_class,
    check_cadence,
    check_color,
    check_game_id,
    check_number,
    check_timestamp,
    num_to_color,
    validate_spin,
)


def _ts(seconds_ago: int) -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=seconds_ago)
    ).isoformat()


# ---------------------------------------------------------------------------
# Number / color
# ---------------------------------------------------------------------------
def test_num_to_color():
    assert num_to_color(0) == "Green"
    assert num_to_color(17) == "Black"
    assert num_to_color(1) == "Red"
    assert num_to_color(36) == "Red"
    assert num_to_color(2) == "Black"


def test_check_number_range():
    assert check_number(0) == []
    assert check_number(36) == []
    assert check_number(37)[0].startswith("number out of range")
    assert check_number(-1)[0].startswith("number out of range")
    assert check_number(None)[0] == "number missing"
    assert check_number("17")[0].startswith("number not integer")


def test_check_color_mismatch_flagged_not_fixed():
    assert check_color(17, "Black") == []          # valid
    assert check_color(17, "Red")[0].startswith("color mismatch")   # PRD: flag, never silently fix
    assert check_color(0, "Black")[0].startswith("color mismatch")  # 0 is Green
    assert check_color(17, None)[0].startswith("color missing")


def test_validate_spin_invalid_on_bad_value():
    r = validate_spin(number=37, color="Red")
    assert r["status"] == "INVALID"
    r = validate_spin(number=17, color="Red")
    assert r["status"] == "INVALID"


def test_validate_spin_valid():
    r = validate_spin(number=17, color="Black",
                      game_id="g100", server_ts=_ts(10))
    assert r["status"] == "VALID"
    assert r["problems"] == []


# ---------------------------------------------------------------------------
# Game-ID integrity (PRD §4, §17 — malformed ids are the dedup_key gap)
# ---------------------------------------------------------------------------
def test_check_game_id_clean():
    assert check_game_id("g100") == []
    assert check_game_id(12345) == []


def test_check_game_id_empty_flagged():
    assert check_game_id("")[0] == "game_id empty/whitespace"
    assert check_game_id("   ")[0] == "game_id empty/whitespace"


def test_check_game_id_missing_flagged():
    assert check_game_id(None)[0] == "game_id missing"


def test_check_game_id_overlong_flagged():
    assert check_game_id("x" * 200)[0].startswith("game_id suspiciously long")


def test_validate_spin_suspect_on_empty_game_id():
    """An empty game_id makes the spin SUSPECT (identity cannot be trusted),
    never silently accepted."""
    r = validate_spin(number=17, color="Black", game_id="  ",
                      server_ts=_ts(10))
    assert r["status"] == "SUSPECT"
    assert any("game_id empty" in p for p in r["problems"])


def test_validate_spin_flags_color_contradiction():
    """PRD §17: '17 Red' is INVALID (17 is Black)."""
    r = validate_spin(number=17, color="Red", server_ts=_ts(10))
    assert r["status"] == "INVALID"
    assert any("color mismatch" in p for p in r["problems"])


def test_validate_spin_valid_17_black():
    """17 + Black is valid."""
    r = validate_spin(number=17, color="Black", game_id="g100",
                      server_ts=_ts(10))
    assert r["status"] == "VALID"


# ---------------------------------------------------------------------------
# PRD §18 — three timestamps + latencies
# ---------------------------------------------------------------------------
def test_timestamp_commit_latency():
    """observed 10s ago, committed 1s ago -> healthy (commit 9s)."""
    server = _ts(11)
    observed = _ts(10)
    committed = _ts(1)
    assert check_timestamp(server, observed, committed) == []


def test_timestamp_commit_latency_high():
    """observed 10s ago, committed 1h ago... no — committed NOW vs observed
    10 min ago -> commit latency ~600s > 300s -> flagged."""
    observed = _ts(600)
    committed = _ts(0)
    probs = check_timestamp(None, observed, committed)
    assert any("commit latency" in p for p in probs)


def test_validate_spin_suspect_on_cadence_gap():
    prev = _ts(300)
    r = validate_spin(number=17, color="Black", server_ts=_ts(10), prev_ts=prev)
    assert r["status"] == "SUSPECT"
    assert any("cadence gap" in p for p in r["problems"])


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------
def test_timestamp_unparseable():
    probs = check_timestamp("not-a-date")
    assert any("unparseable" in p for p in probs)


def test_timestamp_future():
    future = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=30)
    ).isoformat()
    probs = check_timestamp(future)
    assert any("future" in p for p in probs)


def test_timestamp_capture_latency():
    server = _ts(600)   # 10 min ago
    observed = _ts(0)
    probs = check_timestamp(server, observed)
    assert any("capture latency" in p for p in probs)


def test_timestamp_clean():
    assert check_timestamp(_ts(10), _ts(9)) == []


# ---------------------------------------------------------------------------
# Cadence (Signal A — time-based gap detection)
# ---------------------------------------------------------------------------
def test_cadence_bands():
    assert cadence_class(40) == "NORMAL"
    assert cadence_class(90) == "SUSPICIOUS"
    assert cadence_class(150) == "GAP"


def test_check_cadence_normal():
    prev, curr = _ts(60), _ts(10)
    assert check_cadence(prev, curr) == []


def test_check_cadence_suspicious():
    prev, curr = _ts(120), _ts(10)
    probs = check_cadence(prev, curr)
    assert any("suspicious" in p for p in probs)


def test_check_cadence_gap():
    prev, curr = _ts(300), _ts(10)
    probs = check_cadence(prev, curr)
    assert any("cadence gap" in p for p in probs)


def test_check_cadence_out_of_order():
    prev, curr = _ts(10), _ts(60)   # curr older than prev
    probs = check_cadence(prev, curr)
    assert any("out-of-order" in p for p in probs)


# ---------------------------------------------------------------------------
# Latency metrics (PRD §19 — rising P99 = early degradation warning)
# ---------------------------------------------------------------------------
def test_latency_tracker_empty():
    assert LatencyTracker().stats()["n"] == 0


def test_latency_tracker_percentiles():
    tr = LatencyTracker()
    base = datetime.datetime.now(datetime.timezone.utc)
    for i in range(100):
        st = (base - datetime.timedelta(seconds=100 - i)).isoformat()
        ot = (base - datetime.timedelta(seconds=100 - i) + datetime.timedelta(seconds=i % 10)).isoformat()
        tr.add(st, ot)
    s = tr.stats()
    assert s["n"] == 100
    assert 0 <= s["p50"] <= 10
    assert s["max"] >= s["p99"] >= s["p95"] >= s["p50"]


def test_latency_tracker_ignores_negative():
    tr = LatencyTracker()
    base = datetime.datetime.now(datetime.timezone.utc)
    # observed_at BEFORE server_ts => negative latency, must be ignored
    assert tr.add((base - datetime.timedelta(seconds=10)).isoformat(),
                  (base - datetime.timedelta(seconds=15)).isoformat()) is None
    assert tr.stats()["n"] == 0


def test_latency_tracker_commit_latency():
    tr = LatencyTracker()
    base = datetime.datetime.now(datetime.timezone.utc)
    for i in range(10):
        st = (base - datetime.timedelta(seconds=20 - i)).isoformat()
        ot = (base - datetime.timedelta(seconds=10 - i)).isoformat()
        ct = (base - datetime.timedelta(seconds=9 - i)).isoformat()
        tr.add(st, ot, ct)
    s = tr.stats()
    assert s["n"] == 10
    assert s["commit_n"] == 10
    assert 0 <= s["commit_p50"] <= 10
    assert s["commit_max"] >= s["commit_p99"] >= s["commit_p95"] >= s["commit_p50"]
