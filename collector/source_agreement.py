"""Cross-source agreement (Phase 5, PRD §20).

Instead of treating DOM as merely a fallback, use it as a SECONDARY
verification channel: when websocket and DOM both observed the same game_id,
they should agree. Agreement -> VERIFIED; disagreement -> SOURCE_DISAGREEMENT
event and the spin is never silently resolved (PRD §20/§25 — multiple
conflicting sources => no auto-repair, surface instead).
"""

from collector.observer import log_event


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
