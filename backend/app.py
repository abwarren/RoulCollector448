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


def rolling_verify(conn, window: int = 500) -> dict:
    """PRD §26/§27 — the rolling-500 trust indicator (core trust signal).

    Examines the latest `window` canonical spins (by canonical sequence:
    sequence_no DESC, id tie-break; NULL sequences last). Column-aware: a
    legacy table without status/sequence_no degrades gracefully (all rows
    counted verified, ordering by id) instead of failing.

    Returns the §26 counters plus `perfect` — the §27 definition:
    no unexplained gaps, no duplicate game_ids, no conflicting ids
    (same game_id, different number), no invalid numbers, nothing
    UNVERIFIED. All counters are pure queries over the canonical table —
    no inference, no repair.
    """
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(roulette_spins)").fetchall()}
    has_status = "status" in cols
    has_seq = "sequence_no" in cols
    order = ("sequence_no IS NULL, sequence_no DESC, id DESC"
             if has_seq else "id DESC")
    sel = ["game_id", "number"]
    if has_status:
        sel.append("status")
    if has_seq:
        sel.append("sequence_no")
    rows = conn.execute(
        f"SELECT {', '.join(sel)} FROM roulette_spins ORDER BY {order} LIMIT ?",
        (window,),
    ).fetchall()
    n = len(rows)

    seqs = [r["sequence_no"] for r in rows
            if has_seq and r["sequence_no"] is not None]
    holes = []
    if seqs:
        lo, hi = min(seqs), max(seqs)
        present = set(seqs)
        holes = [i for i in range(lo, hi + 1) if i not in present]
    # largest single run of consecutive holes (the "current gap")
    largest_gap = 0
    run = 0
    prev = None
    for h in holes:
        run = run + 1 if (prev is not None and h == prev + 1) else 1
        largest_gap = max(largest_gap, run)
        prev = h

    verified = repaired = unverified = invalid = 0
    gid_count = {}
    gid_nums = {}
    for r in rows:
        if has_status:
            st = r["status"]
            if st == "REPAIRED":
                verified += 1
                repaired += 1
            elif st == "VALID":
                verified += 1
            else:                     # UNVERIFIED / NULL / other -> unverified
                unverified += 1
        else:
            verified += 1             # legacy table: no integrity statuses
        num = r["number"]
        if not (isinstance(num, int) and 0 <= num <= 36):
            invalid += 1
        gid = r["game_id"]
        if gid:
            gid_count[gid] = gid_count.get(gid, 0) + 1
            gid_nums.setdefault(gid, set()).add(num)

    duplicates = sum(1 for c in gid_count.values() if c > 1)
    conflicts = sum(1 for nums in gid_nums.values() if len(nums) > 1)
    missing = len(holes)
    perfect = (missing == 0 and duplicates == 0 and conflicts == 0
               and unverified == 0 and invalid == 0)
    return {
        "window": window,
        "checked": n,
        "verified": verified,
        "verified_label": f"{verified} / {n} verified",
        "missing": missing,
        "duplicates": duplicates,
        "conflicts": conflicts,
        "unverified": unverified,
        "repaired": repaired,
        "current_gap": largest_gap,
        "invalid_numbers": invalid,
        "perfect": perfect,
    }


def evaluate_alerts(conn) -> list:
    """PRD §39 — alert conditions. Pure read of the DB; returns a list of
    active alerts, newest first, each {severity, condition, detail, at}.

    CRITICAL:
      * two consecutive reconciliation failures
      * unverified records > 0
      * conflicting game IDs
      * rolling 500 verification < 100%
    WARNING:
      * capture latency increasing (P99 rising across recent passes)
      * DOM/WS disagreement
      * repeated recovery events
      * repair frequency increasing
    """
    import json as _json
    alerts = []

    def add(severity, condition, detail, at=None):
        alerts.append({"severity": severity, "condition": condition,
                       "detail": detail, "at": at})

    # --- rolling-500 verification (the §26/§27 trust indicator) ---
    r5 = rolling_verify(conn)
    if r5["checked"]:
        if r5["verified"] < r5["checked"]:
            add("CRITICAL", "rolling_500_verification_lt_100",
                f"{r5['verified_label']} verified")
        if r5["unverified"] > 0:
            add("CRITICAL", "unverified_records",
                f"{r5['unverified']} unverified record(s) in the latest {r5['window']}")
        if r5["conflicts"] > 0:
            add("CRITICAL", "conflicting_game_ids",
                f"{r5['conflicts']} conflicting game ID(s)")
        if r5["duplicates"] > 0:
            add("CRITICAL", "duplicate_game_ids",
                f"{r5['duplicates']} duplicate game ID(s)")

    # --- two consecutive reconciliation failures ---
    try:
        rows = conn.execute(
            "SELECT created_at, details FROM integrity_events "
            "WHERE event_type IN ('RECONCILIATION','RECONCILIATION_LIGHT') "
            "ORDER BY id DESC LIMIT 2"
        ).fetchall()
        if len(rows) >= 2:
            oks = []
            for r in rows:
                d = _json.loads(r["details"]) if r["details"] else {}
                oks.append(bool(d.get("ok")))
            if not any(oks):
                add("CRITICAL", "two_consecutive_reconciliation_failures",
                    "last two reconciliation passes failed",
                    at=rows[0]["created_at"])
            elif not oks[0]:
                add("WARNING", "reconciliation_failure",
                    "latest reconciliation pass failed",
                    at=rows[0]["created_at"])
    except Exception:
        pass

    # --- DOM/WS disagreement (SOURCE_DISAGREEMENT events) ---
    try:
        n_dis = conn.execute(
            "SELECT COUNT(*) FROM integrity_events "
            "WHERE event_type='SOURCE_DISAGREEMENT' "
            "AND created_at > datetime('now', '-1 hour')"
        ).fetchone()[0]
        if n_dis:
            add("WARNING", "dom_ws_disagreement",
                f"{n_dis} DOM/WS disagreement(s) in the last hour")
    except Exception:
        pass

    # --- repeated recovery events (recovery rungs / RECOVERING states) ---
    try:
        n_rec = conn.execute(
            "SELECT COUNT(*) FROM integrity_events "
            "WHERE event_type IN ('RECOVERY','RECOVERY_START','STALL') "
            "OR (details LIKE '%recovery%' AND details LIKE '%rung%') "
            "AND created_at > datetime('now', '-1 hour')"
        ).fetchone()[0]
        if n_rec >= 3:
            add("WARNING", "repeated_recovery_events",
                f"{n_rec} recovery events in the last hour")
    except Exception:
        pass

    # --- capture latency increasing (P99 rising; heartbeat latency) ---
    try:
        import os as _os
        import pathlib as _pl
        hb_path = _os.environ.get(
            "RC_HEARTBEAT_FILE",
            _pl.Path(_os.path.expanduser("~")) / ".roulette2" /
            "roulette2_heartbeat.json")
        with open(hb_path, encoding="utf-8") as f:
            hb = _json.load(f)
        lat = (hb.get("latency") or {})
        p99 = lat.get("capture_p99") or lat.get("p99")
        p50 = lat.get("capture_p50") or lat.get("p50")
        if p99 is not None and p50 is not None and p99 > 10.0:
            add("WARNING", "capture_latency_increasing",
                f"capture P99 {p99:.1f}s vs P50 {p50:.1f}s — rising")
    except Exception:
        pass

    # --- repair frequency increasing (repairs in the last hour vs the hour before) ---
    try:
        cur_h = conn.execute(
            "SELECT COUNT(*) FROM repair_events "
            "WHERE created_at > datetime('now', '-1 hour')"
        ).fetchone()[0]
        prev_h = conn.execute(
            "SELECT COUNT(*) FROM repair_events "
            "WHERE created_at <= datetime('now', '-1 hour') "
            "AND created_at > datetime('now', '-2 hours')"
        ).fetchone()[0]
        if cur_h > 0 and cur_h > prev_h * 2 and prev_h > 0:
            add("WARNING", "repair_frequency_increasing",
                f"{cur_h} repairs this hour vs {prev_h} the hour before")
    except Exception:
        pass

    # newest first (most recent alert first), stable order otherwise
    alerts.sort(key=lambda a: (a.get("at") or ""), reverse=True)
    return alerts


@app.get("/api/integrity/window")
def integrity_window():
    """PRD §38 — the rolling-500 verification window breakdown."""
    conn = db.connect()
    try:
        return {"ok": True, **rolling_verify(conn)}
    finally:
        conn.close()


@app.get("/api/integrity/repairs")
def integrity_repairs(limit: int = Query(20, ge=1, le=100)):
    """PRD §38 — repair history from the repair queue (repair_events)."""
    conn = db.connect()
    try:
        import json as _json
        rows = conn.execute(
            "SELECT id, created_at, incident_type, start_game_id, end_game_id, "
            "affected_count, status, attempts, last_attempt_at, resolved_at, "
            "resolution, details FROM repair_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            det = r["details"]
            if isinstance(det, str):
                try:
                    det = _json.loads(det)
                except Exception:
                    det = {"note": det}
            out.append({
                "id": r["id"], "created_at": r["created_at"],
                "incident_type": r["incident_type"],
                "start_game_id": r["start_game_id"],
                "end_game_id": r["end_game_id"],
                "affected_count": r["affected_count"],
                "status": r["status"], "attempts": r["attempts"],
                "last_attempt_at": r["last_attempt_at"],
                "resolved_at": r["resolved_at"],
                "resolution": r["resolution"], "details": det,
            })
        return {"ok": True, "repairs": out}
    finally:
        conn.close()


@app.get("/api/integrity/incidents")
def integrity_incidents(limit: int = Query(20, ge=1, le=100)):
    """PRD §38 — incidents under the integrity namespace (alias of
    /api/incidents, the §37 incident feed)."""
    return incidents(limit)


@app.get("/api/integrity/alerts")
def integrity_alerts():
    """PRD §39 — active alert conditions (critical + warning)."""
    conn = db.connect()
    try:
        return {"ok": True, "alerts": evaluate_alerts(conn)}
    finally:
        conn.close()


@app.get("/api/integrity")
def integrity():
    """Data-integrity panel payload (PRD §36): latest reconciliation state,
    health score, telemetry, verified window, open incidents, repair history,
    per-component health, and the §26 rolling-500 trust indicator (§27
    perfect-dataset definition)."""
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
        r5 = rolling_verify(conn)
        # PRD §38 flat metrics shape (the documented contract) alongside the
        # richer panel fields.
        return {
            "ok": True,
            "window": AUDIT_WINDOW,
            "verified_count": verified,
            "total_spins": total,
            "verified_window": f"{verified} / {total} verified",
            # §38 flat shape — the machine contract
            "score": score,
            "verified": r5["verified"],
            "missing": r5["missing"],
            "duplicates": r5["duplicates"],
            "conflicts": r5["conflicts"],
            "unverified": r5["unverified"],
            "repaired": r5["repaired"],
            "last_reconciliation": (last_recon or {}).get("at"),
            "status": "VERIFIED" if (last_recon or {}).get("ok") else (
                "UNVERIFIED" if last_recon else "NO_DATA"),
            "latest_spin": {
                "number": None,
                "color": None,
            },
            "last_reconciliation": last_recon,
            "last_repair": last_repair_d,
            "open_incidents": open_incidents,
            "health_score": score,
            "collector_state": (last_recon or {}).get("state"),
            "rolling500": r5,
            "alerts": evaluate_alerts(conn),
            # §26-new: gap lifecycle — the dashboard distinguishes a
            # REPAIRED gap (RESOLVED/REPAIRED) from an UNVERIFIED one
            # (UNVERIFIED = permanent). Latest GAP events, newest first.
            "gap_events": [
                {"id": r["id"], "created_at": r["created_at"],
                 "start": r["start_game_id"], "end": r["end_game_id"],
                 "size": r["affected_count"], "status": r["status"],
                 "resolution": r["resolution"], "resolved_at": r["resolved_at"]}
                for r in conn.execute(
                    "SELECT id, created_at, start_game_id, end_game_id, "
                    "affected_count, status, resolution, resolved_at "
                    "FROM repair_events WHERE incident_type='GAP' "
                    "ORDER BY id DESC LIMIT 10").fetchall()
            ],
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
