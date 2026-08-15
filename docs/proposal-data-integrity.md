# Proposal — Data Integrity, Detection & Self-Healing Pipeline (Table 448)

Status: PROPOSAL (Phase B — awaiting approval)
Date: 2026-08-15
Source: PRD — RoulCollector448 Data Integrity, Detection & Self-Healing System
Build protocol: structured-build-protocol (proposal-first, test-per-phase, commit-per-phase)

---

## 1. Context

The collector currently does `capture → append → save`. This proposal turns it into
`capture → observe → validate → canonicalize → persist → reconcile → verify`, adding an
immutable raw-observation store, an integrity engine, a rolling-window reconciler against
the site's available recent history, and deterministic atomic repair. A missing spin must
never be reconstructed from probability; unverifiable records stay UNVERIFIED.

## 2. Current-State Assessment (Phase A findings)

| Area | Existing | Gap vs PRD |
|---|---|---|
| Capture | CDP WS interception + DOM fallback; 15s CDP timeouts; 120s stall threshold; recovery ladder rungs 0-4; 6h sessions | No per-observation provenance; WS/DOM not cross-validated |
| Persistence | `roulette_spins` (id, number, description, color, game_id UNIQUE, server_ts, captured_at, created_at); INSERT OR IGNORE dedup; batches of 25; JSON+CSV | No raw store, no sequence_no, no status/confidence/provenance |
| Audit | `/api/audit` with AUDIT_WINDOW=500 (statistical drift vs all-time); journald live-merge for uncommitted spins | Audit is stats-focused, not integrity-focused; no verification of completeness |
| Watchdog | systemctl is-active + journald silence (12 min) | Process-only; does not see data health |
| API | /api/health, /api/spins, /api/stats/*, /api/audit, /api/transitions (read-only, port 4480) | No /api/integrity* |
| Tests | 16 pytest (conftest builds fixture DB, 1110 spins) | No integrity/reconciliation/repair/chaos coverage |

## 3. Critical Design Question — Authoritative History Availability

The PRD's central mechanism (rolling-500 reconciliation) depends on a recoverable recent
history. Evidence from `docs/collector-reliability.md`: the game page's DOM history panel
shows only the last ~25 results. The 500-spin window must therefore be treated as an
*aspiration with graceful degradation*.

**Decision:** introduce a `HistoryProvider` abstraction. At session start the collector
probes every available source and records the achieved window:

1. **DOM history panel** — scrape the game's recent-results UI (empirically ~25 today;
   may grow if a fuller history tab is found).
2. **WS join snapshot** — Evolution typically pushes recent-result snapshots on
   connect; intercept and buffer them (may be larger than the DOM panel).
3. **REST/API endpoint** — if discovered via network inspection, preferred source.

`RECOVERY_WINDOW = 500` stays the target; `effective_window = min(500, provider capacity)`.
All integrity reporting includes `window_achieved` so the dashboard never overclaims.
The reconciler window is the effective window.

## 4. Data Model (SQLite, additive migration)

### 4.1 New table — collector_sessions
```sql
CREATE TABLE IF NOT EXISTS collector_sessions (
    id             TEXT PRIMARY KEY,            -- e.g. 2026-08-15T04:32:01Z-7f92
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    status         TEXT NOT NULL DEFAULT 'ACTIVE',   -- ACTIVE|ENDED|CRASHED
    spins_captured INTEGER DEFAULT 0,
    source         TEXT,                             -- cdp-ws|dom|mixed
    window_achieved INTEGER                          -- effective recovery window seen
);
```

### 4.2 New table — spin_observations (immutable raw evidence)
```sql
CREATE TABLE IF NOT EXISTS spin_observations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at       TEXT NOT NULL,
    source            TEXT NOT NULL CHECK(source IN ('websocket','dom','history','reconciled','backfilled','manual')),
    session_id        TEXT NOT NULL,
    game_id           TEXT,
    number            INTEGER,
    description       TEXT,
    server_ts         TEXT,
    payload_hash      TEXT NOT NULL,          -- dedup of raw payloads across restarts
    raw_payload       TEXT,                   -- original JSON frame / DOM text
    sequence_hint     INTEGER,                -- WS counter or DOM order when available
    validation_status TEXT NOT NULL DEFAULT 'PENDING'  -- PENDING|VALID|SUSPECT|INVALID|CONFLICT
);
CREATE INDEX IF NOT EXISTS idx_obs_game ON spin_observations(game_id);
CREATE INDEX IF NOT EXISTS idx_obs_ts   ON spin_observations(observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_hash ON spin_observations(payload_hash);
```
Raw observations are NEVER deleted or mutated. Repairs change canonical data only.

### 4.3 Expanded canonical — roulette_spins (ALTER TABLE on live DB)
```sql
ALTER TABLE roulette_spins ADD COLUMN sequence_no INTEGER;          -- canonical position
ALTER TABLE roulette_spins ADD COLUMN source TEXT DEFAULT 'websocket';
ALTER TABLE roulette_spins ADD COLUMN confidence REAL DEFAULT 0.95; -- provenance metadata, not permission to alter outcomes
ALTER TABLE roulette_spins ADD COLUMN status TEXT DEFAULT 'VALID';  -- VALID|SUSPECT|REPAIRED|UNVERIFIED|QUARANTINED
ALTER TABLE roulette_spins ADD COLUMN first_seen_at TEXT;
ALTER TABLE roulette_spins ADD COLUMN last_verified_at TEXT;
ALTER TABLE roulette_spins ADD COLUMN verification_version INTEGER DEFAULT 0;
```
Additive and safe on the live DB (no data rewrite). New installs get the full DDL.

### 4.4 New table — integrity_events (audit of detections & actions)
```sql
CREATE TABLE IF NOT EXISTS integrity_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,   -- MISSING_SPIN|DUPLICATE|CONFLICT|OUT_OF_ORDER|TIMESTAMP_ANOMALY|
                                -- INVALID_NUMBER|INVALID_COLOR|SOURCE_DISAGREEMENT|RECONCILIATION|
                                -- GAP|SESSION_START|SESSION_END|RECOVERY_*
    severity   TEXT NOT NULL DEFAULT 'INFO',   -- INFO|WARNING|CRITICAL
    game_id    TEXT,
    details    TEXT,
    root_cause TEXT             -- NETWORK|CDP|BROWSER|SUNBET|EVOLUTION|DOM|WEBSOCKET|
                                -- AUTHENTICATION|PROCESS|STORAGE|PARSING|DATA_INTEGRITY|UNKNOWN
);
CREATE INDEX IF NOT EXISTS idx_ie_ts ON integrity_events(created_at);
CREATE INDEX IF NOT EXISTS idx_ie_type ON integrity_events(event_type);
```

### 4.5 New table — repair_events (repair queue + audit trail)
```sql
CREATE TABLE IF NOT EXISTS repair_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    incident_type  TEXT NOT NULL,   -- MISSING_SPIN|DUPLICATE|CONFLICT|OUT_OF_ORDER|TIMESTAMP_ANOMALY|SOURCE_DISAGREEMENT
    start_game_id  TEXT,
    end_game_id    TEXT,
    affected_count INTEGER,
    status         TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN|IN_PROGRESS|RESOLVED|FAILED|UNRESOLVED
    attempts       INTEGER DEFAULT 0,
    last_attempt_at TEXT,
    resolved_at    TEXT,
    resolution     TEXT,            -- BACKFILLED|COLLAPSED|CORRECTED|REORDERED|UNRESOLVED
    details        TEXT             -- JSON: old/new values, evidence source, reason, verification result
);
CREATE INDEX IF NOT EXISTS idx_re_status ON repair_events(status);
```

## 5. Component Design (new files)

```
collector/
  roulette2_collector.py   (existing — gains observation writes + task wiring)
  observer.py              capture → spin_observations (immutable), payload_hash dedup
  validator.py             per-spin validation + statuses + latency metrics
  history.py               HistoryProvider: DOM panel / WS snapshot / REST (probe order)
  reconciler.py            rolling-window compare local vs authoritative history
  repairer.py              deterministic atomic repairs (BEGIN/COMMIT/ROLLBACK)
  integrity_state.py       state machine (HEALTHY/SUSPECT/DEGRADED/RECONCILING/RECOVERING),
                           data-health score, telemetry counters
backend/
  integrity.py             read models for /api/integrity* (read-only over collector DB)
tests/
  test_integrity.py        validator/state/score
  test_reconciliation.py   reconciler with fixture histories
  test_repair.py           repairer atomicity + rules
  test_capture_failures.py WS-loss, DOM-loss, disagreement, restart
  test_chaos.py            fault-injection harness (final == authoritative fixture)
```

**Threading/concurrency:** the reconciler runs as an isolated background asyncio task in
the collector process (it must share the browser for history fetches; a second process
would fight over the Sunbet login). All browser calls go through the existing 15s `cdp()`
wrapper. SQLite stays sync + WAL (same as today's `save_spins`) with short transactions,
so no async-in-thread hazards. Repairs serialize behind a lock; capture appends are
INSERT OR IGNORE and never conflict with repair transactions.

**Scheduling (PRD §31):**
- Every new spin → validate immediately (cheap, in capture path)
- Every 45s → lightweight rolling reconciliation (freshness + tail-match)
- Every 5 min → full effective-window integrity audit
- Reconciliation pauses while the recovery ladder is mid-escalation

## 6. Validation Rules (validator.py)

| Check | Rule | Outcome |
|---|---|---|
| Number range | 0..36 int | INVALID_NUMBER event, SUSPECT |
| Color | color == num_to_color(number) | INVALID_COLOR event, SUSPECT (never silently fix) |
| Game ID | present; UNIQUE (existing constraint) | missing → SUSPECT; dupe → DUPLICATE event |
| Conflicting dupe | same game_id, different number | CONFLICT event, both observations kept |
| Timestamp | server_ts parseable; not >5min future; not impossibly old | TIMESTAMP_ANOMALY, SUSPECT |
| Capture latency | observed_at − server_ts | rolling P50/P95/P99/max; WARNING on rising P99 |
| Sequence | if game_ids monotonic (probed at session start): continuity; else cadence (0-70 normal, 70-119 suspicious, ≥120 gap) | OUT_OF_ORDER / MISSING_SPIN triggers |
| Source agreement | WS vs DOM for same game_id | VERIFIED (match) / SOURCE_DISAGREEMENT (mismatch) |

## 7. Reconciliation Algorithm (reconciler.py)

```
1. local  = last effective_window canonical spins (newest first)
2. remote = history_provider.fetch_recent_history()  (newest first)
3. normalize both → {game_id?, number, ts}
4. match by strongest identity available:
     game_id  →  ts+number  →  tail position  →  ts+neighbouring context
5. walk from the tail (newest) backwards to the first mismatch
6. classify: missing / wrong-value / extra-local / ordering
7. build repair plan (deterministic corrections only)
8. apply atomically via repairer; re-run validation; mark window verified
9. remote unavailable → window stays UNVERIFIED (never guess)
```

## 8. Repair Rules (repairer.py)

Auto-repair (authoritative identity required):
- MISSING_SPIN — remote has it, local doesn't → insert canonical + backfilled observation
- CONFLICT / wrong value — same game_id, remote authoritative → replace canonical value,
  keep BOTH original observations
- DUPLICATE — collapse to one canonical record, retain both observations
- OUT_OF_ORDER — rebuild sequence_no for the affected window

Never auto-repair (→ UNVERIFIED + surfaced):
- authoritative source unavailable
- multiple conflicting sources
- identity cannot be established
- statistical inference of any kind

Every repair is one atomic transaction (`BEGIN…COMMIT`, `ROLLBACK` on any failure) and
writes a repair_events row with old/new values, evidence source, reason, attempts,
verification result (PRD §35 audit log).

## 9. State Machine & Data-Health Score (integrity_state.py)

States: HEALTHY → SUSPECT → DEGRADED → RECONCILING → (success → HEALTHY | failure →
RECOVERING → RECONCILING). Process health and data health tracked separately (a browser
can be healthy while data is broken).

Score (0-100), weights per PRD §22:
freshness 20 · sequence continuity 25 · reconciliation match 25 · dupe/conflict 10 ·
timestamp integrity 10 · source agreement 10.
Bands: 100-98 VERIFIED · 97-95 HEALTHY · 94-90 DEGRADED · 89-75 WARNING · <75 CRITICAL.
Thresholds configurable via env.

Telemetry counters (per session + rolling): ws_frames_received, ws_spin_candidates,
ws_spins_accepted, ws_invalid_frames, dom_polls, dom_candidates, dom_spins_detected,
duplicates, conflicts, repairs, reconciliations, reconciliation_failures.

## 10. API (backend/integrity.py — read-only, same DB pattern as today)

- `GET /api/integrity` — score, status band, state, window + window_achieved, verified/
  missing/duplicates/conflicts/unverified/repaired counts, last_reconciliation,
  last_repair, per-source health (WS/DOM/SQLite), telemetry snapshot
- `GET /api/integrity/window` — effective-window verification detail (repaired/unverified
  spin lists, current gap)
- `GET /api/integrity/incidents` — last 20 integrity_events
- `GET /api/integrity/repairs` — last 20 repair_events
- `GET /api/integrity/health` — watchdog-facing summary (collector_alive, last_spin_age,
  sequence_health, reconciliation_health, repair_queue, data_health_score)

## 11. Dashboard (frontend)

New permanent top panel "DATA INTEGRITY": score bar with band colour, `N/N verified`,
missing/duplicates/conflicts/unverified/repaired counts, latest spin, last reconciliation,
last repair, open incidents, WS/DOM/SQLite health dots. New incidents panel: last 20
(Time | Type | Affected | Action | Result). Gap markers distinguish 🔧 repaired vs
⚠️ unverified. `/api/audit` stays untouched.

## 12. Watchdog Upgrade (collector/watchdog.py)

Add a data-health stage after the process checks: read the collector DB read-only —
if `data_health_score < 75` OR open repair_events older than 10 min OR
`last_reconciliation` older than 10 min → restart/alert with root-cause classification.
Watchdog now evaluates: collector_alive, last_spin_age, sequence_health,
reconciliation_health, repair_queue, data_health_score.

## 13. Performance

- Capture path: validation adds <1ms (pure in-memory checks); observation insert is a
  second prepared INSERT per spin.
- Lightweight reconciliation: <1s; full audit: <5s (indexed scans over 500-2000 rows).
- All reconciliation in the background task; capture never blocked (historical CDP-hang
  freeze is already prevented by the 15s `cdp()` wrapper; reconciler uses the same wrapper).
- SQLite WAL + busy_timeout=5000 + short transactions (existing pattern).

## 14. Risk Analysis

| Risk | Mitigation |
|---|---|
| History < 500 available | effective_window reported; reconcile what exists; DOM+WS-snapshot probe at session start |
| game_ids not monotonic | probe monotonicity; fall back to cadence + tail-position matching |
| Repair races capture | WAL; serialized repair lock; INSERT OR IGNORE appends never conflict with repair txns |
| False repairs | deterministic rules only; anything uncertain → UNVERIFIED, surfaced |
| Restart replay of observations | payload_hash + session_id dedup; canonical INSERT OR IGNORE unchanged |
| Collector/DB schema drift on live box | additive migration (CREATE IF NOT EXISTS / ALTER ADD COLUMN); rollback = stop collector, revert code |
| Reconciler browser use during recovery | reconciliation pauses while recovery ladder escalates |
| Observation table growth | ~2-3x canonical volume; retention 30 days (configurable); indexed |

## 15. Implementation Phases (each independently testable & committable)

| Phase | Scope | Tests |
|---|---|---|
| 1 | Schema (4.1-4.5) + sessions + observation store; collector records every raw observation; canonical behaviour unchanged | schema roundtrip, session lifecycle, payload_hash dedup, restart replay |
| 2 | Validator + integrity_events + latency metrics (P50/P95/P99/max) + statuses | every failure class (bad number, bad color, dupe, conflict, ts anomaly, cadence) |
| 3 | HistoryProvider (DOM/WS-snapshot probe) + reconciler (45s light, 5min full) | fixture histories: missing, wrong, extra, order, remote-unavailable |
| 4 | Repairer — atomic backfill/correct/collapse/reorder + repair_events + audit trail + re-verify | each repair type; transaction rollback on injected failure |
| 5 | Cross-source agreement (WS vs DOM) + telemetry counters + recovery state machine | disagreement, ws-loss, dom-loss, restart scenarios |
| 6 | /api/integrity* + dashboard panel + incidents panel + watchdog data-health + health score | API shapes against fixture DB, watchdog thresholds |
| 7 | Chaos harness: inject missing/dupe/delayed/conflict/reload/SQLite-failure → final == authoritative fixture | full chaos suite |

Effort estimate: Phases 1-4 are the P0 core (observation store, validator, reconciler,
repairer, integrity API). Phases 5-7 are P1/P2 hardening.

## 16. Definition of Done (operational target, PRD §53)

04:15:00 WS silently misses 3 spins → 04:18:05 reconciliation finds 3 missing →
04:18:07 backfilled from authoritative history → 04:18:08 verified effective-window
100% → 04:18:09 incident marked REPAIRED. User-visible: dataset complete, collector
healthy, history verified, incident auto-repaired. Where the window can't be verified,
the dashboard says so explicitly.

---

Awaiting approval before any implementation code is written (Phase C).
