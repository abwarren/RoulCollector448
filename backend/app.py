"""RoulCollector448 — read-only API + static dashboard, port 4480."""

import asyncio
import datetime
import os
import re
import subprocess
import time

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from . import db, stats
from .wheel import nn_cluster

AUDIT_INTERVAL = 3600  # hourly audit (per requirements)
AUDIT_WINDOW = 500    # audit compares against the last 500 spins
COLLECTOR_SERVICE = "roulette-collector2.service"

_audit_store = {"data": None, "generated_at": None, "error": None}

FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)

# The collector commits to SQLite in batches of 25 spins (~18 min at 44s
# cadence), so DB freshness lags. Journald logs EVERY spin — that's the
# true liveness signal. Cache the read for 15s (health is polled every 5s).
_journal_cache = {"at": 0.0, "ok": False, "line": "", "age": None}

_SPIN_LINE_RE = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]\s+#\d+:\s+(\d+)\s+(\w+)")

# how many journald spin lines to keep for the live overlay (covers ~1 DB batch)
LIVE_SPINS_KEEP = 40


def _journal_last():
    """Last journald lines for the collector: timestamp age + last spin."""
    now = time.time()
    if now - _journal_cache["at"] < 15:
        return _journal_cache
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", COLLECTOR_SERVICE, "-n", "60",
             "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"},
        ).stdout.strip()
        # last line's timestamp = collector liveness (status lines every ~30s)
        lines = [l for l in out.splitlines() if l.strip()]
        m = re.match(
            r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:?\d{2})",
            lines[-1] if lines else "",
        )
        if m:
            ts = datetime.datetime.fromisoformat(m.group(1))
            age = (datetime.datetime.now().astimezone() - ts).total_seconds()
            _journal_cache.update({"at": now, "ok": True, "line": out, "age": age})
        else:
            _journal_cache.update({"at": now, "ok": False, "line": out, "age": None})
    except Exception:
        _journal_cache.update({"at": now, "ok": False, "line": "", "age": None})
    return _journal_cache


def _parse_live_spins(lines: str):
    """ALL spin lines in the journal tail, newest first (for realtime grid)."""
    spins = []
    for line in reversed(lines.splitlines()):
        m = _SPIN_LINE_RE.search(line)
        if m:
            spins.append({"time": m.group(1), "number": int(m.group(2)), "color": m.group(3)})
            if len(spins) >= LIVE_SPINS_KEEP:
                break
    return spins


def _compute_audit():
    conn = db.connect()
    try:
        return stats.audit(conn, window=AUDIT_WINDOW)
    finally:
        conn.close()


async def _audit_loop():
    while True:
        try:
            data = await asyncio.to_thread(_compute_audit)
            _audit_store["data"] = data
            _audit_store["generated_at"] = datetime.datetime.now().isoformat()
            _audit_store["error"] = None
        except Exception as exc:  # keep the loop alive on transient errors
            _audit_store["error"] = str(exc)
        await asyncio.sleep(AUDIT_INTERVAL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(_audit_loop())
    yield
    task.cancel()


app = FastAPI(title="RoulCollector448", lifespan=lifespan)


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Never cache frontend files — any reload picks up edits immediately."""
    resp = await call_next(request)
    if request.url.path in ("/", "/index.html", "/app.js", "/style.css"):
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
        jl = _journal_last()
        live_all = _parse_live_spins(jl["line"]) if jl["ok"] else []
        live = live_all[0] if live_all else None
        live_age = jl["age"]
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
            "collector_alive": (live_age is not None and live_age < 180)
                               or (db_age is not None and db_age < 180),
            "db_mtime": datetime.datetime.fromtimestamp(
                os.path.getmtime(db.DB_PATH)
            ).isoformat(),
            "now": datetime.datetime.now().isoformat(),
        }
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
    conn = db.connect()
    try:
        return stats.sleepers(conn)
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
    """Return the hourly audit. Computes on demand if none is stored yet."""
    if fresh or _audit_store["data"] is None:
        _audit_store["data"] = _compute_audit()
        _audit_store["generated_at"] = datetime.datetime.now().isoformat()
    return {
        "generated_at": _audit_store["generated_at"],
        "interval_seconds": AUDIT_INTERVAL,
        "window": AUDIT_WINDOW,
        "error": _audit_store["error"],
        "audit": _audit_store["data"],
    }


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
