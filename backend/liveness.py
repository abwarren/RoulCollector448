"""Liveness source abstraction for the dashboard.

The collector's true liveness signal differs per OS:
  * Linux  — journald (systemd user unit tailed via journalctl). Every spin is
             logged, plus ~1 Status line per 30s, so age == collector liveness.
  * Windows — no journald; the collector writes a structured heartbeat file
             (roulette2_heartbeat.json) every ~5s containing its status, the
             last spins (position-countered like the journald #N lines) and
             timestamps. This module reads it with the same shape.

get_liveness() returns a unified dict (cached 15s, matching the old journald
cache) so backend/app.py never cares which source is in use.
"""

import datetime
import json
import os
import re
import subprocess
import time

COLLECTOR_SERVICE = "roulette-collector2.service"
# Journal tail lines fetched per poll. Must comfortably cover LIVE_SPINS_KEEP
# spin lines despite ~1.4 Status: noise lines per spin (~100 lines for 40
# spins), AND leave enough history for the session-marker cut to find the
# last "Starting Roulette 2 session" line (stale pre-restart lines live
# further back in the journal).
JOURNAL_TAIL = 300
SESSION_MARKER = "===== Starting Roulette 2 session ====="

# how many spin lines to keep for the live overlay (covers ~1 DB batch)
LIVE_SPINS_KEEP = 40

# collector's own stdout spin line: "  [18:22:49] #17832: 23 Black"
_SPIN_LINE_RE = re.compile(
    r"\[(\d{2}:\d{2}:\d{2})\]\s+#(\d+):\s+(\d+)\s+(\w+)"
)


def heartbeat_path() -> str:
    """Heartbeat file location: RC_HEARTBEAT_FILE, else under the data dir."""
    env = os.environ.get("RC_HEARTBEAT_FILE")
    if env:
        return env
    if os.name == "nt":
        return os.path.join(os.path.expanduser("~"), ".roulette2", "roulette2_heartbeat.json")
    return "/home/wa/roulette2_heartbeat.json"


def _after_last_session(lines: str) -> str:
    """Drop everything up to and including the collector's last session-start
    marker.

    The journald counter (#N) is `len(spins)` at print time, and `spins`
    resumes from the JSON state file — which resets to the DB count at every
    restart. A restart therefore leaves PREVIOUS session's spin lines in the
    journal whose #N counters overlap the current session's (e.g. pre-restart
    #17828..17831 + post-restart #17818..), and the plain
    `db_total < n <= db_total + KEEP` guard cannot tell them apart. Counting
    both double-fills the live window with stale spins. Only lines after the
    LAST session marker belong to the live session.
    """
    idx = lines.rfind(SESSION_MARKER)
    if idx == -1:
        return lines
    return lines[idx + len(SESSION_MARKER):]


def _journal_last():
    """Last journald lines for the collector: timestamp age + raw tail."""
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", COLLECTOR_SERVICE, "-n",
             str(JOURNAL_TAIL), "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"},
        ).stdout.strip()
        lines = [l for l in out.splitlines() if l.strip()]
        m = re.match(
            r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:?\d{2})",
            lines[-1] if lines else "",
        )
        if m:
            ts = datetime.datetime.fromisoformat(m.group(1))
            age = (datetime.datetime.now().astimezone() - ts).total_seconds()
            return {"ok": True, "line": out, "age": age}
        return {"ok": False, "line": out, "age": None}
    except Exception:
        return {"ok": False, "line": "", "age": None}


def _read_heartbeat():
    """Read + parse the collector heartbeat file (Windows liveness)."""
    try:
        with open(heartbeat_path(), encoding="utf-8") as f:
            hb = json.load(f)
    except Exception:
        return {"ok": False, "heartbeat": None, "age": None}
    at = hb.get("at")
    if not at:
        return {"ok": False, "heartbeat": hb, "age": None}
    try:
        ts = datetime.datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return {"ok": False, "heartbeat": hb, "age": None}
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
    age = (datetime.datetime.now().astimezone() - ts).total_seconds()
    return {"ok": True, "heartbeat": hb, "age": age}


_cache = {"at": 0.0, "data": None}


def get_liveness(force: bool = False) -> dict:
    """Unified liveness dict, cached 15s:

      {ok, source: 'journald'|'heartbeat', age, lines (newest-first spin
       dicts {time,n,number,color}), session_marker_found, hb_status,
       last_spin, hb_raw}
    """
    now = time.time()
    if not force and _cache["data"] and now - _cache["at"] < 15:
        return _cache["data"]

    data = {"ok": False, "source": None, "age": None,
            "lines": [], "session_marker_found": False,
            "hb_status": None, "last_spin": None, "hb_raw": None}

    if os.name == "nt":
        hb = _read_heartbeat()
        data.update(source="heartbeat", age=hb["age"], hb_raw=hb["heartbeat"])
        if hb["ok"]:
            hb_data = hb["heartbeat"] or {}
            recent = hb_data.get("recent_spins") or []
            # newest-first, same shape as journald parsing
            data["lines"] = list(reversed(recent))
            data["hb_status"] = hb_data.get("status")
            data["last_spin"] = hb_data.get("last_spin")
            data["ok"] = True
    else:
        jl = _journal_last()
        data.update(source="journald", age=jl["age"])
        if jl["ok"]:
            lines = jl["line"]
            data["session_marker_found"] = SESSION_MARKER in lines
            spins = []
            for line in reversed(_after_last_session(lines).splitlines()):
                m = _SPIN_LINE_RE.search(line)
                if m:
                    spins.append({
                        "time": m.group(1),
                        "n": int(m.group(2)),
                        "number": int(m.group(3)),
                        "color": m.group(4),
                    })
                    if len(spins) >= LIVE_SPINS_KEEP:
                        break
            data["lines"] = spins
            data["ok"] = True

    _cache.update({"at": now, "data": data})
    return data


def live_spins_uncommitted(db_total: int) -> list:
    """Journald/heartbeat spins newer than the DB, chronological (not yet
    committed). Same guard as before: only spins with n in
    (db_total, db_total + LIVE_SPINS_KEEP] count, and on journald only lines
    after the last session marker (restart counter resets would otherwise
    double-fill the window)."""
    lv = get_liveness()
    if not lv["ok"]:
        return []
    if lv["source"] == "heartbeat":
        # heartbeat recent_spins are chronological; n == position in dataset
        return [s for s in lv["hb_raw"].get("recent_spins") or []
                if db_total < s.get("n", 0) <= db_total + LIVE_SPINS_KEEP]
    out = []
    for s in reversed(lv["lines"]):  # lines newest-first -> chronological
        if db_total < s["n"] <= db_total + LIVE_SPINS_KEEP:
            out.append({"number": s["number"], "time": s["time"]})
    return out
