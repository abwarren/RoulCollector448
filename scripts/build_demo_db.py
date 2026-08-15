"""Build a demo DB with integrity tables + events so the dashboard's new
DATA INTEGRITY / INCIDENT panels have real data to render. Dev tool only."""
import datetime
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

DB = pathlib.Path.home() / ".roulette2" / "demo.db"
DB.parent.mkdir(exist_ok=True)
if DB.exists():
    DB.unlink()

REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# canonical table (legacy shape, then ensure_schema migrates it)
conn.executescript("""
CREATE TABLE roulette_spins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    number      INTEGER NOT NULL,
    description TEXT NOT NULL,
    color       TEXT NOT NULL,
    game_id     TEXT NOT NULL UNIQUE,
    server_ts   TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""")

now = datetime.datetime.now()
for i in range(2200):
    n = i % 37
    color = "Green" if n == 0 else ("Red" if n in REDS else "Black")
    ts = (now - datetime.timedelta(seconds=(2200 - i) * 44)).isoformat()
    conn.execute(
        "INSERT INTO roulette_spins (number, description, color, game_id, server_ts, captured_at) "
        "VALUES (?,?,?,?,?,?)",
        (n, f"{n} {color}", color, f"g{i}", ts, ts),
    )
conn.commit()

# integrity layer
os.environ.setdefault("RC_DB_PATH", str(DB))
from collector import observer, schema  # noqa: E402

schema.ensure_schema(conn)
conn.execute("UPDATE roulette_spins SET sequence_no = id, status='VALID', "
             "source='websocket', first_seen_at = server_ts, "
             "last_verified_at = captured_at")

sid = observer.start_session(conn, source="cdp-ws")
observer.log_event(conn, "SESSION_START", details={"resumed_spins": 2200, "last_game_id": "g2199"})

# a verified reconciliation
observer.log_event(
    conn, "RECONCILIATION", severity="INFO",
    details={
        "ok": True, "window": 500, "window_achieved": 25, "missing": 0,
        "corrections": 0, "duplicates": 0, "authoritative": True,
        "message": "verified", "repaired": None, "state": "HEALTHY",
        "score": 99.2,
        "telemetry": {
            "ws_frames_received": 1240, "ws_spin_candidates": 2200,
            "ws_spins_accepted": 2200, "dom_candidates": 3,
            "dom_spins_detected": 3, "reconciliations": 8,
            "reconciliation_failures": 0, "repairs": 1,
        },
    },
    root_cause="RECONCILIATION",
)

# a past repair (backfill of a missing spin)
observer.log_event(
    conn, "RECONCILIATION", severity="INFO",
    details={
        "ok": False, "window": 500, "window_achieved": 25, "missing": 1,
        "corrections": 0, "duplicates": 0, "authoritative": True,
        "message": "repair plan generated", "repaired": {"backfilled": 1},
        "state": "RECONCILING", "score": 95.4,
        "telemetry": {"reconciliations": 7, "repairs": 1},
    },
    root_cause="RECONCILIATION",
)
conn.execute(
    "INSERT INTO repair_events (created_at, incident_type, start_game_id, "
    "end_game_id, affected_count, status, attempts, last_attempt_at, "
    "resolved_at, resolution, details) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
    (observer.now_iso(), "RECONCILIATION_REPAIR", "g1087", "g1089", 1,
     "RESOLVED", 1, observer.now_iso(), observer.now_iso(), "REPAIRED",
     '{"backfilled": 1, "corrected": 0, "collapsed": 0, "reordered": 0}'),
)

# a gap incident (open repair)
observer.log_event(
    conn, "GAP", severity="WARNING",
    details={"cadence_s": 128, "window": 500},
    root_cause="DATA",
)
conn.execute(
    "INSERT INTO repair_events (created_at, incident_type, start_game_id, "
    "end_game_id, affected_count, status, attempts, last_attempt_at, "
    "resolution, details) VALUES (?,?,?,?,?,?,?,?,?,?)",
    (observer.now_iso(), "MISSING_SPIN", "g1107", "g1109", 1,
     "OPEN", 0, None, None, '{"detected": "cadence gap 128s"}'),
)

# a WS disconnect incident
observer.log_event(
    conn, "WS_DISCONNECT", severity="WARNING",
    details={"action": "Reconnection ladder", "rung": 1},
    root_cause="WS_DISCONNECT",
)

observer.end_session(conn, sid, spins_captured=2200, status="ENDED")
conn.close()

# heartbeat file so /api/health reports collector alive + live overlay
hb = {
    "at": datetime.datetime.now().isoformat(),
    "status": "RUNNING",
    "spins_count": 2200,
    "ws_captured": 2200,
    "session_id": sid,
    "last_spin": {"number": 36, "description": "36 Black", "gameId": "g2199",
                  "timestamp": now.isoformat(),
                  "captured_at": now.isoformat()},
    "recent_spins": [
        {"time": (now - datetime.timedelta(seconds=k * 44)).isoformat()[11:19],
         "n": 2200 - k,
         "number": (36 - k) % 37,
         "color": "Green" if (36 - k) % 37 == 0 else
                  ("Red" if (36 - k) % 37 in REDS else "Black")}
        for k in range(5)
    ],
}
hb_path = pathlib.Path.home() / ".roulette2" / "roulette2_heartbeat.json"
hb_path.write_text(__import__("json").dumps(hb))

print(f"demo DB: {DB} ({os.path.getsize(DB)} bytes)")
print(f"heartbeat: {hb_path}")
