"""Cross-source agreement (Phase 5, PRD §20).

Instead of treating DOM as merely a fallback, use it as a SECONDARY
verification channel: when websocket and DOM both observed the same game_id,
they should agree. Agreement -> VERIFIED; disagreement -> SOURCE_DISAGREEMENT
event and the spin is never silently resolved (PRD §20/§25 — multiple
conflicting sources => no auto-repair, surface instead).
"""

from datetime import datetime

from collector.observer import log_event

#: Max |Δt| between a DOM observation and the websocket observation of the
#: same spin for them to be treated as the same spin. DOM observations carry
#: no game_id, so time proximity is the only identity link available.
AGREEMENT_WINDOW_S = 3.0


def cross_check(conn, ws_obs, dom_obs) -> dict:
    """Compare a websocket observation against a DOM observation for the same
    game_id. Returns {status: VERIFIED|CONFLICT|UNVERIFIED, detail}.

    ws_obs / dom_obs: dicts with number (+ optional game_id/server_ts).
    """
    if not ws_obs or not dom_obs:
        return {"status": "UNVERIFIED", "detail": "missing observation"}
    ws_num = ws_obs.get("number")
    dom_num = dom_obs.get("number")
    if ws_num is None or dom_num is None:
        return {"status": "UNVERIFIED", "detail": "number missing on one side"}
    if ws_num == dom_num:
        return {"status": "VERIFIED", "detail": f"WS={ws_num} DOM={dom_num} agree"}
    return {"status": "CONFLICT", "detail": f"WS={ws_num} DOM={dom_num} disagree"}


def cross_check_and_log(conn, ws_obs, dom_obs, game_id=None) -> dict:
    """cross_check + log SOURCE_DISAGREEMENT events (no silent resolution)."""
    res = cross_check(conn, ws_obs, dom_obs)
    if res["status"] == "CONFLICT":
        log_event(
            conn, "SOURCE_DISAGREEMENT", severity="WARNING",
            game_id=game_id,
            details={"ws": ws_obs.get("number"),
                     "dom": dom_obs.get("number"),
                     "detail": res["detail"]},
            root_cause="DATA_INTEGRITY",
        )
    return res


def _iso_epoch(iso: str) -> float:
    """ISO-8601 observed_at -> epoch seconds (handles 'Z' and '+00:00')."""
    if not iso:
        return 0.0
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return datetime.fromisoformat(iso).timestamp()


def _rows_to_dicts(cur):
    """sqlite3 cursor -> list of dicts (works with or without Row factory)."""
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def verify_recent_agreement(conn, window: int = 50) -> dict:
    """Per-spin integrity check (PRD §20): WS-vs-DOM agreement over the most
    recent observations.

    Pulls the last `window` spin_observations that carry a number from the
    websocket and dom sources (newest first). A DOM observation has no
    game_id, so it is paired with the websocket observation of the same
    spin by time proximity: same spin iff |observed_at(dom) - observed_at(ws)|
    <= AGREEMENT_WINDOW_S. Every pair is run through cross_check_and_log, so
    disagreements surface as SOURCE_DISAGREEMENT events — never silently
    resolved (PRD §20).

    A DOM observation with no websocket observation within the window is
    UNVERIFIED: absence of the second source is not a contradiction, so it
    is NOT a conflict and logs nothing.

    Returns {"checked", "verified", "conflicts", "agreement_ratio"}:
      checked         DOM obs that found a WS pair (verified + conflicts)
      verified        pairs where both sources agree on the number
      conflicts       pairs that disagree (SOURCE_DISAGREEMENT logged)
      agreement_ratio verified / checked (0.0 when nothing was checked)
    """
    rows = _rows_to_dicts(conn.execute(
        "SELECT id, observed_at, source, game_id, number, server_ts "
        "FROM spin_observations "
        "WHERE number IS NOT NULL AND source IN ('websocket', 'dom') "
        "ORDER BY observed_at DESC, id DESC LIMIT ?",
        (window,),
    ))
    ws_obs = [r for r in rows if r["source"] == "websocket"]
    dom_obs = [r for r in rows if r["source"] == "dom"]

    ws_used = set()
    conflict_gids = set()
    checked = verified = conflicts = 0
    for dom in dom_obs:                       # newest first
        dom_t = _iso_epoch(dom["observed_at"])
        best = None
        for ws in ws_obs:
            if ws["id"] in ws_used:
                continue
            dt = abs(_iso_epoch(ws["observed_at"]) - dom_t)
            if dt <= AGREEMENT_WINDOW_S and (best is None or dt < best[0]):
                best = (dt, ws)
        if best is None:
            continue                          # UNVERIFIED — not a conflict
        ws = best[1]
        ws_used.add(ws["id"])
        checked += 1
        res = cross_check_and_log(conn, ws, dom, game_id=ws.get("game_id"))
        if res["status"] == "VERIFIED":
            verified += 1
        elif res["status"] == "CONFLICT":
            conflicts += 1
            if ws.get("game_id"):
                conflict_gids.add(ws["game_id"])

    return {
        "checked": checked,
        "verified": verified,
        "conflicts": conflicts,
        "conflict_game_ids": sorted(conflict_gids),
        "agreement_ratio": (verified / checked) if checked else 0.0,
    }
