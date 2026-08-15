"""Phase 3 — reconciler: the reconcile -> verify loop (PRD §13, §53).

Scenario coverage:
  * silent WS miss (3 spins) -> reconcile finds them -> plan backfills
  * misaligned suffix (wrong value at position N shifts everything)
  * duplicate / conflicting game_id detection
  * authoritative source unavailable -> UNVERIFIED, never guessed
  * weak identity (no game_id) matching by ts+number / position
"""

import pytest

from collector.reconciler import (
    HistoryProvider,
    HistoryRecord,
    RepairPlan,
    compare_windows,
    duplicate_incidents,
    normalize_record,
    reconcile,
)


def canon(game_id, number, ts=None):
    return {"game_id": game_id, "number": number, "server_ts": ts or f"2026-08-15T00:00:{number:02d}Z"}


def rem(game_id, number, ts=None, order=None):
    return HistoryRecord(game_id=game_id, number=number, server_ts=ts or f"2026-08-15T00:00:{number:02d}Z", order_hint=order)


# ---------------------------------------------------------------------------
# compare_windows — the core walk
# ---------------------------------------------------------------------------
def test_identical_windows_verify():
    local = [canon("a", 1), canon("b", 2), canon("c", 3)]
    remote = [rem("c", 3), rem("b", 2), rem("a", 1)]  # newest-first
    plan = compare_windows(local, remote)
    assert plan.authoritative
    assert not plan.missing
    assert not plan.corrections
    assert not plan.duplicates
    assert not plan.reorder


def test_silent_ws_miss_3_spins():
    """PRD §53: WS silently misses 3 spins mid-window."""
    # local: newest-first [c, b, a] (real capture missed x, y, z)
    local = [canon("c", 3), canon("b", 2), canon("a", 1)]
    # remote: authoritative history has 3 MORE spins at the tail
    remote = [rem("f", 6), rem("e", 5), rem("d", 4),
              rem("c", 3), rem("b", 2), rem("a", 1)]
    plan = compare_windows(local, remote)
    assert plan.authoritative
    # newest-first walk: the 3 newest spins (f,e,d) are the missed ones
    assert [r.game_id for r in plan.missing] == ["f", "e", "d"]
    assert not plan.corrections
    assert plan.window_achieved == 6


def test_wrong_value_then_alignment():
    """PRD §15: local 5004=9 but authoritative says 5004=0, 5005=9, 5006=32."""
    local = [canon("s5", 32), canon("s4", 9), canon("s3", 21)]
    remote = [rem("s6", 32), rem("s5", 9), rem("s4", 0), rem("s3", 21)]
    plan = compare_windows(local, remote)
    # s5 and s4 exist locally; s5=32 vs remote s6=32 mismatch by position
    assert plan.missing  # s6
    assert plan.corrections  # s5 corrected to 9, s4 to 0
    assert any(c[0] == "s4" and c[2] == 0 for c in plan.corrections)


def test_conflicting_duplicate_detected():
    """PRD §16: same game_id, different number -> CONFLICT (CRITICAL), never
    silently overwrite. Canonical UNIQUE normally prevents this, so it's the
    legacy/malformed-data scenario."""
    local = [canon("g", 17), canon("b", 2), canon("g", 23)]  # dup g with different number
    remote = [rem("g", 17), rem("b", 2)]
    plan = compare_windows(local, remote)
    assert "g" in plan.duplicates
    assert plan.duplicate_kinds["g"] == "CONFLICT"
    incs = duplicate_incidents(plan)
    assert incs and incs[0]["kind"] == "CONFLICT"
    assert incs[0]["severity"] == "CRITICAL"


def test_identical_duplicate_detected():
    """PRD §16: game_id + number + timestamp ALL identical -> EXACT duplicate
    (WARNING), collapsible."""
    local = [canon("g", 17), canon("g", 17), canon("b", 2)]  # g twice, same everything
    remote = [rem("g", 17), rem("b", 2)]
    plan = compare_windows(local, remote)
    assert "g" in plan.duplicates
    assert plan.duplicate_kinds["g"] == "EXACT"
    incs = duplicate_incidents(plan)
    assert incs and incs[0]["severity"] == "WARNING"


def test_ts_mismatch_duplicate():
    """Same game_id AND number but DIFFERENT server_ts -> TS_MISMATCH (the
    identity is duplicated with an inconsistent timestamp — flagged, never
    silently resolved)."""
    local = [canon("g", 17, "t1"), canon("g", 17, "t2"), canon("b", 2)]
    remote = [rem("g", 17), rem("b", 2)]
    plan = compare_windows(local, remote)
    assert "g" in plan.duplicates
    assert plan.duplicate_kinds["g"] == "TS_MISMATCH"
    incs = duplicate_incidents(plan)
    assert incs and incs[0]["severity"] == "WARNING"


def test_duplicate_incidents_pure():
    """duplicate_incidents is deterministic and never raises on an empty plan."""
    assert duplicate_incidents(RepairPlan()) == []


def test_duplicate_with_authority_corrects_both():
    """A duplicate game_id where remote says a DIFFERENT number — both local
    rows get corrections (the duplicate detector and value corrections
    coexist; collapse + correct is the full §16 repair)."""
    local = [canon("g", 17), canon("g", 17), canon("b", 2)]
    remote = [rem("g", 23), rem("b", 2)]  # authoritative: g=23
    plan = compare_windows(local, remote)
    assert "g" in plan.duplicates
    assert ("g", 17, 23) in plan.corrections


def test_no_authority_unverified():
    """PRD §25: authoritative unavailable -> UNVERIFIED, never guess."""
    local = [canon("a", 1), canon("b", 2)]

    class EmptyProvider(HistoryProvider):
        def fetch_recent_history(self, limit=500):
            return []

    result = reconcile(local, EmptyProvider())
    assert not result.ok
    assert not result.plan.authoritative
    assert "UNVERIFIED" in result.message


def test_provider_failure_unverified():
    local = [canon("a", 1)]

    class BrokenProvider(HistoryProvider):
        def fetch_recent_history(self, limit=500):
            raise RuntimeError("network down")

    result = reconcile(local, BrokenProvider())
    assert not result.ok
    assert "history unavailable" in result.message


# ---------------------------------------------------------------------------
# reconcile() — full pass with a deterministic provider
# ---------------------------------------------------------------------------
class FixtureProvider(HistoryProvider):
    def __init__(self, records):
        self._records = records

    def fetch_recent_history(self, limit=500):
        return self._records[:limit]

    @property
    def max_window(self):
        return len(self._records)


def test_reconcile_full_pass_missing():
    local = [canon("a", 1), canon("b", 2), canon("c", 3)]  # oldest-first
    provider = FixtureProvider([
        rem("f", 6), rem("e", 5), rem("d", 4),
        rem("c", 3), rem("b", 2), rem("a", 1),
    ])
    result = reconcile(local, provider, window=10)
    assert result.missing_count == 3
    assert result.verified_count == 3
    assert not result.ok          # repair plan needed
    assert result.plan.window_achieved == 6


def test_reconcile_clean_verifies():
    local = [canon("a", 1), canon("b", 2), canon("c", 3)]
    provider = FixtureProvider([
        rem("c", 3), rem("b", 2), rem("a", 1),
    ])
    result = reconcile(local, provider, window=10)
    assert result.ok
    assert result.missing_count == 0
    assert result.verified_count == 3
    assert result.message == "verified"


def test_normalize_record_coerces_dicts():
    rec = normalize_record({"game_id": "x", "number": "7", "server_ts": "t"})
    assert rec.game_id == "x"
    assert rec.number == 7


def test_weak_identity_matches_by_position():
    """No game_ids on either side — position + number must align."""
    local = [canon(None, 1), canon(None, 2), canon(None, 3)]
    remote = [rem(None, 4), rem(None, 3), rem(None, 2), rem(None, 1)]
    plan = compare_windows(local, remote)
    assert plan.authoritative
    assert len(plan.missing) == 1       # the 4 at the tail
    assert not plan.corrections
