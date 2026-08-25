#!/usr/bin/env python3
"""
Auto Roulette spin collector — 24/7 operation.
Table 448 — Auto Roulette (Auto-Roulette 2 - 400K) via Sunbet / Evolution Gaming.

Capture: CDP WebSocket interception (catches ALL frames) + DOM polling fallback.
Persistence: JSON state + CSV export + SQLite (INSERT OR IGNORE, dedup by gameId).

v2 — 2026-08-12 reliability ladder (fixes recurring time gaps in the dataset):
  * Stall threshold raised 44s -> 120s. Table 448 legit cadence is 40-57s
    (median 44s), so the old 44s detector false-fired ~once a minute on a
    healthy table. Each false-fire re-armed CDP; that listener churn is what
    destabilised the WS stream and CAUSED the recurring ~35-min stream deaths.
    120s matches the dashboard GAP_S and never false-fires on a healthy table.
  * Every CDP call wrapped in asyncio.wait_for (15s). On 2026-08-11 a hung
    CDP session send froze the whole asyncio loop for ~26 min -> the 32.5-min
    gap. That can no longer happen.
  * Recovery ladder instead of a 4x-reload spiral:
      rung 0: passive wait 30s (stream often self-heals)
      rung 1: re-arm CDP only, wait 30s
      rung 2: click game refresh button (best effort), wait 30s
      rung 3: page reload, verify frames resume within 90s
      rung 4: full browser restart (fresh login), verify within 90s
      fail   : abandon session -> main loop starts a fresh session
    After every rung the loop VERIFIES a new spin arrived before escalating.
    The old path did 4 reloads x ~60s + abandon + re-login = ~5.8 min dead
    time per incident; the ladder resolves most stalls in under a minute.
  * DOM candidate dump when the refresh selector misses, so the next stall
    logs the real Evolution refresh control instead of guessing blindly.

v3 — 2026-08-15 integrity layer (observation store, no behaviour change):
  * Every raw capture (WS frame + DOM poll) is recorded in spin_observations
    (immutable) with session_id + payload_hash dedup, before canonical save.
  * collector_sessions + integrity_events audit trail (SESSION_START/END).
  * Canonical save path (INSERT OR IGNORE by gameId) is untouched; the
    integrity tables are additive (ensure_schema at startup).
  See docs/proposal-data-integrity.md (Phase 1).

Credentials are read from env vars SUNBET_USER/SUNBET_PASS, falling back to
~/.config/roulette2_collector.env (KEY=VALUE lines). NEVER committed — this
repo is public.
"""
import json, re, time, os, sys, asyncio, sqlite3
from collections import deque
from playwright.async_api import async_playwright
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ---- config ----
# All paths live under RC_DATA_DIR (default /home/wa on Linux, ~/.roulette2 on
# Windows). Individual overrides: RC_DB_PATH, RC_STATE_FILE, RC_CSV_FILE,
# RC_CRED_FILE, RC_HEARTBEAT_FILE, RC_GAME_URL.
def _data_dir() -> str:
    env = os.environ.get("RC_DATA_DIR")
    if env:
        return env
    if os.name == "nt":
        return os.path.join(os.path.expanduser("~"), ".roulette2")
    return "/home/wa"


_DATA_DIR = _data_dir()
os.makedirs(_DATA_DIR, exist_ok=True)

GAME_URL = os.environ.get(
    "RC_GAME_URL",
    "https://www.sunbet.co.za/en/play/auto-roulette",
)
LOGIN_URL = os.environ.get(
    "RC_LOGIN_URL", "https://www.sunbet.co.za/en/login"
)
# Evolution lobby table-key filter for Table 448 (Auto-Roulette R2).
# Confirmed 2026-08-24: key 48z5pjps3ntvqc1b is stable per table (matches
# user-observed numbers 6, 23 at capture start). Capture only this table.
LOBBY_TABLE = os.environ.get("RC_LOBBY_TABLE", "48z5pjps3ntvqc1b")
# Save batch size: flush to DB every N captured spins (default 25).
SAVE_EVERY = int(os.environ.get("RC_SAVE_EVERY", "25"))
STATE_FILE = os.environ.get("RC_STATE_FILE", os.path.join(_DATA_DIR, "roulette2_spins.json"))
CSV_FILE = os.environ.get("RC_CSV_FILE", os.path.join(_DATA_DIR, "roulette2_spins.csv"))
DB_FILE = os.environ.get("RC_DB_PATH", os.path.join(_DATA_DIR, "roulette2_spins.db"))
CRED_FILE = os.environ.get(
    "RC_CRED_FILE", os.path.join(_DATA_DIR, "roulette2_collector.env")
)
HEARTBEAT_FILE = os.environ.get(
    "RC_HEARTBEAT_FILE", os.path.join(_DATA_DIR, "roulette2_heartbeat.json")
)

# ---- integrity layer (v3) — additive, never blocks capture ----
os.environ.setdefault("RC_DB_PATH", DB_FILE)
try:                                     # repo layout (package context)
    from . import (observer, schema, reconciler, history, validator,
                   repairer, integrity_state)
except ImportError:                      # deployed flat script on the box
    # Import as collector.<mod> — NOT as bare top-level modules. The
    # integrity submodules import each other via `from collector.reconciler
    # import ...` (absolute, package-qualified); importing reconciler as a
    # top-level name here would load a SECOND module instance with its own
    # HistoryRecord class, and isinstance() checks in normalize_record
    # would fail across the two copies ('no attribute get' / 'cannot
    # normalize' crashes). PYTHONPATH=/opt/deploy/repos/RoulCollector448
    # puts the repo on sys.path, so `collector.reconciler` resolves to the
    # same file the absolute imports use.
    from collector import (observer, schema, reconciler, history, validator,
                           repairer, integrity_state)

# Reconcile cadence (PRD §31, §13): per-spin validation is instant; the
# rolling-window reconciliation (load local 500, obtain authoritative 500,
# match by identity, compare sequence/order, repair the affected suffix)
# runs every 30s (light) and 60s (full 500-window audit).
RECONCILE_LIGHT_S = 30
RECONCILE_FULL_S = 60
RECONCILE_WINDOW = 500
RECONCILE_FETCH_LIMIT = 2000  # obs fetch span — wider than the window so
# old-but-in-window obs rows (observed_at < window edge) are still visible
# to the walk; the reconcile then range-filters to the local span.

STALL_THRESHOLD_S = 60    # PROCESS/REALTIME health: user requires gap >= 1min
                         # to trigger refresh + backlog backfill (was 120)


def tail_slide(tail_nums, prev_tail_nums):
    """How many of the NEWEST tail entries are genuinely new spins.

    The lobby frame re-lists the last ~10 spins every few seconds; a new
    spin pushes the old tail down one slot. When a recovery BURST lands (K
    new spins in one frame), the old tail slides down K slots — the old
    per-slot / i-1 checks then misclassify EVERY entry as new and the gid
    counter inflates by the tail length per frame. This returns K = the
    position where the PREVIOUS newest re-appears (0 = pure re-listing,
    len(tail) = fully rotated). Falls back to prev_tail[1] as a 1-slot
    drift anchor; if the previous newest rolled off entirely the whole
    tail counts as new (it IS new — the gap exceeded the tail length).
    """
    if not prev_tail_nums:
        return len(tail_nums)
    prev_newest = prev_tail_nums[0]
    for k in range(len(tail_nums)):
        if tail_nums[k] == prev_newest:
            return k
        if k + 1 < len(prev_tail_nums) and tail_nums[k] == prev_tail_nums[1]:
            return k + 1
    return len(tail_nums)
                         # between spins; >120s means the capture stream is
                         # not flowing in realtime (a stall) — triggers the
                         # §30 recovery ladder. This is NOT a data-integrity
                         # signal: data health has its own cadence (30/60s
                         # reconcile, 5-min deep sweep) and its own signals
                         # (gaps, reconciliation health, repair queue).
                         # Process health (dead/hung/no-output) is the
                         # watchdog's job, separate from both.
RUNG_WAIT_S = 30          # wait for a new spin after rungs 0-2
RELOAD_VERIFY_S = 90      # frames must resume within this after a reload
RESTART_VERIFY_S = 90     # frames must resume within this after browser restart
CDP_TIMEOUT_S = 15        # hard timeout for every CDP call
SESSION_DURATION = 6 * 3600


def load_credentials():
    creds = {
        "SUNBET_USER": os.environ.get("SUNBET_USER"),
        "SUNBET_PASS": os.environ.get("SUNBET_PASS"),
    }
    if all(creds.values()):
        return creds
    try:
        with open(CRED_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    creds[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    if not all(creds.values()):
        sys.exit(f"FATAL: Sunbet credentials missing. Set SUNBET_USER/SUNBET_PASS "
                 f"env vars or create {CRED_FILE}")
    return creds


CREDS = load_credentials()
SUNBET_USER = CREDS["SUNBET_USER"]
SUNBET_PASS = CREDS["SUNBET_PASS"]

# European roulette number -> color
REDS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}


def num_to_color(n):
    if n == 0:
        return "Green"
    return "Red" if n in REDS else "Black"


def save_spins(spins):
    with open(STATE_FILE, "w") as f:
        json.dump(spins, f, indent=1)
    with open(CSV_FILE, "w") as f:
        f.write("number,description,gameId,timestamp,captured_at\n")
        for s in spins:
            desc = s.get("description", "").replace(",", "")
            f.write(f"{s.get('number','')},{desc},{s.get('gameId','')},{s.get('timestamp','')},{s.get('captured_at','')}\n")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Canonical table via the integrity schema (additive/idempotent) so the
    # storage-level dedup_key column + unique index exist before inserts.
    schema.ensure_schema(conn)
    inserted = 0
    no_identity = 0
    now_iso_ts = datetime.now(timezone.utc).isoformat()
    for s in spins:
        desc = s.get("description", "")
        color = num_to_color(s['number']) if isinstance(s['number'], int) else "Green"
        # Storage-level identity (PRD): game_id when valid, else ts+number.
        # None -> cannot dedup -> never inserted canonically (surfaced via
        # integrity event, kept in the JSON state file).
        dk = schema.canonical_dedup_key(
            s.get("gameId"), s.get("timestamp"), s.get("number"))
        if dk is None:
            no_identity += 1
            continue
        # PRD §18: three timestamps — server_ts (from the source),
        # observed_at (when the collector saw it — already captured_at),
        # committed_at (when THIS save commits it). capture_latency and
        # commit_latency are derived and stored for the latency tracker.
        server_ts = s.get("timestamp", "")
        observed_at = s.get("captured_at", "")
        committed_at = s.get("committed_at") or now_iso_ts
        capture_latency = _latency_seconds(server_ts, observed_at)
        commit_latency = _latency_seconds(observed_at, committed_at)
        # sequence_no = the lobby gid counter (true order) when present, so
        # the canonical sequence stays consistent with the obs band and the
        # dashboard's sequence-based gap detector. The blanket
        # "sequence_no = id WHERE NULL" fallback below (id-order) previously
        # cascaded the tail into the 200k range on any row inserted without
        # a sequence (2026-08-25 incident).
        seq = None
        m_seq = re.search(r"-(\d+)$", s.get("gameId", ""))
        if m_seq:
            seq = int(m_seq.group(1))
        try:
            c.execute('''
                INSERT OR IGNORE INTO roulette_spins
                    (number, description, color, game_id, server_ts,
                     captured_at, dedup_key, observed_at, committed_at,
                     capture_latency, commit_latency, sequence_no)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (int(s['number']), desc, color, s.get('gameId', ''),
                  server_ts, observed_at, dk, observed_at, committed_at,
                  capture_latency, commit_latency, seq))
            if c.rowcount:
                inserted += 1
        except Exception:
            pass
    # Canonical ordering (PRD §11): assign sequence_no for rows the integrity
    # layer never touched. Lobby rows get their gid counter (set in the
    # INSERT above); only counter-less legacy rows fall back to id order.
    try:
        c.execute("UPDATE roulette_spins SET sequence_no = id "
                  "WHERE sequence_no IS NULL")
    except Exception:
        pass
    conn.commit()
    conn.close()
    if no_identity:
        try:
            observer.log_event(
                schema.connect(), "SPIN_NO_IDENTITY", severity="WARNING",
                details={"count": no_identity,
                         "note": "no game_id/ts+number identity — kept as "
                                 "observations, not canonical"},
                root_cause="DATA",
            )
        except Exception:
            pass
    print(f"  Saved {len(spins)} spins to disk (+{inserted} new to DB, "
          f"{no_identity} without identity)")


# Capture-latency early-warning (PRD §19): an increasing P99 is a
# degradation signal BEFORE an outright stall. Defaults are generous for a
# real table (~sub-second capture); the collector's own cadence dominates.
LATENCY_P99_ALERT_S = 10.0

LATENCY_ALERT_COOLDOWN_S = 300  # don't spam the same alert


def _freshness_component(spins, now=None) -> float:
    """§22 capture-freshness component: 1.0 when a spin arrived within 3x
    cadence (3 min), decaying to 0.0 after 15 min of silence."""
    if not spins:
        return 0.0
    last = spins[-1].get("captured_at") or spins[-1].get("timestamp")
    if not last:
        return 0.0
    try:
        import datetime as _dt
        t = _dt.datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
        now = now or _dt.datetime.now(_dt.timezone.utc)
        age = (now - t).total_seconds()
    except Exception:
        return 0.0
    if age <= 180:
        return 1.0
    if age >= 900:
        return 0.0
    return 1.0 - (age - 180) / (900 - 180)


def _timestamp_component(spins, window: int = 50) -> float:
    """§22 timestamp-integrity component: 1 − fraction of recent spins with
    check_timestamp problems (unparseable, future skew, latency)."""
    if not spins:
        return 1.0
    bad = 0
    for sp in spins[-window:]:
        probs = validator.check_timestamp(
            sp.get("timestamp"), sp.get("captured_at"))
        if probs:
            bad += 1
    return 1.0 - bad / min(len(spins), window)


def _score_components(state, result, conn=None) -> dict:
    """Compute all six §22 components from a reconcile pass + live state.
    Pure-ish; any DB failure degrades a component to its neutral value."""
    comps = {
        "freshness": _freshness_component(state.get("spins") or []),
        "sequence": 1.0,
        "reconciliation": 0.0 if not result.ok else 1.0,
        "duplicates": 1.0,
        "timestamps": _timestamp_component(state.get("spins") or []),
        "source_agreement": 1.0,
    }
    # sequence continuity: 1 − (renumbered+reordered) / window
    plan = result.plan
    n_seq = len(plan.renumber) + len(plan.reorder)
    if n_seq:
        comps["sequence"] = max(0.0, 1.0 - n_seq / max(plan.window_achieved, 1))
    # duplicate/conflict: CONFLICT -> 0 (critical), else 1 − dup_rate
    if plan.duplicates:
        if "CONFLICT" in plan.duplicate_kinds.values():
            comps["duplicates"] = 0.0
        else:
            comps["duplicates"] = max(
                0.0, 1.0 - len(plan.duplicates) / max(plan.window_achieved, 1))
    # source agreement: real WS-vs-DOM ratio when pairs were checked, else
    # neutral 1.0 (absence of the second source is not disagreement).
    if conn is not None:
        try:
            from collector import source_agreement
            ag = source_agreement.verify_recent_agreement(conn, window=50)
            if ag.get("checked"):
                comps["source_agreement"] = ag.get("agreement_ratio", 1.0)
        except Exception:
            pass
    return comps


def _aware_utc(ts) -> str | None:
    """Normalize a server timestamp to aware-UTC ISO.

    The direct game WS frame carries a NAIVE timestamp in the site's local
    time (Africa/Johannesburg, UTC+2) — the same 2h skew that once triggered
    false "future skew" flags. Aware stamps pass through; empty/unparseable
    -> None (caller falls back to its own capture-time stamp).
    """
    try:
        if not ts:
            return None
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("Africa/Johannesburg"))
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def _latency_seconds(a, b) -> float | None:
    """Seconds from ISO timestamp a to b; None if unparseable/negative."""
    try:
        import datetime as _dt
        pa = _dt.datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        pb = _dt.datetime.fromisoformat(str(b).replace("Z", "+00:00"))
        if pa.tzinfo is None:
            pa = pa.replace(tzinfo=_dt.timezone.utc)
        if pb.tzinfo is None:
            pb = pb.replace(tzinfo=_dt.timezone.utc)
        d = (pb - pa).total_seconds()
        return d if d >= 0 else None
    except Exception:
        return None


def _spin_color(spin):
    """Color as OBSERVED by the source (from description text) when present,
    else derived from the number — never a false 'color missing' flag for
    captures that simply don't carry a color field."""
    d = spin.get("description") or ""
    for c in ("Red", "Black", "Green"):
        if c in d:
            return c
    n = spin.get("number")
    return num_to_color(n) if isinstance(n, int) else None


def check_observed_contradiction(spin):
    """PRD §17: flag a source observation whose color CONTRADICTS its number
    (e.g. '17 Red') instead of silently normalizing it. Returns a problem
    string or None. Never raises."""
    try:
        n = spin.get("number")
        if not isinstance(n, int):
            return None
        if not 0 <= n <= 36:
            return None
        obs = _spin_color(spin)
        if obs is None:
            return None
        expected = num_to_color(n)
        if obs != expected:
            return f"color contradiction: source observed {obs}, number {n} is {expected}"
    except Exception:
        return None
    return None


def validate_new_spin(state, spin, prev_spin):
    """Per-spin fast validation (PRD §31 — the fastest interval). Runs
    validate_spin on every new canonical spin; SUSPECT/INVALID problems are
    logged as integrity events (never silently passed). Feeds the capture
    latency tracker (PRD §19). Never blocks capture, never raises."""
    # PRD §19: feed the per-session capture-latency tracker on every spin.
    try:
        tr = state.get("latency_tracker")
        if tr is not None:
            tr.add(spin.get("timestamp"), spin.get("captured_at"),
                   spin.get("committed_at"))
    except Exception:
        pass
    try:
        res = validator.validate_spin(
            number=spin.get("number"),
            color=_spin_color(spin),
            game_id=spin.get("gameId"),
            server_ts=spin.get("timestamp"),
            observed_at=spin.get("captured_at"),
            committed_at=spin.get("committed_at"),
            prev_ts=prev_spin.get("timestamp") if prev_spin else None,
        )
    except Exception:
        return
    problems = res.get("problems") or []
    # PRD §17: source-observed color contradiction — flagged explicitly,
    # never normalized silently (the save path derives color from number,
    # but the contradiction must be an integrity event first).
    contra = check_observed_contradiction(spin)
    if contra:
        problems = problems + [contra]
        res["status"] = "INVALID" if res.get("status") == "INVALID" else "SUSPECT"
    if not problems:
        return
    sev = "CRITICAL" if res.get("status") == "INVALID" else "WARNING"
    try:
        observer.log_event(
            schema.connect(),
            "SPIN_INVALID" if sev == "CRITICAL" else "SPIN_SUSPECT",
            severity=sev,
            game_id=spin.get("gameId"),
            details={"problems": problems, "number": spin.get("number")},
            root_cause="DATA",
        )
    except Exception:
        pass
    state["validation_issues"] = state.get("validation_issues", 0) + 1


def flush_history_obs(state):
    """Persist buffered history observations (source='history') — module
    level so the WS frame handler's closure can reach it. History frames
    are rare (snapshots), so flush immediately; never blocks capture,
    never raises."""
    if not state.get("history_obs") or state.get("session_id") is None:
        state["history_obs"] = []
        return 0
    try:
        sconn = schema.connect()
        n = observer.flush_observations(sconn, state["history_obs"])
        sconn.close()
        return n
    except Exception as e:
        print(f"  [INTEGRITY] history obs flush failed: {e}")
        state["history_obs"] = []
        return 0


def write_heartbeat(state, spins):
    """Write the liveness heartbeat consumed by the dashboard.

    On Linux the dashboard tails journald; on Windows there is no journald,
    so this file IS the liveness signal plus the live-spin overlay source
    (PRD §36 collector-health row). Atomic replace, never raises.
    """
    recent = []
    base = len(spins)
    for i, s in enumerate(spins[-40:], start=1):
        n = s.get("number")
        color = s.get("color")
        if color is None and isinstance(n, int):
            color = num_to_color(n)
        recent.append({
            "time": str(s.get("captured_at", ""))[11:19],
            "n": base - len(spins[-40:]) + i,  # position in dataset (matches #N)
            "number": int(n) if isinstance(n, int) else n,
            "color": color,
        })
    hb = {
        "at": datetime.now(timezone.utc).isoformat(),
        "status": state.get("hb_status", "RUNNING"),
        "spins_count": len(spins),
        "ws_captured": state.get("ws_captured", 0),
        "validation_issues": state.get("validation_issues", 0),
        # PRD §19: rolling capture/commit latency percentiles — a rising
        # P99 is the early-warning that the collector is degrading.
        "latency": (state.get("latency_tracker") or validator.LatencyTracker()).stats(),
        "session_id": state.get("session_id"),
        "last_spin": spins[-1] if spins else None,
        "recent_spins": recent,
    }
    tmp = HEARTBEAT_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(hb, f)
        os.replace(tmp, HEARTBEAT_FILE)
    except Exception as e:
        print(f"  [HEARTBEAT] write failed: {e}")


# Refresh button selectors in the Evolution game UI (checked across all frames).
# The old list (data-role/aria-label/title only) never matched the real DOM —
# widened, plus a candidate dump on miss so the next stall teaches us the real
# control instead of guessing again.
REFRESH_SELECTORS = [
    '[data-role*="refresh" i]', '[data-role*="reload" i]', '[data-role*="restart" i]',
    'button[aria-label*="refresh" i]', 'button[aria-label*="reload" i]',
    '[title*="refresh" i]', '[title*="reload" i]',
    '[class*="refresh" i]', '[class*="reload" i]',
    'svg[title*="refresh" i]', 'svg[title*="reload" i]',
    'img[alt*="refresh" i]', 'img[alt*="reload" i]',
]


async def click_refresh_button(page):
    """Find the game's refresh/reload button in any frame and real-click it.
    Returns True if clicked, False if none found."""
    frames = [page] + list(page.frames)
    for f in frames:
        for sel in REFRESH_SELECTORS:
            try:
                loc = f.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=1000):
                    await loc.click(timeout=3000)
                    print(f"  [REFRESH] Clicked {sel}")
                    return True
            except Exception:
                pass
    return False


async def dump_refresh_candidates(page):
    """Log classes/aria/title of likely refresh controls for the next stall —
    self-learning diagnostics instead of blind guessing."""
    frames = [page] + list(page.frames)
    seen = set()
    for f in frames:
        for sel in ('[class*="refresh" i]', '[class*="reload" i]',
                    '[aria-label*="refresh" i]', '[aria-label*="reload" i]'):
            try:
                els = await f.query_selector_all(sel)
                for el in els[:4]:
                    info = await el.evaluate(
                        "e => ({tag: e.tagName, cls: (e.className||'').toString().slice(0,60), "
                        "aria: e.getAttribute('aria-label'), title: e.getAttribute('title'), "
                        "txt: (e.innerText||'').trim().slice(0,40)})")
                    key = str(info)
                    if key not in seen:
                        seen.add(key)
                        print(f"    candidate: {info}")
            except Exception:
                pass


# CDP WS interception state — re-armable across page reloads
CDP = {"session": None, "enabled": False}


async def cdp(fut, label, timeout_s=CDP_TIMEOUT_S):
    """Run a CDP call under a hard timeout. A hung send() once froze the whole
    loop for 26 min — every CDP call goes through here."""
    try:
        return await asyncio.wait_for(fut, timeout=timeout_s)
    except asyncio.TimeoutError:
        print(f"  [CDP] {label}: TIMEOUT after {timeout_s}s")
        return None
    except Exception as e:
        print(f"  [CDP] {label}: failed: {e}")
        return None


async def setup_cdp(page, context, on_frame):
    """(Re)attach CDP Network interception to the GAME IFRAME (evo-games).
    The game's WebSocket lives in the iframe target, not the top page. Never blocks."""
    try:
        if CDP["session"]:
            await cdp(CDP["session"].send("Network.disable"), "Network.disable")
            CDP["session"] = None
        # Find the game iframe (evo-games) and create a CDP session ON that frame.
        game_frame = None
        for f in page.frames:
            url = f.url or ""
            if "evo-games.com" in url or "greentube" in url or "gamehost" in url:
                game_frame = f
                break
        if game_frame is not None:
            sess = await cdp(context.new_cdp_session(game_frame), "new_cdp_session_iframe")
            if sess is not None:
                await cdp(sess.send("Network.enable"), "Network.enable_iframe")
                sess.on("Network.webSocketFrameReceived", on_frame)
                sess.on("Network.webSocketFrameSent", on_frame)
                CDP["session"] = sess
                CDP["enabled"] = True
                return True
        # Fallback: attach to the top page (may miss iframe WS but keeps old behaviour).
        sess = await cdp(context.new_cdp_session(page), "new_cdp_session")
        if sess is None:
            return False
        CDP["session"] = sess
        await cdp(sess.send("Network.enable"), "Network.enable")
        sess.on("Network.webSocketFrameReceived", on_frame)
        sess.on("Network.webSocketFrameSent", on_frame)
        CDP["enabled"] = True
        return True
    except Exception as e:
        CDP["enabled"] = False
        print(f"  [CDP] Setup failed: {e}")
        return False


def make_on_ws_frame(state):
    """Build the WS frame handler bound to a mutable state dict."""
    async def on_ws_frame(params):
        s = state
        for key in ["response", "request"]:
            frame = params.get(key, {})
            payload = frame.get("payloadData", "")
            if not payload or len(payload) < 20:
                continue
            try:
                data = json.loads(payload)
                # Signal C: buffer history-shaped frames (join snapshot /
                # periodic recent-results lists) — they carry game_id +
                # server_ts, the identity the reconciler needs for repair
                # authority. Never blocks capture; best-effort only.
                try:
                    recs = history.parse_history_frame(data)
                    if recs:
                        s["ws_history"].extend(recs)
                        # persist as observations (source='history') so the
                        # authority survives restarts; content-hash dedup
                        # keeps overlapping snapshots from duplicating rows
                        for rec in recs:
                            s["history_obs"].append({
                                "source": "history",
                                "session_id": s["session_id"],
                                "game_id": rec.game_id,
                                "number": rec.number,
                                "server_ts": rec.server_ts,
                                "raw_payload": payload[:500],
                                "sequence_hint": rec.order_hint,
                            })
                        flush_history_obs(s)
                except Exception:
                    pass
                # NEW (2026-08): Evolution lobby stream — capture the newest
                # spin per table when it changes. game_id is synthesized for
                # dedup; the table key is STABLE per table — Table 448
                # (Auto-Roulette R2) = 48z5pjps3ntvqc1b. Optional
                # RC_LOBBY_TABLE filter for when the key is known/locked.
                try:
                    lobby_recs = history.parse_lobby_history(data)
                    if lobby_recs:
                        # Persist the FULL tail for the tracked table as
                        # source='history' observations (the authority the
                        # reconciler needs to backfill gaps). The lobby
                        # frame carries ~10 recent spins — the backlog
                        # window that closes gaps after a stall.
                        # game_id is UNIQUE per spin instance: repeated
                        # numbers collide on lobby-<key>-<number>, which
                        # breaks reconcile matching — so append a per-table
                        # counter (lobby-<key>-<n>-<cnt>).
                        new_tail = []
                        gid_map = {}
                        try:
                            tail = history.parse_lobby_tail(data, LOBBY_TABLE)
                            if tail:
                                # NEW-SPIN detection. The lobby frame has no
                                # timestamps, so identity is the tail
                                # position slide: when K new spins land, the
                                # old tail slides down K slots. A current
                                # spin at pos i is a RE-OBSERVATION of a
                                # spin already stored if it matches the
                                # previous tail at pos i or i-1 (the 1-slot
                                # slide). Everything else is a genuinely new
                                # spin. Each new spin gets ONE stable gid
                                # (monotonic counter); re-observations are
                                # dropped so backfill can never duplicate.
                                # (A 2+ spin burst can under-capture; the
                                # reconciler backfills those — duplication
                                # is worse than a rare recoverable gap.)
                                prev_tail = s.setdefault("lobby_prev_tail", [])
                                new_tail = []
                                # SLIDE-ALIGNED new-spin detection (burst-robust,
                                # 2026-08-25). The old 1-slot check
                                # (pos i or i-1) failed on recovery BURSTS:
                                # when K new spins land in one frame the old
                                # tail slides down K slots, so every entry
                                # looked "new" every frame and the gid counter
                                # inflated (+13 in one burst on 2026-08-25,
                                # which then corrupted the canonical tail via
                                # backfill). Fix: find the slide K where the
                                # PREVIOUS newest re-appears in the current
                                # tail; positions 0..K-1 are genuinely new,
                                # positions >= K are re-listings. If the
                                # previous newest rolled off entirely (gap >
                                # tail length) the whole tail is new.
                                if prev_tail:
                                    slide_k = tail_slide([r.number for r in tail],
                                                         prev_tail)
                                    new_tail = tail[:slide_k]
                                else:
                                    new_tail = tail  # first frame
                                s["lobby_prev_tail"] = [rec.number for rec in tail]
                                cnt = s.setdefault("lobby_seq", {}).get(LOBBY_TABLE, 0)
                                gid_map = {}
                                for rec in new_tail:
                                    cnt += 1
                                    gid = f"{rec.game_id}-{cnt}"
                                    gid_map[id(rec)] = gid
                                    s["history_obs"].append({
                                        "source": "history",
                                        "session_id": s["session_id"],
                                        "game_id": gid,
                                        "number": rec.number,
                                        "server_ts": rec.server_ts,  # physical identity
                                        "raw_payload": payload[:500],
                                        "sequence_hint": rec.order_hint,
                                    })
                                if new_tail:
                                    s.setdefault("lobby_seq", {})[LOBBY_TABLE] = cnt
                                    s.setdefault("lobby_newest", {})[LOBBY_TABLE] = new_tail[0].number
                                    # newest observation's unique gid: the
                                    # FIRST new entry got cnt = start+1
                                    first_cnt = (cnt - len(new_tail) + 1)
                                    s.setdefault("lobby_newest_gid", {})[LOBBY_TABLE] = \
                                        f"{new_tail[0].game_id}-{first_cnt}"
                                    flush_history_obs(s)
                        except Exception:
                            pass
                        # canonical spins: consume the NEW-spin gids assigned
                        # by the obs store above (single source of identity,
                        # lobby-<key>-<n>-<cnt>). Each new physical spin gets
                        # appended once with its stable gid; re-observations
                        # were already filtered into new_tail.
                        for rec in new_tail:
                            parts = (rec.game_id or "").split("-")
                            table_key = parts[1] if len(parts) > 1 else rec.game_id
                            if LOBBY_TABLE and table_key != LOBBY_TABLE:
                                continue
                            uniq_gid = gid_map.get(id(rec))
                            if not uniq_gid:
                                # obs path didn't assign (shouldn't happen —
                                # same loop) — defensive fallback
                                continue
                            desc_full = f"{rec.number} {num_to_color(rec.number)} [lobby {table_key}]"
                            s["spins"].append({
                                "number": rec.number,
                                "description": desc_full,
                                "gameId": uniq_gid,
                                # Lobby frames carry NO server ts — stamp the
                                # OBSERVATION time as the truth, timezone-aware
                                # UTC (naive local stamps parse as UTC and
                                # trigger false "future skew" / stall flags).
                                "timestamp": rec.server_ts or datetime.now(timezone.utc).isoformat(),
                                "captured_at": datetime.now(timezone.utc).isoformat()
                            })
                            s["new_since_save"] += 1
                            s["ws_captured"] += 1
                            s["hb_status"] = "RUNNING"
                            total = len(s["spins"])
                            print(f"  [{datetime.now().strftime('%H:%M:%S')}] #{total}: {desc_full}")
                            prev_spin = s["spins"][-2] if len(s["spins"]) >= 2 else None
                            validate_new_spin(s, s["spins"][-1], prev_spin)
                            if s["new_since_save"] >= SAVE_EVERY:
                                try:
                                    save_spins(s["spins"])
                                    s["new_since_save"] = 0
                                    if s.get("flush_obs"):
                                        s["flush_obs"](s)
                                except Exception as se:
                                    print(f"  [SAVE] failed: {se}")
                except Exception:
                    pass
                # Auto Roulette message format:
                # {"args":{"code":1,"description":"1 Red","gameId":"...","timestamp":"..."}}
                args = data if isinstance(data, dict) and "code" in data else data.get("args", data)
                code = args.get("code", "")
                desc = args.get("description", "")
                game_id = args.get("gameId", "")
                ts = args.get("timestamp", args.get("time", ""))

                if game_id and code and game_id != s["last_game_id"]:
                    s["last_game_id"] = game_id
                    try:
                        number = int(code)
                    except Exception:
                        number = code
                    desc_full = desc if desc else f"{number} {num_to_color(number)}"
                    desc_full = " ".join(desc_full.split())
                    ts = _aware_utc(ts) or datetime.now(timezone.utc).isoformat()
                    s["spins"].append({
                        "number": number,
                        "description": desc_full,
                        "gameId": game_id,
                        "timestamp": ts,
                        "captured_at": datetime.now(timezone.utc).isoformat()
                    })
                    # v3: record the raw observation (immutable) — dedup by content
                    obs_ts = datetime.now(timezone.utc).isoformat()
                    s["obs_buffer"].append({
                        "source": "websocket",
                        "session_id": s["session_id"],
                        "game_id": game_id,
                        "number": number if isinstance(number, int) else None,
                        "description": desc_full,
                        "server_ts": ts or None,
                        "raw_payload": payload,
                        "sequence_hint": s["ws_captured"],
                        # PRD §18/§19: per-observation server->collector
                        # latency, computed at capture time.
                        "capture_latency": _latency_seconds(ts, obs_ts),
                    })
                    s["new_since_save"] += 1
                    s["ws_captured"] += 1
                    s["hb_status"] = "RUNNING"
                    total = len(s["spins"])
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] #{total}: {desc_full}")
                    # Per-spin fast validation (PRD §31) — SUSPECT/INVALID
                    # logged as integrity events; never blocks capture.
                    prev_spin = s["spins"][-2] if len(s["spins"]) >= 2 else None
                    validate_new_spin(s, s["spins"][-1], prev_spin)
                    # PRD §19: P99 capture-latency breach -> integrity event
                    # (cooldown-gated so it doesn't spam every 5s tick).
                    try:
                        tr = s.get("latency_tracker")
                        if tr is not None:
                            st = tr.stats()
                            p99 = st.get("p99")
                            now_t = time.time()
                            if (p99 is not None and p99 > LATENCY_P99_ALERT_S
                                    and now_t - s.get("latency_last_alert", 0)
                                    > LATENCY_ALERT_COOLDOWN_S):
                                s["latency_last_alert"] = now_t
                                observer.log_event(
                                    schema.connect(), "LATENCY_HIGH",
                                    severity="WARNING",
                                    details={"p99_s": round(p99, 2),
                                             "p95_s": st.get("p95"),
                                             "max_s": st.get("max")},
                                    root_cause="PERFORMANCE",
                                )
                    except Exception:
                        pass
                    write_heartbeat(s, s["spins"])

                    if s["new_since_save"] >= SAVE_EVERY:
                        save_spins(s["spins"])
                        s["new_since_save"] = 0
                        if s.get("flush_obs"):
                            s["flush_obs"](s)
            except Exception:
                pass
    return on_ws_frame


async def start_session(p, state, on_frame):
    """Fresh browser -> login -> game page -> CDP setup.
    Returns (browser, context, page). Raises on hard failure."""
    browser = await p.chromium.launch(
        channel="chrome", headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        locale="en-ZA",
        timezone_id="Africa/Johannesburg",
    )
    await context.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    page = await context.new_page()

    print("[1] Loading Sunbet login...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(8000)

    print("[2] Logging in...")
    try:
        username_input = page.get_by_label("Username", exact=True)
        pw_input = page.get_by_label("Password", exact=True)
        await username_input.fill(SUNBET_USER, timeout=10000)
        await pw_input.fill(SUNBET_PASS)
        try:
            await page.locator('button[type="submit"]').first.click(timeout=5000)
        except Exception:
            await page.keyboard.press("Enter")
        print("   Login submitted")
        await page.wait_for_timeout(10000)
    except Exception as e:
        print(f"   Login flow (may already be logged in): {e}")

    print("[3] Loading game URL...")
    await page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(10000)
    # SPA page: when logged in, the game iframe auto-loads (evo-games host).
    # If not, try clicking the game's Play button (contained MuiButton).
    try:
        play_btn = page.locator('button[type="button"]:has-text("Play"), button:has-text("PLAY")').first
        if await play_btn.is_visible(timeout=6000):
            await play_btn.click(timeout=6000)
            print("   PLAY clicked")
            await page.wait_for_timeout(12000)
    except Exception:
        print("   No PLAY button (may auto-load)")
    # Wait for the game iframe (evo-games / gamehost / greentube / evolution / #gameIframe)
    iframe_elem = None
    for _ in range(8):
        try:
            all_pages = context.pages
            for pg in all_pages:
                try:
                    fr = pg.locator('iframe[src*="evo-games"], iframe[src*="gamehost"], iframe[src*="greentube"], iframe[src*="evolution"], #gameIframe').first
                    if await fr.is_visible(timeout=2000):
                        iframe_elem = fr
                        page = pg
                        break
                except Exception:
                    pass
            if iframe_elem:
                break
        except Exception:
            pass
        await page.wait_for_timeout(5000)
    if not iframe_elem:
        raise RuntimeError("game iframe not found after SPA load")
    game_src = await iframe_elem.get_attribute('src')
    print(f"   Game src: {game_src[:100]}...")

    print("[4] Navigating to game page...")
    await page.goto(game_src, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)

    print("[5] Setting up CDP WS interception...")
    ok = await setup_cdp(page, context, on_frame)
    if ok:
        print("  [CDP] WS interception enabled ✓")
    return browser, context, page


async def collect_loop():
    print(f"[{datetime.now().isoformat()}] ===== Starting Roulette 2 session =====\n")
    spins = []
    last_game_id = None
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                spins = json.load(f)
            if spins:
                last_game_id = spins[-1]["gameId"]
            print(f"  Resuming from {len(spins)} existing spins (last gameId: {last_game_id})")
        except Exception:
            spins = []

    state = {"spins": spins, "last_game_id": last_game_id,
             "new_since_save": 0, "ws_captured": 0,
             "obs_buffer": [], "session_ok": False,
             # PRD §19: per-session capture-latency tracker (fed on every
             # spin; stats ride the heartbeat; P99 breach alerts).
             "latency_tracker": validator.LatencyTracker(),
             "latency_last_alert": 0.0,
             # Signal C: ring buffer of history-shaped WS frames (join
             # snapshot / periodic recent-results payloads). These carry
             # game_id + server_ts — the identity that gives the reconciler
             # repair authority (vs DOM text, numbers only). The records are
             # ALSO persisted to spin_observations (source='history') so the
             # authority survives restarts (DBHistoryProvider).
             "ws_history": deque(maxlen=600),
             "history_obs": []}

    # Seed the lobby unique-gid counter from the DB so restarts continue
    # the sequence (avoid UNIQUE(game_id) collisions with old rows).
    # NOTE: parse the trailing counter segment properly (r"-(\d+)$") — the
    # old MAX(CAST(substr(game_id,-5))) grabbed digits from the NUMBER too
    # (e.g. lobby-<key>-35-106 -> "5-106" -> 5), seeding a garbage base that
    # collided with existing counters and silently dropped new rows
    # (INSERT OR IGNORE on UNIQUE(game_id)).
    try:
        sconn = schema.connect()
        rows = sconn.execute(
            "SELECT game_id FROM spin_observations "
            "WHERE source='history' AND game_id LIKE ?",
            (f"lobby-{LOBBY_TABLE}-%-%",),
        ).fetchall()
        sconn.close()
        max_cnt = 0
        for (gid,) in rows:
            m = re.search(r"-(\d+)$", gid or "")
            if m:
                max_cnt = max(max_cnt, int(m.group(1)))
        if max_cnt:
            state.setdefault("lobby_seq", {})[LOBBY_TABLE] = max_cnt
            print(f"  Seeded lobby gid counter from DB: {max_cnt}")
    except Exception as e:
        print(f"  (counter seed skipped: {e})")

    # v3: integrity layer — schema + session. Failure degrades gracefully:
    # the canonical capture path continues without the audit trail.
    state["session_id"] = None
    try:
        sconn = schema.connect()
        schema.ensure_schema(sconn)
        state["session_id"] = observer.start_session(sconn, source="cdp-ws")
        observer.log_event(sconn, "SESSION_START",
                           details={"resumed_spins": len(spins),
                                    "last_game_id": last_game_id})
        sconn.close()
    except Exception as e:
        print(f"  [INTEGRITY] session init failed (continuing without it): {e}")

    on_frame = make_on_ws_frame(state)

    async with async_playwright() as p:
        browser, context, page = await start_session(p, state, on_frame)

        # ---- DOM polling fallback (read visible result) ----
        last_dom_result = None

        async def poll_dom():
            nonlocal last_dom_result
            try:
                selectors = [
                    '.number-win', '.win-number', '.result-number',
                    '[class*="number"][class*="win"]', '[class*="result"]',
                    '.last-result .number', '.current-result .number',
                    'div[class*="roulette-result"]', 'div[class*="last-number"]',
                    'div[class*="prev-number"]', '.bubble-number',
                    'span[class*="number"][class*="big"]',
                    'div[class*="value"]:has-text("Red"):has-text("Black")',
                ]
                for sel in selectors:
                    try:
                        el = await page.query_selector(sel)
                        if el:
                            text = await el.inner_text()
                            text = text.strip()
                            if text and text != last_dom_result:
                                last_dom_result = text
                                # v3: DOM observation (no game_id — number only)
                                m = re.search(r"\d+", text)
                                num = int(m.group()) if m and 0 <= int(m.group()) <= 36 else None
                                state["obs_buffer"].append({
                                    "source": "dom",
                                    "session_id": state["session_id"],
                                    "number": num,
                                    "description": text[:200],
                                    "raw_payload": f"{sel}: {text[:200]}",
                                })
                                print(f"  [DOM] Found element '{sel}': '{text}'")
                                return
                    except Exception:
                        pass
            except Exception:
                pass

        async def wait_for_new_spin(base_count, timeout_s, label):
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if len(state["spins"]) > base_count:
                    print(f"  [RECOVERY] {label}: new spin #{len(state['spins'])} — stream is back")
                    return True
                await asyncio.sleep(2)
            print(f"  [RECOVERY] {label}: no new spin within {timeout_s}s")
            return False

        async def post_recovery_backfill():
            """After the stream recovers, backfill gap spins from the tail
            authority + rebuild sequence_no. The post-recovery lobby frames
            carry the spins that fell during the outage (tail ~10), so the
            authority has them even though live capture missed them."""
            try:
                sconn = schema.connect()
                from scripts.backfill_gaps import backfill_gaps
                ins, skp = backfill_gaps(sconn, window=RECONCILE_WINDOW)
                # rebuild sequence_no from game_id counters (true order)
                import re as _re
                rows = sconn.execute(
                    "SELECT id, game_id FROM roulette_spins WHERE game_id LIKE ?",
                    (f"lobby-{LOBBY_TABLE}-%-%",)).fetchall()
                upd = 0
                for rid, gid in rows:
                    m = _re.search(r"-(\d+)$", gid or "")
                    if m:
                        sconn.execute("UPDATE roulette_spins SET sequence_no=? WHERE id=?",
                                      (int(m.group(1)), rid))
                        upd += 1
                sconn.commit()
                sconn.close()
                if ins:
                    print(f"  [RECOVERY] post-recovery backfill: +{ins} spins, {upd} sequenced")
            except Exception as e:
                print(f"  [RECOVERY] post-recovery backfill failed: {e}")

        async def recover(base_count):
            """PRD §30 self-healing escalation ladder — 9 rungs (0-8).

            The destructive recovery (WS re-arm, refresh, reload, browser
            restart) is now PRECEDED by data reconciliation (L2): a spin
            stream that silently gaps (process alive, journal active,
            sequence incomplete) is often a DATA problem, not a transport
            one — reconcile the rolling 500 from authoritative history and
            repair BEFORE tearing down the browser. Returns True if a new
            spin arrived OR the sequence was repaired.
            """
            nonlocal browser, context, page
            state["hb_status"] = "RECOVERING"
            # Reorder (2026-08-24): backfill FIRST from the page's own
            # lobby tail authority (non-destructive, no refresh needed),
            # then passive wait, then DOM, then destructive refresh as a
            # LAST resort (user: refresh "at least not often"). A gap is
            # usually a data problem (missed lobby update), not transport.
            print("  [RECOVERY] L0: backfill gaps from lobby tail authority — no refresh")
            try:
                sconn = schema.connect()
                from scripts.standalone_reconcile import recover_gaps
                outcomes = recover_gaps(sconn, window=RECONCILE_WINDOW)
                sconn.close()
                repaired = [o for o in outcomes if o["resolution"] == "REPAIRED"]
                print(f"    reconcile: {len(outcomes)} gap(s), {len(repaired)} repaired")
                if repaired:
                    await post_recovery_backfill()
                    return True
            except Exception as e:
                print(f"    reconcile failed: {e}")
            if await wait_for_new_spin(base_count, RUNG_WAIT_S, "after backfill"):
                await post_recovery_backfill()
                return True

            print("  [RECOVERY] L1: cross-check DOM — the secondary channel")
            await poll_dom()
            if await wait_for_new_spin(base_count, RUNG_WAIT_S, "after DOM cross-check"):
                await post_recovery_backfill()
                return True

            print("  [RECOVERY] L2: re-arm WebSocket (CDP interception)")
            await setup_cdp(page, context, on_frame)
            if await wait_for_new_spin(base_count, RUNG_WAIT_S, "after CDP re-arm"):
                await post_recovery_backfill()
                return True

            print("  [RECOVERY] L3: refresh game (best effort)")
            clicked = await click_refresh_button(page)
            if not clicked:
                print("    refresh button not found — dumping DOM candidates for next time")
                await dump_refresh_candidates(page)
            if await wait_for_new_spin(base_count, RUNG_WAIT_S, "after refresh click"):
                await post_recovery_backfill()
                return True

            print("  [RECOVERY] L4: reload page — verifying frames resume")
            try:
                await asyncio.wait_for(page.reload(wait_until="domcontentloaded"), timeout=60)
            except Exception as e:
                print(f"    reload failed: {e}")
            await page.wait_for_timeout(5000)
            await setup_cdp(page, context, on_frame)
            if await wait_for_new_spin(base_count, RELOAD_VERIFY_S, "after reload"):
                await post_recovery_backfill()
                return True

            print("  [RECOVERY] L5: restart browser (fresh session)")
            try:
                await browser.close()
            except Exception:
                pass
            browser, context, page = await start_session(p, state, on_frame)
            if await wait_for_new_spin(base_count, RESTART_VERIFY_S, "after browser restart"):
                await post_recovery_backfill()
                return True

            print("  [RECOVERY] L6: restart collector (full process restart)")
            state["hb_status"] = "ABANDONED"
            write_heartbeat(state, spins)
            state["session_ok"] = False
            return False

            # L8 (flag unresolved incident) is the CALLER's job: the stall
            # branch logs RECOVERY_FAILED + the watchdog escalates.

        print(f"[6] Monitoring — {len(spins)} spins so far\n")
        start_time = time.time()
        last_spin_count = len(spins)
        dom_poll_count = 0

        async def reconcile_task():
            """PRD §31/§53 — the reconcile -> verify loop, running alongside
            capture without ever blocking it (capture is non-blocking; this
            task only reads `state["spins"]` and writes integrity_events).

            Light pass every 30s (RECONCILE_LIGHT_S), full window audit
            every 60s (RECONCILE_FULL_S). The DOM history panel is the
            authoritative source; if it's unavailable the window is
            UNVERIFIED (never guessed, PRD §5/§25). Results land in
            integrity_events for the dashboard /api/integrity.
            """
            if state["session_id"] is None:
                return
            telemetry = integrity_state.Telemetry()
            health = integrity_state.DataHealthScore()
            state_machine = integrity_state.RecoveryStateMachine()
            last_light = time.time()
            last_full = 0.0
            while True:
                await asyncio.sleep(10)
                now = time.time()
                # light: RECONCILE_LIGHT_S (30s) cadence; full: RECONCILE_FULL_S (60s) cadence
                if now - last_light < RECONCILE_LIGHT_S and now - last_full < RECONCILE_FULL_S:
                    continue
                is_full = (now - last_full) >= RECONCILE_FULL_S
                telemetry.inc("reconciliations")
                try:
                    spins = list(state["spins"])[-RECONCILE_WINDOW:]
                    if not spins:
                        continue
                    # Signal C provider selection: WS-buffered history first
                    # (freshest, identity-bearing -> repair authority); then
                    # the durable DB history (survives restarts); DOM text
                    # last (numbers only -> detection only).
                    ws_recs = list(state.get("ws_history") or [])[-RECONCILE_WINDOW:]
                    if ws_recs:
                        provider = history.WSHistoryProvider(ws_recs)
                        remote = provider.fetch_recent_history(limit=RECONCILE_WINDOW)
                        history_source = "ws"
                    else:
                        try:
                            dbrecs = history.DBHistoryProvider().fetch_recent_history(
                                limit=RECONCILE_FETCH_LIMIT)
                        except Exception:
                            dbrecs = []
                        if dbrecs:
                            remote = dbrecs
                            history_source = "db-history"
                        else:
                            provider = history.DOMHistoryProvider(page, max_window=RECONCILE_WINDOW)
                            remote = await provider.fetch_recent_history_async(limit=RECONCILE_WINDOW)
                            history_source = "dom"
                    local = [{"game_id": s.get("gameId"), "number": s["number"],
                              "server_ts": s.get("timestamp")} for s in spins]
                    # Align the authority window to the LOCAL span. Obs rows
                    # can run AHEAD of state spins (phantom counters from
                    # recovery bursts / re-observation), and obs rows for
                    # OLD in-window spins sit outside a blind last-500 fetch
                    # (observed_at order). A misaligned window makes every
                    # pass report missing+extras forever and ok can never be
                    # true (score stuck at 65). Fetch a generous obs span and
                    # keep only records whose counter falls in [local_min,
                    # local_max]; counter-less records (legacy/dom) are kept.
                    def _gid_counter(gid):
                        m = re.search(r"-(\d+)$", gid or "")
                        return int(m.group(1)) if m else None
                    local_min_cnt = None
                    local_max_cnt = None
                    for _s in local:
                        _c = _gid_counter(_s.get("game_id"))
                        if _c is not None:
                            if local_min_cnt is None or _c < local_min_cnt:
                                local_min_cnt = _c
                            if local_max_cnt is None or _c > local_max_cnt:
                                local_max_cnt = _c
                    if local_min_cnt is not None and local_max_cnt is not None:
                        _aligned = []
                        for _r in remote:
                            _rc = _gid_counter(getattr(_r, "game_id", None))
                            if _rc is None or (local_min_cnt <= _rc <= local_max_cnt):
                                _aligned.append(_r)
                        if _aligned:
                            # Sort the authority by gid counter DESC (newest
                            # first) — obs rows from bursts share identical
                            # observed_at, so the provider's observed_at order
                            # returns ties arbitrarily (781 counter descents on
                            # 2026-08-25) and the walk sees phantom reorders.
                            # Counter order is the true order; counter-less
                            # legacy records trail at the end.
                            def _rc_key(_r):
                                _c = _gid_counter(getattr(_r, "game_id", None))
                                return _c if _c is not None else -1
                            _aligned.sort(key=_rc_key, reverse=True)
                            remote = _aligned
                    result = reconciler.reconcile(local, history.StaticHistoryProvider(remote),
                                                  window=RECONCILE_WINDOW)
                    # Phase 4: apply deterministic repairs only when the
                    # authority is solid AND carries identity (PRD §24, §5 —
                    # never manufacture identity from number-only history).
                    # Reconcile -> repair -> VERIFY: the repair is only
                    # success once re-validation passes.
                    repaired = None
                    if result.plan.repairable and not result.ok:
                        # PRD §16: surface duplicate incidents — CONFLICT is
                        # CRITICAL (never silent), EXACT/TS_MISMATCH WARNING.
                        for inc in reconciler.duplicate_incidents(result.plan):
                            observer.log_event(
                                schema.connect(), "DUPLICATE",
                                severity=inc["severity"],
                                game_id=inc["game_id"],
                                details={"kind": inc["kind"]},
                                root_cause="DATA",
                            )
                        try:
                            sconn = schema.connect()
                            rep = repairer.Repairer(sconn)
                            repaired = rep.apply_plan(result.plan)
                            sconn.close()
                            telemetry.inc("repairs")
                        except repairer.RepairRefused as e:
                            # PRD §25: refused (no authority / no identity /
                            # conflicting sources) — surfaced, never silent.
                            observer.log_event(schema.connect(),
                                               "REPAIR_REFUSED", severity="WARNING",
                                               details={"reason": str(e)},
                                               root_cause="DATA_INTEGRITY")
                        except Exception as e:
                            observer.log_event(schema.connect(),
                                               "REPAIR_FAILED", severity="CRITICAL",
                                               details={"error": str(e)},
                                               root_cause="DATA_INTEGRITY")
                    if not result.ok:
                        telemetry.inc("reconciliation_failures")
                    state_machine.observe(reconciled_ok=result.ok,
                                          repairing=repaired is not None)
                    # §22: feed ALL six components from real per-pass data —
                    # freshness (spin recency), sequence (renumber/reorder),
                    # reconciliation, duplicates (kinds), timestamps
                    # (check_timestamp rate), source_agreement (WS-vs-DOM).
                    sconn2 = schema.connect()
                    try:
                        comps = _score_components(state, result, conn=sconn2)
                    finally:
                        sconn2.close()
                    score = health.compute(**comps)
                    sev = "CRITICAL" if not result.ok and result.plan.authoritative else "INFO"
                    observer.log_event(
                        schema.connect(),
                        "RECONCILIATION" if is_full else "RECONCILIATION_LIGHT",
                        severity=sev,
                        details={
                            "ok": result.ok,
                            "window": result.window,
                            "window_achieved": result.plan.window_achieved,
                            "missing": result.missing_count,
                            "corrections": result.correction_count,
                            "duplicates": result.duplicate_count,
                            "reordered": result.reorder_count,
                            "extras": result.extra_count,
                            "repairable": result.repairable,
                            "history_source": history_source,
                            "authoritative": result.plan.authoritative,
                            "message": result.message,
                            "repaired": repaired,
                            "state": state_machine.state,
                            "score": score,
                            "score_components": comps,
                            "telemetry": telemetry.snapshot(),
                        },
                    )
                    if is_full:
                        last_full = now
                    last_light = now
                except Exception as e:
                    # HARDENED: a reconcile-task error must NEVER kill the
                    # task (it died silently for hours on 2026-08-25 — events
                    # froze at 15:12 while capture continued). Log traceback
                    # to journald AND an event; keep the cadence moving.
                    import traceback as _tb
                    _tb_s = _tb.format_exc()
                    try:
                        print(f"  [RECONCILE] task error: {e}\n{_tb_s}")
                    except Exception:
                        pass
                    try:
                        observer.log_event(schema.connect(), "RECONCILIATION_FAILED",
                                           severity="WARNING",
                                           details={"error": str(e),
                                                    "traceback": _tb_s[-2000:]},
                                           root_cause="DATA_INTEGRITY")
                    except Exception:
                        pass
                    finally:
                        last_light = now

        task = asyncio.create_task(reconcile_task())

        def flush_obs(s):
            """Batch-persist buffered observations. Never raises — the
            canonical capture path must not depend on the audit trail."""
            if not s["obs_buffer"] or s["session_id"] is None:
                s["obs_buffer"].clear()
                return 0
            try:
                sconn = schema.connect()
                n = observer.flush_observations(sconn, s["obs_buffer"])
                sconn.close()
                return n
            except Exception as e:
                print(f"  [INTEGRITY] obs flush failed: {e}")
                s["obs_buffer"].clear()
                return 0

        state["flush_obs"] = flush_obs

        try:
            while (time.time() - start_time) < SESSION_DURATION:
                await asyncio.sleep(5)
                elapsed = time.time() - start_time

                # DOM poll every 15s while the WS has never produced anything
                if state["ws_captured"] == 0 and len(spins) == 0:
                    dom_poll_count += 1
                    if dom_poll_count % 3 == 0:
                        await poll_dom()

                # Stall detection — silence beyond the legit cadence envelope.
                # 120s threshold: no false fires (max legit ~57s), catches real
                # stream deaths promptly. Also covers a fresh session that never
                # produced its first spin (bootstrap watchdog after 4 min).
                if spins:
                    last_spin_time = spins[-1]["captured_at"]
                    try:
                        _lst = datetime.fromisoformat(last_spin_time)
                        if _lst.tzinfo is None:
                            _lst = _lst.replace(tzinfo=timezone.utc)
                        sec_since_last = (datetime.now(timezone.utc) - _lst).total_seconds()
                    except Exception:
                        sec_since_last = elapsed
                else:
                    sec_since_last = elapsed
                if sec_since_last > STALL_THRESHOLD_S and (spins or elapsed > 240):
                    state["hb_status"] = "STALLED"
                    write_heartbeat(state, spins)
                    print(f"  STALLED — {sec_since_last:.0f}s since last spin. Running recovery ladder...")
                    ok = await recover(len(spins))
                    if not ok:
                        state["hb_status"] = "ABANDONED"
                        write_heartbeat(state, spins)
                        print("  Recovery ladder failed — stream still silent after full restart. Abandoning session.")
                        state["session_ok"] = False
                        break

                # Liveness heartbeat every tick (Windows dashboard liveness
                # source; Linux journald remains authoritative there).
                write_heartbeat(state, spins)

                # Status every 60s
                if int(elapsed) % 60 < 6:
                    rate = len(spins) / (elapsed / 3600) if elapsed > 0 else 0
                    print(f"  Status: {len(spins)} spins (WS: {state['ws_captured']}), {elapsed/60:.0f}m elapsed, {rate:.1f}/hr")

                if len(spins) > last_spin_count:
                    last_spin_count = len(spins)

            save_spins(spins)
            hours = (time.time() - start_time) / 3600
            print(f"  Session ended after {hours:.1f}h — {len(spins)} spins total")
            state["session_ok"] = True
        finally:
            task.cancel()
            # v3: flush observations + close the session (ENDED vs CRASHED)
            try:
                if state["session_id"]:
                    sconn = schema.connect()
                    flush_obs(state)
                    observer.end_session(
                        sconn, state["session_id"],
                        spins_captured=len(spins),
                        status="ENDED" if state["session_ok"] else "CRASHED",
                    )
                    observer.log_event(sconn, "SESSION_END",
                                       details={"total": len(spins),
                                                "ws": state["ws_captured"],
                                                "ok": state["session_ok"]})
                    sconn.close()
            except Exception as e:
                print(f"  [INTEGRITY] session end failed: {e}")
        await browser.close()
        return spins


async def main():
    print(f"[{datetime.now().isoformat()}] Roulette 2 Collector v2 (reliability ladder) — 24/7 MODE")
    while True:
        try:
            await collect_loop()
            print("  Session complete, starting new session in 10s...")
            await asyncio.sleep(10)
        except Exception as e:
            print(f"  SESSION CRASHED: {e}")
            print("  Restarting in 30s...")
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
