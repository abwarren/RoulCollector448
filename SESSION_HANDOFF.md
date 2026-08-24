# SESSION_HANDOFF — RoulCollector448

Status: MILESTONE 2 + DATA-FIX (backfill window 500) — 2026-08-24 20:10 SAST

## CURRENT OBJECTIVE
Collect Table 448 (Auto-Roulette R2) spins 24/7 with 100% accuracy; serve via dashboard; then analysis layer (Service 3).

## LOCKED REQUIREMENTS
- Table 448 only (lobby key 48z5pjps3ntvqc1b — STABLE, confirmed by user numbers)
- Capture via /en/play/auto-roulette SPA → evo-games iframe CDP → lobby.historyUpdated
- Save in 25-spin batches (RC_SAVE_EVERY, default 25)
- 30-min accuracy audit cron (audit_448.py) — distinguishes SAVE-LAG from real problems
- Deploy dashboard ONLY on worker-01 (user: "no need to deploy on this box only on worker")
- 100% accuracy: gap >= 1 min auto-closes via backfill (recovery ladder L0-L6)

## SYSTEM STATE
| Box | Collector | Dashboard | DB |
|-----|-----------|-----------|-----|
| gdi (this box) | ACTIVE | not deployed (per user) | /home/gdi/roulette2 |
| worker-01 (.100) | ACTIVE, capturing live (~44s cadence) | ACTIVE :4480 | /home/wa/roulette2_spins.db — 283+ rows (208 REPAIRED + 73 VALID), 21 legacy NULL-seq residue |

## THIS SLICE (2026-08-24 19:45-20:10) — "fix data with backfill last 500"
1. **Backfill DONE**: standalone reconcile (window 500) inserted ~182 authority-known spins (REPAIRED) from spin_observations history. Then re-stabilized sequence.
2. **Sequence authority = gid counter**: 4-part gids `lobby-<key>-<n>-<cnt>` → sequence=cnt; legacy 3-part gids → NULL (unorderable residue, rotates out). Dashboard gap detector is sequence-based.
3. **ROOT CAUSE FOUND — counter-seed bug**: `MAX(CAST(substr(game_id,-5)))` grabs number digits too (`lobby-<key>-35-106` → `"5-106"` → 5). Garbage base after restart → new gids collide → INSERT OR IGNORE silently DROPS new spins. FIXED in collector/roulette2_collector.py (parse `r"-(\d+)$"`), commit 0817926 PUSHED, deployed to worker-01.
4. **reconcile_task HistoryRecord bug — STILL OPEN**: every reconcile pass fails `'HistoryRecord' object has no attribute 'get'` (61+ failures, score 65 DEGRADED, auto-repair dead). Traceback patch deployed in reconciler.py (message carries traceback tail) — **the NEXT COLLECTOR RESTART will reveal the exact line in /api/integrity last_reconciliation.message**. Restart was BLOCKED (security layer auto-deny, user away). Give user the one-liner:
   `ssh abwarren@192.168.1.100 'export XDG_RUNTIME_DIR=/run/user/$(id -u); export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus; systemctl --user restart roulette-collector2.service'`
5. **Do NOT run `standalone_reconcile.run_once` on backfill-era data** — its reconstruct_ordering sorts by approximate timestamps → sequence cascade +400/run + bogus RESOLVED gap events (ids 11, 17 in integrity_events — leave them, trail is append-only).

## COMPLETED SLICES
- Service 1 (collector+DB+watchdog): TRACER BULLET ✅ — commits 6cc0154, fb14709, fbd6d27, 6e58402, 782181e, fb04e23, 0817926 pushed.
- Service 2 (dashboard API :4480): ✅ deployed worker-01, /api/health ok, frontend serves. Reads RC_DB_PATH.
- Data-fix slice: backfill 500 ✅, sequence rebuild ✅, seed bug fixed+deployed+pushed ✅.

## FILES CHANGED (this slice)
- collector/roulette2_collector.py — counter seed fix (substr(-5) → regex trailing segment)
- collector/reconciler.py — reconcile failure message now includes traceback tail (permanent, aids remote diagnosis)
- worker-01: both scp'd to /opt/deploy/repos/RoulCollector448/collector/ (load on next restart)

## NEXT SLICE
- Restart collector (user approval) → read traceback → fix reconcile_task HistoryRecord bug → verify /api/integrity shows ok=true, score 100, rolling500 perfect
- Service 3 (analysis layer) per integrity-first plan

## RESOLVED 2026-08-25 — reconcile crash + duplicate backfill corruption

**Symptoms**: /api/integrity score stuck 65 DEGRADED; every reconcile pass
failed `'HistoryRecord' object has no attribute 'get'`; grid tail showed the
same spin block ~3x (19:10:04 x6 in DB, 132 dup rows deleted).

**Root causes** (2 independent):
1. Import mismatch: collector ran as a flat script; fallback `import
   reconciler` loaded a SECOND module instance, so `HistoryRecord` from
   history.py's `from collector.reconciler import` was a different class
   than the collector's — isinstance() in normalize_record failed. Fixed:
   fallback now `from collector import (...)`. (commit fae1176)
2. Obs re-observation: the lobby tail re-lists the last ~10 spins every
   frame; prev_newest dedupe only caught the newest, so the same spin was
   re-observed with a fresh counter gid, and backfill_gaps inserted each
   under a different gid. Fixed: tail-slide dedupe (pos i or i-1 match =
   re-observation) gives each physical spin ONE stable counter gid; obs
   store is the single source of identity, canonical loop consumes it.
   (commits b53e982 + fae1176)

**Verified**: 588/588 verified, 0 dups, 0 missing, 0 conflicts; capture
cadence ~40s/spin; obs counters strictly sequential; 0 crashes.
