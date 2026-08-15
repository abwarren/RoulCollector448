"""Phase 3 — history.py: DOM history parsing + providers.

The site's rolling history is the recovery buffer; these tests pin the
parsing and provider contract so the reconciler can be fed real data.
"""

import pytest

from collector.history import (
    DOMHistoryProvider,
    StaticHistoryProvider,
    parse_history_text,
)
from collector.reconciler import HistoryRecord, reconcile


# ---------------------------------------------------------------------------
# parse_history_text
# ---------------------------------------------------------------------------
def test_parse_chip_run():
    text = "12 34 5 0 17"
    recs = parse_history_text(text)
    # document order 12,34,5,0,17 -> newest-first = 17,0,5,34,12
    assert [r.number for r in recs] == [17, 0, 5, 34, 12]
    assert all(r.game_id is None for r in recs)


def test_parse_number_color_rows():
    text = "12 Red\n34 Black\n0 Green"
    recs = parse_history_text(text)
    assert [r.number for r in recs] == [0, 34, 12]  # newest-first


def test_parse_empty():
    assert parse_history_text("") == []
    assert parse_history_text("No results yet") == []


def test_parse_mixed_noise():
    text = "History: 5, 7, 12"   # label + trailing chip run
    recs = parse_history_text(text)
    assert [r.number for r in recs] == [12, 7, 5]  # newest-first


def test_static_provider():
    recs = [HistoryRecord(game_id="a", number=1),
            HistoryRecord(game_id="b", number=2)]
    p = StaticHistoryProvider(recs)
    assert p.max_window == 2
    got = p.fetch_recent_history(limit=1)
    assert len(got) == 1
    assert got[0].game_id == "a"


def test_dom_provider_is_async_only():
    p = DOMHistoryProvider(page=None)
    with pytest.raises(RuntimeError):
        p.fetch_recent_history()


# ---------------------------------------------------------------------------
# reconcile() end-to-end with a static provider (no browser)
# ---------------------------------------------------------------------------
def test_reconcile_with_static_provider_finds_missing():
    local = [{"game_id": "a", "number": 1, "server_ts": "t1"},
             {"game_id": "b", "number": 2, "server_ts": "t2"}]
    provider = StaticHistoryProvider([
        HistoryRecord(game_id="d", number=4),
        HistoryRecord(game_id="c", number=3),
        HistoryRecord(game_id="b", number=2),
        HistoryRecord(game_id="a", number=1),
    ])
    result = reconcile(local, provider, window=10)
    assert result.missing_count == 2
    assert {r.game_id for r in result.plan.missing} == {"c", "d"}


def test_reconcile_verify_clean():
    local = [{"game_id": "a", "number": 1, "server_ts": "t1"},
             {"game_id": "b", "number": 2, "server_ts": "t2"}]
    provider = StaticHistoryProvider([
        HistoryRecord(game_id="b", number=2),
        HistoryRecord(game_id="a", number=1),
    ])
    result = reconcile(local, provider, window=10)
    assert result.ok
    assert result.message == "verified"
