"""Integrity state machine + telemetry (Phase 5).

Separates PROCESS health from DATA health (PRD §21): a browser can be
perfectly alive while the dataset is broken. States:

    HEALTHY -> SUSPECT -> DEGRADED -> RECONCILING
                                        |-- success -> HEALTHY
                                        `-- failure -> RECOVERING -> RECONCILING

Data-health score (PRD §22) with configurable weights; capture telemetry
counters (PRD §41) that make failures measurable rather than anecdotal.
"""

import os
import time
from dataclasses import dataclass, field


class IntegrityState:
    HEALTHY = "HEALTHY"
    SUSPECT = "SUSPECT"
    DEGRADED = "DEGRADED"
    RECONCILING = "RECONCILING"
    RECOVERING = "RECOVERING"


# PRD §22 weights — configurable via env overrides
DEFAULT_WEIGHTS = {
    "freshness": 0.20,
    "sequence": 0.25,
    "reconciliation": 0.25,
    "duplicates": 0.10,
    "timestamps": 0.10,
    "source_agreement": 0.10,
}

# Score bands (PRD §22) — the exact thresholds are CONFIGURABLE via env:
#   RC_SCORE_VERIFIED / RC_SCORE_HEALTHY / RC_SCORE_DEGRADED / RC_SCORE_WARNING
# Defaults match the PRD: 100-98 VERIFIED, 97-95 HEALTHY, 94-90 DEGRADED,
# 89-75 WARNING, <75 CRITICAL.
DEFAULT_SCORE_BANDS = [   # (min_score, label)
    (98, "VERIFIED"),
    (95, "HEALTHY"),
    (90, "DEGRADED"),
    (75, "WARNING"),
    (0, "CRITICAL"),
]

_BAND_ENV = [
    ("RC_SCORE_VERIFIED", "VERIFIED"),
    ("RC_SCORE_HEALTHY", "HEALTHY"),
    ("RC_SCORE_DEGRADED", "DEGRADED"),
    ("RC_SCORE_WARNING", "WARNING"),
]


def score_bands() -> list:
    """Resolve the score-band thresholds, honouring env overrides. Parsed
    per call so a config change applies without restart; invalid env values
    fall back to defaults (never crash the health score). Un-overridden
    bands keep their defaults — an override replaces only that threshold."""
    bands = {label: mn for mn, label in DEFAULT_SCORE_BANDS}
    for var, label in _BAND_ENV:
        raw = os.environ.get(var, "").strip()
        if raw:
            try:
                bands[label] = int(raw)
            except ValueError:
                pass
    ordered = sorted(
        ((bands[label], label) for _, label in _BAND_ENV),
        key=lambda t: -t[0],
    )
    return ordered + [(0, "CRITICAL")]


def score_band(score: float) -> str:
    for min_score, label in score_bands():
        if score >= min_score:
            return label
    return "CRITICAL"


class DataHealthScore:
    """Component-weighted 0-100 score (PRD §22). Each component is a 0-1
    ratio; the score is the weighted sum scaled to 100."""

    def __init__(self, weights: dict | None = None):
        self.weights = weights or DEFAULT_WEIGHTS

    def compute(self, *, freshness=1.0, sequence=1.0, reconciliation=1.0,
                duplicates=1.0, timestamps=1.0, source_agreement=1.0) -> float:
        comps = {
            "freshness": freshness,
            "sequence": sequence,
            "reconciliation": reconciliation,
            "duplicates": duplicates,
            "timestamps": timestamps,
            "source_agreement": source_agreement,
        }
        return round(100 * sum(self.weights[k] * min(1.0, max(0.0, comps[k]))
                               for k in self.weights), 1)

    def band(self, score: float) -> str:
        return score_band(score)


class Telemetry:
    """Capture counters (PRD §41) — makes failures measurable."""

    def __init__(self):
        self.counters = {
            "ws_frames_received": 0,
            "ws_spin_candidates": 0,
            "ws_spins_accepted": 0,
            "ws_invalid_frames": 0,
            "dom_polls": 0,
            "dom_candidates": 0,
            "dom_spins_detected": 0,
            "duplicates": 0,
            "conflicts": 0,
            "repairs": 0,
            "reconciliations": 0,
            "reconciliation_failures": 0,
        }

    def inc(self, name: str, n: int = 1):
        if name in self.counters:
            self.counters[name] += n

    def snapshot(self) -> dict:
        return dict(self.counters)

    def merge(self, other: dict):
        for k, v in other.items():
            if k in self.counters:
                self.counters[k] += v


class RecoveryStateMachine:
    """Tracks data-health state independently of process health (PRD §21)."""

    def __init__(self):
        self.state = IntegrityState.HEALTHY
        self.last_transition = time.time()
        self._history = []

    def _transition(self, new_state: str, reason: str):
        if new_state != self.state:
            self._history.append((self.state, new_state, reason, time.time()))
            self.state = new_state
            self.last_transition = time.time()

    def observe(self, *, reconciled_ok: bool, repairing: bool = False,
                recovering: bool = False) -> str:
        """Feed one observation cycle; returns the resulting state."""
        if recovering:
            self._transition(IntegrityState.RECOVERING, "browser/session recovery")
        elif repairing:
            self._transition(IntegrityState.RECONCILING, "repair in progress")
        elif reconciled_ok:
            self._transition(IntegrityState.HEALTHY, "reconciliation verified")
        else:
            self._transition(IntegrityState.DEGRADED, "reconciliation failed")
        return self.state

    def history(self) -> list:
        return list(self._history)
