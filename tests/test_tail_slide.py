"""Unit tests for tail_slide — the burst-robust new-spin detector.

Regression for the 2026-08-25 counter-inflation incident: the old per-slot
check (pos i or i-1) failed on recovery bursts, inflating the gid counter
(+13) and corrupting the canonical tail via backfill.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector.roulette2_collector import tail_slide  # noqa: E402


def test_first_frame_all_new():
    assert tail_slide([17, 2, 31], []) == 3


def test_steady_state_one_new():
    # prev: [32, 17, 2]; new spin 5 arrives -> [5, 32, 17, 2]
    assert tail_slide([5, 32, 17, 2], [32, 17, 2]) == 1


def test_pure_relisting_no_new():
    # frame with no new spin: identical tail
    assert tail_slide([32, 17, 2], [32, 17, 2]) == 0


def test_burst_of_five():
    # recovery burst: 5 new spins land in one frame, old tail slides down 5
    prev = [32, 17, 2, 31, 9, 8, 6, 21, 13, 23]
    cur = [0, 10, 26, 4, 33, 32, 17, 2, 31, 9]  # 5 new + prev shifted
    assert tail_slide(cur, prev) == 5


def test_rolloff_gap_exceeds_tail():
    # previous newest gone entirely -> whole current tail is new
    assert tail_slide([1, 2, 3], [99, 98, 97]) == 3


def test_drift_anchor_prev_slot_one():
    # prev newest replaced but prev[1] re-appears at pos 0 -> 1 new
    assert tail_slide([17, 2, 31], [32, 17, 2]) == 1


def test_repeated_number_relisting():
    # prev newest 8 repeats (8 Black twice in a row); current tail starts 8
    # again -> treated as re-listing (under-capture is recoverable via
    # backfill; duplication is worse)
    assert tail_slide([8, 8, 32, 17], [8, 32, 17]) == 0
