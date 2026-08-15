"""HistoryProvider (Phase 3) — authoritative recent history from the site.

The rolling ~500-result history is the recovery buffer the whole
self-healing design hangs on (PRD §3). In practice the DOM history panel
currently exposes ~25 recent results; this module probes for whatever
history surface exists and reports `max_window` honestly so the dashboard
never overclaims (proposal §3: effective_window = min(500, capacity)).

Probe order (best first):
  1. history strip containers (Evolution roulette UIs render a recent
     results bar: [class*="history"], [class*="last-result"], ...)
  2. any element whose text is a run of numbers 0-36 with the roulette
     color classes (Red/Black/Green) — a strong signal of result history
  3. fall back to the current-result element (window = 1) rather than
     inventing anything

The provider is intentionally pure-DOM and synchronous-safe: it is called
from the collector's background reconcile task through the same 15s cdp()
timeout wrapper as everything else, so a hung page can never freeze capture
(PRD §47 — the 26-minute CDP freeze is the cautionary tale).
"""

import re

from collector.reconciler import HistoryProvider, HistoryRecord

_NUM_RE = re.compile(r"\b([0-3]?[0-9])\b")   # 0-36 candidate
_RANGE = set(range(37))

# Evolution-style history containers, best first
HISTORY_SELECTORS = [
    '[class*="history-item"]',
    '[class*="history-item"] [class*="number"]',
    '[class*="history"] [class*="number"]',
    '[class*="last-results"] [class*="number"]',
    '[class*="previous-results"] [class*="number"]',
    '[class*="recent-results"] [class*="number"]',
    '[class*="result-history"] [class*="number"]',
    '[data-testid*="history"] [class*="number"]',
]

# Fallback: containers that hold a run of result chips
CONTAINER_SELECTORS = [
    '[class*="history"]',
    '[class*="last-results"]',
    '[class*="previous-results"]',
    '[class*="recent-results"]',
    '[class*="result-history"]',
    '[class*="results-history"]',
    '[data-testid*="history"]',
]


def parse_history_text(text: str) -> list[HistoryRecord]:
    """Parse a DOM text blob into newest-first HistoryRecords.

    Handles both chip text ("12 34 5 0") and richer rows
    ("12 Red", "34 Black", "0 Green") — numbers only, in document order.
    Document order in a history bar is usually oldest->newest (left->right),
    so the result is reversed to newest-first for the reconciler.

    Returns [] when nothing parseable — the caller decides how to degrade.
    """
    if not text:
        return []
    recs = []
    for line in re.split(r"[\n\r;|]+", text):
        line = line.strip()
        if not line:
            continue
        nums = [int(m) for m in _NUM_RE.findall(line) if int(m) in _RANGE]
        if not nums:
            continue
        # Find the maximal trailing run of pure numbers ("History: 5, 7, 12"
        # -> 5,7,12 as chips; "Bet 12" -> just 12; "12 34 5 0 17" -> all five).
        tokens = re.split(r"[\s,]+", line)
        start = len(tokens)
        for i in range(len(tokens) - 1, -1, -1):
            if tokens[i].strip().isdigit():
                start = i
            else:
                break
        if start < len(tokens):
            trailing = [int(t) for t in tokens[start:] if t.strip().isdigit()]
            recs.extend(HistoryRecord(game_id=None, number=n) for n in trailing
                        if n in _RANGE)
        else:
            recs.append(HistoryRecord(game_id=None, number=nums[-1]))
    if not recs:
        return []
    recs.reverse()          # oldest->newest -> newest-first for reconciler
    return recs


class DOMHistoryProvider(HistoryProvider):
    """Scrape recent results from the live page via Playwright selectors."""

    def __init__(self, page, max_window: int = 500):
        self._page = page
        self._max_window = max_window
        self._last_fetch: list[HistoryRecord] = []

    @property
    def max_window(self) -> int:
        return self._max_window

    async def fetch_recent_history_async(self, limit: int = 500) -> list[HistoryRecord]:
        """Async fetch — Playwright is async; the reconciler wraps this via
        asyncio in the collector task."""
        limit = min(limit, self._max_window)
        frames = [self._page] + list(self._page.frames)
        for sel in HISTORY_SELECTORS:
            try:
                for f in frames:
                    els = await f.query_selector_all(sel)
                    if not els:
                        continue
                    texts = [((await el.inner_text()) or "").strip()
                             for el in els[:limit]]
                    joined = " | ".join(t for t in texts if t)
                    recs = parse_history_text(joined)
                    if recs:
                        self._last_fetch = recs[:limit]
                        return self._last_fetch
            except Exception:
                continue
        # container-level fallback: one element whose text is a run of chips
        for sel in CONTAINER_SELECTORS:
            try:
                for f in frames:
                    el = await f.query_selector(sel)
                    if not el:
                        continue
                    text = (await el.inner_text()) or ""
                    recs = parse_history_text(text)
                    if recs:
                        self._last_fetch = recs[:limit]
                        return self._last_fetch
            except Exception:
                continue
        self._last_fetch = []
        return []

    # HistoryProvider sync interface — the collector task runs this in the
    # event loop via asyncio.run_coroutine_threadsafe if ever needed off-loop.
    def fetch_recent_history(self, limit: int = 500) -> list[HistoryRecord]:
        raise RuntimeError(
            "DOMHistoryProvider is async — use fetch_recent_history_async() "
            "inside the collector's asyncio task"
        )


class StaticHistoryProvider(HistoryProvider):
    """Deterministic provider for tests and dry-runs (no browser needed)."""

    def __init__(self, records: list[HistoryRecord]):
        self._records = records
        self._max_window = len(records)

    @property
    def max_window(self) -> int:
        return self._max_window

    def fetch_recent_history(self, limit: int = 500) -> list[HistoryRecord]:
        return self._records[:limit]
