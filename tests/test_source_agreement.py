"""PRD §20 — WS-vs-DOM source agreement as a per-spin integrity check.

verify_recent_agreement turns the dormant cross_check helper into a real
per-spin verification pass over the most recent observations: each DOM
observation (number only, no game_id) is paired with the websocket
observation of the same spin (close observed_at) and cross-checked.

  * same number, close observed_at      -> VERIFIED (counted)
  * different number, close observed_at -> CONFLICT (SOURCE_DISAGREEMENT
    event logged — never silent on conflict)
  * DOM obs with no WS obs within 3s    -> UNVERIFIED, no event (absence
    of the second source is not a contradiction)
"""

import json
import sqlite3

from collector import observer, schema, source_agreement


def _conn(tmp_path, name):
    conn = sqlite3.connect(tmp_path / name)
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    return conn


def _disagreement_events(conn):
    return conn.execute(
        "SELECT * FROM integrity_events "
        "WHERE event_type = 'SOURCE_DISAGREEMENT'"
    ).fetchall()


def test_agreeing_observations_verified(tmp_path):
    """Same number, close observed_at -> paired, VERIFIED, no event."""
    conn = _conn(tmp_path, "agree.db")
    sid = observer.start_session(conn)
    observer.record_observation(conn, source="websocket", session_id=sid,
                                game_id="g1", number=17,
                                server_ts="2026-08-15T00:01:00Z")
    observer.record_observation(conn, source="dom", session_id=sid, number=17)
    try:
        res = source_agreement.verify_recent_agreement(conn)
        assert res == {"checked": 1, "verified": 1, "conflicts": 0,
                       "agreement_ratio": 1.0}
        assert _disagreement_events(conn) == []
    finally:
        conn.close()


def test_disagreeing_observations_log_conflict(tmp_path):
    """Different number, close observed_at -> CONFLICT logged as
    SOURCE_DISAGREEMENT (PRD §20: never silent on conflict)."""
    conn = _conn(tmp_path, "conflict.db")
    sid = observer.start_session(conn)
    observer.record_observation(conn, source="websocket", session_id=sid,
                                game_id="g2", number=17,
                                server_ts="2026-08-15T00:02:00Z")
    observer.record_observation(conn, source="dom", session_id=sid, number=7)
    try:
        res = source_agreement.verify_recent_agreement(conn)
        assert res == {"checked": 1, "verified": 0, "conflicts": 1,
                       "agreement_ratio": 0.0}
        evs = _disagreement_events(conn)
        assert len(evs) == 1
        details = json.loads(evs[0]["details"])
        assert details["ws"] == 17 and details["dom"] == 7
        assert evs[0]["root_cause"] == "DATA_INTEGRITY"
        assert evs[0]["game_id"] == "g2"
    finally:
        conn.close()


def test_dom_without_ws_pair_unverified(tmp_path):
    """DOM obs with no WS obs within the 3s window -> UNVERIFIED, no event
    (missing second source is not a contradiction)."""
    conn = _conn(tmp_path, "unverified.db")
    sid = observer.start_session(conn)
    wid = observer.record_observation(conn, source="websocket", session_id=sid,
                                      game_id="g3", number=10,
                                      server_ts="2026-08-15T00:03:00Z")
    observer.record_observation(conn, source="dom", session_id=sid, number=20)
    # push the WS obs far outside the agreement window: that spin was never
    # observed by the websocket, so the DOM obs has no pair to check against
    conn.execute("UPDATE spin_observations SET observed_at = "
                 "'2026-08-15T00:00:00+00:00' WHERE id = ?", (wid,))
    conn.commit()
    try:
        res = source_agreement.verify_recent_agreement(conn)
        assert res == {"checked": 0, "verified": 0, "conflicts": 0,
                       "agreement_ratio": 0.0}
        assert _disagreement_events(conn) == []
    finally:
        conn.close()
