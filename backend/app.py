"""RoulCollector448 — read-only API + static dashboard, port 4480.

Liveness comes from backend/liveness.py: journald on Linux, the collector's
heartbeat file on Windows. The API response shapes are OS-independent.
"""

import datetime
import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from . import db, liveness, stats
from .wheel import nn_cluster

AUDIT_INTERVAL = 3600  # audit panel refresh cadence (frontend re-fetches hourly)
AUDIT_WINDOW = 500     # audit compares against the last 500 spins

FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)

# The collector commits to SQLite in batches of 25 spins (~18 min at 44s
# cadence), so DB freshness lags. The journald/heartbeat signal logs EVERY
# spin — that is the true liveness signal (cached 15s inside liveness.py).

# ---------------------------------------------------------------------------
# Integrity endpoints (PRD §36-37)
# ---------------------------------------------------------------------------

# Root-cause classification (§40): event_type -> category + recovery action.
# REPAIR entries are checked before RECONCILIATION so "RECONCILIATION_REPAIR"
# (repair_events rows) classifies as a repair, not an audit.
_ROOT_CAUSE = {
    "STALL": ("STALL", "Recovery ladder executed"),
    "SESSION_START": ("SESSION", "Session opened"),
    "SESSION_END": ("SESSION", "Session closed"),
    "REPAIR_FAILED": ("REPAIR", "Repair attempted, verify failed"),
    "REPAIR": ("REPAIR", "Deterministic repair applied"),
    "MISSING": ("DATA", "Backfill from authoritative history"),
    "SPIN_INVALID": ("DATA", "Per-spin validation: structurally invalid"),
    "SPIN_SUSPECT": ("DATA", "Per-spin validation: anomaly flagged"),
    "SPIN_NO_IDENTITY": ("DATA", "No establishable identity — kept as observation"),
    "DUPLICATE": ("DATA", "Duplicate game_id collapsed (CONFLICT=critical)"),
    "REPAIR_REFUSED": ("DATA", "PRD §25: auto-repair refused (no authority/identity/conflict)"),
    "LATENCY_HIGH": ("PERFORMANCE", "Capture latency P99 breach — degradation warning"),
    "RECONCILIATION": ("RECONCILIATION", "Full-window audit vs site history"),
    "RECONCILIATION_LIGHT": ("RECONCILIATION", "Light audit vs site history"),
    "SOURCE_DISAGREEMENT": ("DATA", "WS vs DOM conflict surfaced, no auto-repair"),
    "GAP": ("DATA", "Cadence gap >= 120s flagged"),
    "CDP": ("CDP", "CDP interception failure"),
}


def classify_event(event_type: str, details=None) -> dict:
    """Map an integrity event to {root_cause, action, severity} (PRD §40)."""
    for key, (cat, action) in _ROOT_CAUSE.items():
        if key in (event_type or "").upper():
            return {"root_cause": cat, "action": action}
    # fall back on details content
    blob = str(details or "").lower()
    if "disconnect" in blob or "reconnect" in blob:
        return {"root_cause": "WS_DISCONNECT", "action": "Reconnection ladder"}
    if "cdp" in blob:
        return {"root_cause": "CDP", "action": "CDP re-arm / timeout guard"}
    return {"root_cause": "OTHER", "action": "Investigate"}


def _compute_audit():
    conn = db.connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0]
        live = liveness.live_spins_uncommitted(total)
        return stats.audit(conn, window=AUDIT_WINDOW, live_spins=live)
    finally:
        conn.close()


app = FastAPI(title="RoulCollector448")


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Never cache frontend files or API responses — any reload picks up
    edits immediately, and the live feed never serves stale JSON."""
    resp = await call_next(request)
    if (
        request.url.path in ("/", "/index.html", "/app.js", "/style.css")
        or request.url.path.startswith("/api/")
    ):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


def _now_iso(ts: str) -> datetime.datetime:
    try:
        return datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return datetime.datetime.now()


@app.get("/api/health")
def health():
    conn = db.connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0]
        last = conn.execute(
            "SELECT id, number, color, captured_at FROM roulette_spins "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_captured = last["captured_at"] if last else None
        db_age = (
            (datetime.datetime.now() - _now_iso(last_captured)).total_seconds()
            if last_captured
            else None
        )
        lv = liveness.get_liveness()
        live_all = lv["lines"]
        live = live_all[0] if live_all else None
        live_age = lv["age"]
        return {
            "ok": True,
            "db": db.DB_PATH,
            "total_spins": total,
            "last_spin": dict(last) if last else None,
            "last_captured_at": last_captured,
            "db_age_seconds": round(db_age, 1) if db_age is not None else None,
            "live_last_spin": live,
            "live_spins": live_all,
            "live_age_seconds": round(live_age, 1) if live_age is not None else None,
            "liveness_source": lv["source"],
            "collector_status": lv["hb_status"],
            "latency": (lv.get("hb_raw") or {}).get("latency"),
            "collector_alive": (live_age is not None and live_age < 180)
                               or (db_age is not None and db_age < 180),
            "db_mtime": datetime.datetime.fromtimestamp(
                os.path.getmtime(db.DB_PATH)
            ).isoformat(),
            "now": datetime.datetime.now().isoformat(),
        }
    finally:
        conn.close()


@app.get("/api/integrity")
def integrity():
    """Data-integrity panel payload (PRD §36): latest reconciliation state,
    health score, telemetry, verified window, open incidents, repair history,
    and per-component health (WebSocket/DOM/SQLite/Collector)."""
    conn = db.connect()
    try:
        # latest reconciliation event (light or full) carries state/score/telemetry
        row = conn.execute(
            "SELECT created_at, event_type, severity, details, root_cause "
            "FROM integrity_events "
            "WHERE event_type IN ('RECONCILIATION','RECONCILIATION_LIGHT') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_recon = None
        if row:
            import json as _json
            details = _json.loads(row["details"]) if row["details"] else {}
            last_recon = {
                "at": row["created_at"],
                "event_type": row["event_type"],
                "severity": row["severity"],
                **details,
            }

        open_incidents = conn.execute(
            "SELECT COUNT(*) FROM repair_events WHERE status='OPEN'"
        ).fetchone()[0]

        last_repair = conn.execute(
            "SELECT created_at, incident_type, affected_count, resolution, "
            "details FROM repair_events WHERE resolution IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_repair_d = dict(last_repair) if last_repair else None

        verified = conn.execute(
            "SELECT COUNT(*) FROM roulette_spins WHERE status='VALID' "
            "OR status='REPAIRED'"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0]

        # component health (PRD §36): WS/DOM from latest telemetry, SQLite by
        # a live read (this query just succeeded), Collector from liveness.
        telemetry = (last_recon or {}).get("telemetry") or {}
        ws_ok = bool(telemetry.get("ws_spins_accepted", 0)) or total > 0
        dom_ok = telemetry.get("dom_candidates", 0) > 0
        lv = liveness.get_liveness()
        collector_ok = bool(lv["ok"]) and (
            lv["age"] is None or lv["age"] < 180
        ) or (total > 0)

        score = (last_recon or {}).get("score")
        return {
            "ok": True,
            "window": AUDIT_WINDOW,
            "verified_count": verified,
            "total_spins": total,
            "verified_window": f"{verified} / {total} verified",
            "latest_spin": {
                "number": None,
                "color": None,
            },
            "last_reconciliation": last_recon,
            "last_repair": last_repair_d,
            "open_incidents": open_incidents,
            "health_score": score,
            "collector_state": (last_recon or {}).get("state"),
            "components": {
                "collector": "Healthy" if collector_ok else "Stalled",
                "websocket": "Healthy" if ws_ok else "Idle",
                "dom": "Healthy" if dom_ok else "Idle",
                "sqlite": "Healthy",
            },
            "liveness_source": lv["source"],
            "collector_status": lv["hb_status"],
        }
    finally:
        conn.close()


@app.get("/api/incidents")
def incidents(limit: int = Query(20, ge=1, le=100)):
    """Incident panel (PRD §37): last N incidents, merged from
    integrity_events (detections) and repair_events (resolutions), each with
    root-cause classification (§40) and an action/result summary."""
    conn = db.connect()
    try:
        import json as _json

        rows = conn.execute(
            "SELECT created_at AS time, event_type AS type, severity, "
            "game_id, details, root_cause FROM integrity_events "
            "UNION ALL "
            "SELECT created_at, incident_type, "
            "CASE WHEN status='OPEN' THEN 'WARNING' ELSE 'INFO' END, "
            "COALESCE(start_game_id, end_game_id), details, "
            "'REPAIR:' || COALESCE(resolution, status) "
            "FROM repair_events "
            "ORDER BY time DESC LIMIT ?",
            (limit,),
        ).fetchall()

        out = []
        for r in rows:
            details = r["details"]
            if isinstance(details, str):
                try:
                    details = _json.loads(details)
                except Exception:
                    details = {"note": details}
            details = details or {}
            affected = details.get("missing", 0) or details.get("affected_count", 0)
            if details.get("repaired") is not None:
                rep = details["repaired"] or {}
                affected = sum(v for k, v in rep.items()
                               if isinstance(v, int) and k != "repair_event_id")
            cls = classify_event(r["type"], details)
            action = details.get("message") or cls["action"]
            result = "Repaired" if details.get("repaired") else (
                "Verified" if details.get("ok") else (
                    "Open" if r["severity"] == "WARNING" else "Detected"
                )
            )
            out.append({
                "time": r["time"],
                "type": r["type"],
                "severity": r["severity"],
                "affected": affected,
                "action": action,
                "result": result,
                "game_id": r["game_id"],
                "root_cause": cls["root_cause"],
            })
        return {"ok": True, "incidents": out}
    finally:
        conn.close()


@app.get("/api/spins/count")
def spins_count():
    conn = db.connect()
    try:
        return {"total": conn.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0]}
    finally:
        conn.close()


@app.get("/api/spins")
def spins(
    offset: int = Query(0, ge=0),
    limit: int = Query(2000, ge=1, le=10000),
):
    """Chronological spins; offset counts back from the newest."""
    conn = db.connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0]
        start = max(0, total - limit - offset)
        rows = conn.execute(
            "SELECT id, number, color, captured_at, server_ts FROM roulette_spins "
            "ORDER BY id ASC LIMIT ? OFFSET ?",
            (limit, start),
        ).fetchall()
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "returned": len(rows),
            "spins": [dict(r) for r in rows],
        }
    finally:
        conn.close()


@app.get("/api/neighbors/{number}")
def neighbors(number: int):
    if number < 0 or number > 36:
        raise HTTPException(status_code=400, detail="number must be 0-36")
    cluster = nn_cluster(number)
    return {"number": number, "cluster": cluster}


@app.get("/api/stats/numbers")
def stats_numbers(limit: int | None = Query(None, ge=1, le=100000)):
    conn = db.connect()
    try:
        return stats.numbers_stats(conn, limit=limit)
    finally:
        conn.close()


@app.get("/api/stats/sleepers")
def stats_sleepers():
    """Current drought per number — merged with uncommitted live spins so the
    panel is realtime-correct instead of lagging the 25-spin DB batches."""
    conn = db.connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0]
        live = liveness.live_spins_uncommitted(total)
        return stats.sleepers(conn, live_spins=live)
    finally:
        conn.close()


@app.get("/api/stats/streaks")
def stats_streaks():
    conn = db.connect()
    try:
        return stats.streaks(conn)
    finally:
        conn.close()


@app.get("/api/stats/rolling")
def stats_rolling(window: int = Query(500, ge=50, le=10000)):
    conn = db.connect()
    try:
        return stats.rolling(conn, window)
    finally:
        conn.close()


@app.get("/api/audit")
def get_audit(fresh: bool = Query(False)):
    """Audit vs the true last 500 spins (DB + uncommitted live merge).

    Computed on demand so the panel is never stale — there is no hourly
    snapshot to lag behind (the frontend re-fetches hourly itself).
    `fresh` kept for API compatibility; every request is fresh.
    """
    data = _compute_audit()
    return {
        "generated_at": datetime.datetime.now().isoformat(),
        "interval_seconds": AUDIT_INTERVAL,
        "window": AUDIT_WINDOW,
        "error": None,
        "audit": data,
    }


@app.get("/api/transitions")
def get_transitions(limit: int | None = Query(None, ge=100, le=100000)):
    """37x37 next-spin transition matrix + neighbor-sequence diagnostics.

    Live-merges uncommitted live spins so the matrix tracks the wheel in
    realtime, not the 25-spin DB batch cadence.
    """
    conn = db.connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0]
        live = liveness.live_spins_uncommitted(total)
        return stats.transitions(conn, limit=limit, live_spins=live)
    finally:
        conn.close()


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
