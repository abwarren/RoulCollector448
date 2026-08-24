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

import datetime
import os
import re
import sqlite3

from collector import schema
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


# ---------------------------------------------------------------------------
# Signal C: authoritative recent history from WebSocket frames
# ---------------------------------------------------------------------------
# Evolution game clients push history-shaped payloads on join (snapshot) and
# periodically ("history", "lastResults", "results", "records" — lists of
# {gameId, number/value, timestamp}). Unlike DOM text (numbers only), these
# entries carry game_id + server_ts — the identity the reconciler needs for
# repair authority (PRD §14 identity hierarchy, §5 observed > inferred).

_NUMBER_KEYS = ("number", "value", "winNumber", "win_number", "result")
_WRAP_KEYS = ("args", "data", "payload", "body")


def _extract_number(entry: dict):
    for k in _NUMBER_KEYS:
        v = entry.get(k)
        if isinstance(v, bool) or v is None:
            continue
        try:
            n = int(v)
            if 0 <= n <= 36:
                return n
        except (TypeError, ValueError):
            continue
    return None


def _ts_epoch(s) -> float | None:
    """Parse ISO-8601 or epoch (s/ms) timestamps to epoch seconds."""
    if not s:
        return None
    s = str(s)
    try:
        f = float(s)
        return f if f < 1e12 else f / 1000.0
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(
            s.replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        return None


def parse_history_frame(payload: dict) -> list[HistoryRecord] | None:
    """Parse a WS frame carrying a list of past results.

    Returns newest-first HistoryRecords, or None when the frame carries no
    history-shaped list. game_id + server_ts are kept when present (the
    entries that give the reconciler repair authority, Signal C + B).
    Frame order: assumed newest-first; reversed only when the first two
    entries' timestamps prove ascending (oldest-first source).
    """
    if not isinstance(payload, dict):
        return None
    best: list[HistoryRecord] | None = None

    def consider(candidates):
        nonlocal best
        recs = []
        for i, entry in enumerate(candidates):
            if not isinstance(entry, dict):
                continue
            n = _extract_number(entry)
            if n is None:
                continue
            gid = entry.get("gameId") or entry.get("game_id")
            ts = (entry.get("timestamp") or entry.get("time")
                  or entry.get("serverTime") or entry.get("server_ts"))
            recs.append(HistoryRecord(
                game_id=str(gid) if gid else None,
                number=n,
                server_ts=str(ts) if ts else None,
                order_hint=i,
            ))
        if recs and (best is None or len(recs) > len(best)):
            best = recs

    for k, v in payload.items():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v[:5]):
            consider(v)
    for wrap in _WRAP_KEYS:
        w = payload.get(wrap)
        if isinstance(w, dict):
            for k, v in w.items():
                if isinstance(v, list) and v and all(isinstance(x, dict) for x in v[:5]):
                    consider(v)
    if best is None:
        return None

    # order: newest-first by timestamp when provable
    ts0 = _ts_epoch(best[0].server_ts)
    ts1 = _ts_epoch(best[1].server_ts) if len(best) > 1 else None
    if ts0 is not None and ts1 is not None and ts0 < ts1:
        best.reverse()
    return best


def parse_lobby_history(payload: dict) -> list[HistoryRecord] | None:
    """Parse a `lobby.historyUpdated` WS frame (Evolution lobby stream).

    New format (2026-08): the game iframe shows the LOBBY (all roulette
    tables), and every table's recent results arrive in one frame:
      {"type":"lobby.historyUpdated","args":{
         "<table_key>":{"results":[[{"number":"21"}],[{"number":"19"}],...]}, ...}}
    results is newest-first; each result is a list (multi-ball tables have
    >1 entry). Returns the newest spin per table (game_id synthesized as
    lobby-<table_key>-<number> for dedup; no server_ts in this stream).
    None when the frame carries no lobby-history shape.
    """
    if not isinstance(payload, dict):
        return None
    args = payload.get("args")
    if not isinstance(args, dict):
        return None
    # quick reject: not a per-table results map
    if not any(isinstance(v, dict) and isinstance(v.get("results"), list)
               for v in args.values()):
        return None
    recs = []
    for table_key, val in (args or {}).items():
        if not isinstance(val, dict):
            continue
        results = val.get("results")
        if not isinstance(results, list) or not results:
            continue
        newest = results[0]
        if isinstance(newest, list):
            entry = newest[0] if newest else None
        elif isinstance(newest, dict):
            entry = newest
        else:
            entry = None
        n = _extract_number(entry) if isinstance(entry, dict) else None
        if n is None:
            continue
        recs.append(HistoryRecord(
            game_id=f"lobby-{table_key}-{n}",
            number=n,
            server_ts=None,
            order_hint=0,
        ))
    return recs or None


def parse_lobby_tail(payload: dict, table_key: str) -> list[HistoryRecord] | None:
    """Parse the FULL recent-results tail for ONE table from a lobby frame.

    Same frame shape as parse_lobby_history, but returns every entry in
    `results` (newest-first) instead of only results[0]. This is the
    backlog authority the reconciler needs to backfill gaps: when the
    stream misses spins, the tail carries the last ~10 results, and the
    missing numbers can be reconstructed by diffing tail vs DB.

    Returns None when the frame carries no results for `table_key`.
    """
    if not isinstance(payload, dict):
        return None
    args = payload.get("args")
    if not isinstance(args, dict):
        return None
    val = args.get(table_key)
    if not isinstance(val, dict):
        return None
    results = val.get("results")
    if not isinstance(results, list) or not results:
        return None
    recs = []
    for pos, result in enumerate(results):
        if isinstance(result, list):
            entry = result[0] if result else None
        elif isinstance(result, dict):
            entry = result
        else:
            entry = None
        n = _extract_number(entry) if isinstance(entry, dict) else None
        if n is None:
            continue
        recs.append(HistoryRecord(
            game_id=f"lobby-{table_key}-{n}",
            number=n,
            server_ts=None,
            order_hint=pos,  # 0 = newest
        ))
    return recs or None


class WSHistoryProvider(HistoryProvider):
    """History buffered from captured WebSocket frames (join snapshot /
    periodic history payloads), accumulated by the collector's CDP frame
    handler. Entries carry game_id + server_ts — repair authority, unlike
    DOM text. `max_window` honestly reflects what was actually buffered.
    """

    def __init__(self, records: list[HistoryRecord]):
        self._records = list(records)

    @property
    def max_window(self) -> int:
        return len(self._records)

    def fetch_recent_history(self, limit: int = 500) -> list[HistoryRecord]:
        return self._records[:limit]


class DBHistoryProvider(HistoryProvider):
    """Durable Signal C provider: history observations persisted to
    spin_observations (source='history') by the collector.

    The in-memory WS ring buffer dies on restart; this provider reloads the
    persisted history so reconciliation has repair authority from boot,
    across sessions. Rows are deduped by game_id (same spin seen in
    overlapping snapshots appears once) and returned newest-first.
    """

    def __init__(self, conn=None, max_records: int = 2000):
        self._conn = conn          # optional test connection
        self._max_records = max_records

    def _rows(self):
        if self._conn is not None:
            return self._conn.execute(
                "SELECT game_id, number, server_ts FROM spin_observations "
                "WHERE source='history' AND game_id IS NOT NULL "
                "ORDER BY observed_at DESC LIMIT ?",
                (self._max_records,),
            ).fetchall()
        # No test connection: open our own READ-ONLY connection, resolving
        # the DB path at call time (schema.DB_PATH is frozen at import; the
        # collector sets RC_DB_PATH before its integrity-layer import, but a
        # provider must work regardless of import order).
        path = os.environ.get("RC_DB_PATH") or schema.default_db_path()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT game_id, number, server_ts FROM spin_observations "
                "WHERE source='history' AND game_id IS NOT NULL "
                "ORDER BY observed_at DESC LIMIT ?",
                (self._max_records,),
            ).fetchall()
        finally:
            conn.close()

    def fetch_recent_history(self, limit: int = 500) -> list[HistoryRecord]:
        seen = {}
        for r in self._rows():
            gid = r["game_id"]
            if gid in seen:
                continue
            seen[gid] = HistoryRecord(game_id=gid, number=r["number"],
                                      server_ts=r["server_ts"])
            if len(seen) >= limit:
                break
        recs = sorted(seen.values(), key=lambda r: r.server_ts or "", reverse=True)
        return recs[:limit]

    @property
    def max_window(self) -> int:
        return len(self._rows())
