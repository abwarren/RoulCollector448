"""Per-spin integrity validator (Phase 2).

Every observation / canonical spin passes through validate_spin() before
acceptance. Pure, deterministic checks + event logging — NEVER mutates
canonical data (repairs are the reconciler/repairer's job, Phases 3-4).

Pipeline contract (PRD §52, §54): capture -> observation -> VALIDATE ->
canonicalize -> persist -> reconcile -> verify. This module is the
"validate" step; it flags anomalies as integrity_events and statuses, and
feeds the reconcile -> verify loop with a clean signal of what is SUSPECT.

Checks (PRD §17-19):
  * number in 0..36
  * color == num_to_color(number)   (never silently "fix" — flag)
  * game_id present / duplicate / conflicting duplicate
  * timestamps parseable, not impossible (future >5min, capture latency)
  * cadence: 0-70 normal, 70-119 suspicious, >=120 confirmed gap
  * capture latency rolling metrics: P50/P95/P99/max
"""

import datetime
import math
from collections import deque

REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}

# Cadence bands (seconds between spins) — Table 448 median ~44s, max legit ~57s
# PRD §12: normal 0-70s, suspicious 70-119s, confirmed gap >= 120s.
CADENCE_SUSPICIOUS = 70     # start of the suspicious band
CADENCE_GAP = 120           # confirmed gap (matches collector STALL_THRESHOLD_S)

MAX_FUTURE_SKEW_S = 300    # server_ts more than 5min in the future -> anomaly
MAX_CAPTURE_LATENCY_S = 120  # server->collector latency beyond this is suspicious


def num_to_color(number: int) -> str:
    """Standard roulette color: 0=Green, red set, else Black."""
    if number == 0:
        return "Green"
    return "Red" if number in REDS else "Black"


def _parse_ts(ts) -> datetime.datetime | None:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Individual checks — each returns list[str] of problems ([] = clean)
# ---------------------------------------------------------------------------
def check_number(number) -> list:
    if number is None:
        return ["number missing"]
    if not isinstance(number, int):
        return [f"number not integer: {number!r}"]
    if not 0 <= number <= 36:
        return [f"number out of range: {number}"]
    return []


def check_color(number, color) -> list:
    if number is None or not isinstance(number, int):
        return []  # number check already flags it; skip color
    if not 0 <= number <= 36:
        return []  # ditto
    expected = num_to_color(number)
    if color is None:
        return [f"color missing for {number} (expected {expected})"]
    if str(color).strip().lower() != expected.lower():
        return [f"color mismatch: {number} is {expected}, got {color!r}"]
    return []


def check_timestamp(server_ts, observed_at=None) -> list:
    """Impossible timestamp detection (PRD §18)."""
    probs = []
    st = _parse_ts(server_ts)
    if server_ts is not None and st is None:
        probs.append(f"server_ts unparseable: {server_ts!r}")
    if st is not None:
        now = datetime.datetime.now(datetime.timezone.utc)
        if st.tzinfo is None:
            st = st.replace(tzinfo=datetime.timezone.utc)
        skew = (st - now).total_seconds()
        if skew > MAX_FUTURE_SKEW_S:
            probs.append(f"server_ts {skew:.0f}s in the future")
        if skew < -86400 * 365:
            probs.append("server_ts more than a year old")
    if observed_at:
        ot = _parse_ts(observed_at)
        if ot is not None and st is not None:
            if ot.tzinfo is None:
                ot = ot.replace(tzinfo=datetime.timezone.utc)
            lat = (ot - st).total_seconds()
            if lat > MAX_CAPTURE_LATENCY_S:
                probs.append(f"capture latency {lat:.0f}s > {MAX_CAPTURE_LATENCY_S}s")
    return probs


def check_cadence(prev_ts, curr_ts) -> list:
    """Time-based gap detection (Signal A, PRD §12)."""
    if not prev_ts or not curr_ts:
        return []
    pt, ct = _parse_ts(prev_ts), _parse_ts(curr_ts)
    if pt is None or ct is None:
        return []
    if pt.tzinfo is None:
        pt = pt.replace(tzinfo=datetime.timezone.utc)
    if ct.tzinfo is None:
        ct = ct.replace(tzinfo=datetime.timezone.utc)
    delta = (ct - pt).total_seconds()
    if delta < 0:
        return [f"out-of-order timestamps: {delta:.0f}s negative delta"]
    if delta >= CADENCE_GAP:
        return [f"cadence gap {delta:.0f}s (>= {CADENCE_GAP}s)"]
    if delta >= CADENCE_SUSPICIOUS:
        return [f"cadence suspicious {delta:.0f}s ({CADENCE_SUSPICIOUS}-{CADENCE_GAP}s)"]
    return []


def cadence_class(delta_s: float) -> str:
    if delta_s >= CADENCE_GAP:
        return "GAP"
    if delta_s >= CADENCE_SUSPICIOUS:
        return "SUSPICIOUS"
    return "NORMAL"
# ---------------------------------------------------------------------------
# Full-spin validation
# ---------------------------------------------------------------------------
def validate_spin(*, number=None, color=None, game_id=None, server_ts=None,
                  observed_at=None, prev_ts=None) -> dict:
    """Validate one spin. Returns {status, problems, details}.

    status: VALID | SUSPECT | INVALID
      INVALID  — structurally impossible (bad number, bad color, bad ts)
      SUSPECT  — anomaly worth reconciling (cadence gap, latency, dup)
      VALID    — clean
    """
    problems = []
    problems += check_number(number)
    problems += check_color(number, color)
    problems += check_timestamp(server_ts, observed_at)
    problems += check_cadence(prev_ts, server_ts)

    has_invalid = any(
        p.startswith(("number", "color")) for p in problems
    )
    status = "INVALID" if has_invalid else ("SUSPECT" if problems else "VALID")
    return {"status": status, "problems": problems, "details": {}}


def classify_status(status: str) -> bool:
    """True if the status is acceptable for canonical data."""
    return status == "VALID"


# ---------------------------------------------------------------------------
# Capture-latency tracker (PRD §19): rolling P50/P95/P99/max
# ---------------------------------------------------------------------------
class LatencyTracker:
    """Rolling window of server->collector latencies with percentile stats.

    An increasing P99 is an early-warning signal that the collector is
    degrading before an outright stall.
    """

    def __init__(self, maxlen: int = 500):
        self._samples = deque(maxlen=maxlen)

    def add(self, server_ts, observed_at=None) -> float | None:
        st, ot = _parse_ts(server_ts), _parse_ts(observed_at)
        if st is None or ot is None:
            return None
        if st.tzinfo is None:
            st = st.replace(tzinfo=datetime.timezone.utc)
        if ot.tzinfo is None:
            ot = ot.replace(tzinfo=datetime.timezone.utc)
        lat = (ot - st).total_seconds()
        if lat < 0:
            return None
        self._samples.append(lat)
        return lat

    def _percentile(self, q: float) -> float | None:
        if not self._samples:
            return None
        s = sorted(self._samples)
        idx = min(len(s) - 1, math.ceil(q / 100 * len(s)) - 1)
        return s[max(0, idx)]

    def stats(self) -> dict:
        def _r(v):
            return round(v, 3) if v is not None else None
        return {
            "p50": _r(self._percentile(50)),
            "p95": _r(self._percentile(95)),
            "p99": _r(self._percentile(99)),
            "max": _r(max(self._samples)) if self._samples else None,
            "n": len(self._samples),
        }

    def reset(self):
        self._samples.clear()
