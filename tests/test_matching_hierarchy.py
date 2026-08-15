"""PRD §14 — matching hierarchy.

Use the strongest identifier available first:
  1. game_id
  2. server timestamp + result identity
  3. ordered sequence position
  4. timestamp + neighbouring results

These tests pin that the walk uses the strongest identity available on BOTH
sides at each index, that game_id is the ONLY level granting repair
authority (PRD §5 — never manufacture identity), and that weaker levels
still align positions / detect discrepancies (just never repair).
"""

import pytest

from collector.reconciler import HistoryProvider, HistoryRecord, compare_windows


def canon(game_id, number, ts=None):
    return {"game_id": game_id, "number": number,
            "server_ts": ts or f"2026-08-15T00:00:{number:02d}Z"}


def rem(game_id, number, ts=None):
    return HistoryRecord(game_id=game_id, number=number,
                         server_ts=ts or f"2026-08-15T00:00:{number:02d}Z")


class _Static(HistoryProvider):
    def __init__(self, records):
        self._records = records

    def fetch_recent_history(self, limit=500):
        return self._records[:limit]

    @property
    def max_window(self):
        return len(self._records)


# ---------------------------------------------------------------------------
# Level 1 — game_id
# ---------------------------------------------------------------------------
def test_level1_game_id_matches_and_repairable():
    local = [canon("b", 2), canon("a", 1)]        # newest-first
    remote = [rem("b", 2), rem("a", 1)]
    plan = compare_windows(local, remote)
    assert plan.match_level == 1
    assert plan.repairable            # game_id -> repair authority


def test_level1_game_id_divergence_breaks_walk():
    """Same position, different game_id -> NOT a match (breaks the walk).
    With a divergent newest record there is no anchor, so match_level 0
    (no identity established) and the newest suffix is flagged."""
    local = [canon("x", 5), canon("a", 1)]
    remote = [rem("b", 5), rem("a", 1)]
    plan = compare_windows(local, remote)
    assert plan.match_level == 0      # no matched anchor (broke at i=0)
    assert not plan.repairable
    assert plan.missing or plan.extras or plan.corrections


# ---------------------------------------------------------------------------
# Level 2 — server_ts + number
# ---------------------------------------------------------------------------
def test_level2_ts_number_matches():
    """No game_id anywhere; same ts+number -> matches at level 2, and does
    NOT grant repair authority (PRD §5)."""
    local = [{"game_id": None, "number": 2, "server_ts": "t2"},
             {"game_id": None, "number": 1, "server_ts": "t1"}]
    remote = [HistoryRecord(None, 2, "t2"), HistoryRecord(None, 1, "t1")]
    plan = compare_windows(local, remote)
    assert plan.match_level == 2
    assert not plan.repairable        # ts+number never manufactures identity
    assert not plan.missing and not plan.corrections


def test_level2_ts_number_detects_wrong_value():
    """ts matches but number differs -> not a level-2 match; the suffix is
    flagged (no game_id, so detection only)."""
    local = [{"game_id": None, "number": 5, "server_ts": "t2"},
             {"game_id": None, "number": 1, "server_ts": "t1"}]
    remote = [HistoryRecord(None, 3, "t2"), HistoryRecord(None, 1, "t1")]
    plan = compare_windows(local, remote)
    assert not plan.repairable
    # the t2 record mismatches -> the newest suffix needs attention
    assert plan.missing or plan.extras or plan.corrections


# ---------------------------------------------------------------------------
# Level 3 — ordered sequence position
# ---------------------------------------------------------------------------
def test_level3_position_matches():
    """Neither game_id nor ts+number; same number at the same index
    (ordered position) -> matches at level 3."""
    local = [{"game_id": None, "number": 4, "server_ts": None},
             {"game_id": None, "number": 2, "server_ts": None}]
    remote = [HistoryRecord(None, 4, None), HistoryRecord(None, 2, None)]
    plan = compare_windows(local, remote)
    assert plan.match_level == 3
    assert not plan.repairable
    assert not plan.missing


def test_level3_position_detects_tail_extra():
    """No identity at all; position+number only. A remote tail record with
    no local counterpart is still detected (missing), never repaired."""
    # NOTE: the newest local record (4) differs from remote (3) at i=0, so
    # no level-3 match there — match_level 0 (no identity). The tail extra
    # is still DETECTED as missing (never repaired).
    local = [{"game_id": None, "number": 2, "server_ts": None},
             {"game_id": None, "number": 1, "server_ts": None}]
    remote = [HistoryRecord(None, 3, None), HistoryRecord(None, 2, None),
              HistoryRecord(None, 1, None)]
    plan = compare_windows(local, remote)
    assert plan.match_level == 0      # no identity matched
    assert len(plan.missing) == 1     # the 3 at the tail IS detected
    assert not plan.repairable


# ---------------------------------------------------------------------------
# Level 4 — timestamp + neighbouring results
# ---------------------------------------------------------------------------
def test_level4_ts_neighbours_matches():
    """Same ts but DIFFERENT number, with the same neighbouring number ->
    matches at level 4 (weakest)."""
    # t2: local 5 vs remote 3 (different number, SAME ts) + neighbour 1 matches
    local = [{"game_id": None, "number": 5, "server_ts": "t2"},
             {"game_id": None, "number": 1, "server_ts": "t1"}]
    remote = [HistoryRecord(None, 3, "t2"), HistoryRecord(None, 1, "t1")]
    plan = compare_windows(local, remote)
    # t2+neighbour 1 aligns both sides -> level 4
    assert plan.match_level == 4
    assert not plan.repairable


def test_level4_no_match_no_identity():
    """Nothing matches at any level -> match_level 0, authoritative (history
    present) but not repairable, and the whole suffix flagged."""
    local = [{"game_id": None, "number": 7, "server_ts": "t9"},
             {"game_id": None, "number": 1, "server_ts": "t1"}]
    remote = [HistoryRecord(None, 2, "t8"), HistoryRecord(None, 1, "t1")]
    plan = compare_windows(local, remote)
    assert plan.match_level == 0
    assert plan.authoritative
    assert not plan.repairable
    assert plan.missing or plan.extras


# ---------------------------------------------------------------------------
# Strongest identity available wins
# ---------------------------------------------------------------------------
def test_game_id_beats_ts_number_when_both_present():
    """When game_id is present it is used FIRST (PRD §14 priority 1), even
    if ts+number would also match."""
    local = [canon("b", 2, "t2"), canon("a", 1, "t1")]
    remote = [rem("b", 2, "t2"), rem("a", 1, "t1")]
    plan = compare_windows(local, remote)
    assert plan.match_level == 1      # game_id used, not ts+number


def test_mixed_identity_uses_strongest_per_record():
    """A mix: some records have game_id, some only ts+number. Each index
    uses the strongest available; a game_id match (level 1) grants
    authority even when other records match at weaker levels."""
    local = [canon("b", 2, "t2"),
             {"game_id": None, "number": 1, "server_ts": "t1"}]
    remote = [rem("b", 2, "t2"), HistoryRecord(None, 1, "t1")]
    plan = compare_windows(local, remote)
    assert plan.match_level == 2      # max level seen (record 2 matched ts+number)
    assert plan.repairable            # but a level-1 (game_id) match grants authority
