# SESSION_HANDOFF — RoulCollector448

Status: INCIDENT + FIXES DEPLOYED (burst dedupe / window alignment / UTC stamps) — 2026-08-25 17:30 SAST

## CURRENT OBJECTIVE
Collect Table 448 (Auto-Roulette R2) spins 24/7 with 100% accuracy; serve via dashboard; then analysis layer (Service 3).

## LOCKED REQUIREMENTS
- Table 448 only (lobby key 48z5pjps3ntvqc1b — STABLE, confirmed by user numbers)
- Capture via /en/play/auto-roulette SPA → evo-games iframe CDP → lobby.historyUpdated
- Save in 25-spin batches (RC_SAVE_EVERY, default 25)
- 30-min accuracy audit cron (audit_448.py) — distinguishes SAVE-LAG from real problems
- Deploy dashboard ONLY on worker-01 (user: "no need to deploy on this box only on worker")
- 100% accuracy: gap >= 1 min auto-closes via backfill (recovery ladder L0-L6)

## OPEN ACTION — POST-RESTART REPAIR (needs user go-ahead; restart auto-denied 2026-08-25 17:20, 17:45)
The 17:08 recovery burst inflated the gid counter +13 (old per-slot dedupe);
the reconcile backfilled inflated obs rows into canonical as real spins
(phantom band counters 2578-2586 = truth 2565-2573 shifted) and the
reconstruct cascade renumbered the tail into the 200k range. FIXES are
deployed (commit c199abb + e931f74, files verified sha256-identical on
worker-01) but the RUNNING process still has old code.
CORRECT ORDER — STOP → RESTORE → START (not restart-then-restore):
a stopped collector writes nothing during the purge, so the counter seeds
from the CORRECTED obs MAX (truth band) on start. Restore-first-while-
running or restart-then-restore both leave a window where fresh obs rows
get purged or the seed reads a shifted MAX.
IMPORTANT: the old code is STILL corrupting live — backfill bursts at
18:09:53, 19:56:57, 20:25:51 wrote wrong numbers + stale timestamps into
canonical (verified live). Do NOT delay this repair; every ~20 min adds
more phantom rows. Run restore with LIVE journald truth (default — do NOT
set RC_TRUTH_FILE) so the truth band extends to the stop moment and the
newest spins are re-inserted, not lost.
1. `ssh abwarren@192.168.1.100 'export XDG_RUNTIME_DIR=/run/user/$(id -u); export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus; systemctl --user stop roulette-collector2.service && cd /opt/deploy/repos/RoulCollector448 && RC_DB_PATH=/home/wa/roulette2_spins.db PYTHONPATH=/opt/deploy/repos/RoulCollector448 /opt/deploy/venv/bin/python3 scripts/restore_tail_truth.py && systemctl --user start roulette-collector2.service && sleep 5 && systemctl --user is-active roulette-collector2.service'`
   (purges canonical+obs counters >= 2551, re-inserts journald truth #2551+,
   VERIFIED, aware-UTC; collector seeds 2599 on start)
2. Verify: `curl -s http://192.168.1.100:4480/api/integrity` → score 100, ok:true;
   spot-check canonical tail vs journald (`journalctl --user -u roulette-collector2 -n 200 | grep -oE "#[0-9]+: [0-9]+"`).
3. Install the 30-min audit timer (cron absent on worker-01, use systemd user timer):
   unit files drafted in this session — roulette-audit.service/.timer under
   ~/.config/systemd/user (OnCalendar=*:0/30, ExecStart=audit_448.py with
   RC_DB_PATH=/home/wa/roulette2_spins.db RC_CRED_FILE=/home/abwarren/.config/roulette2_collector.env).
   Install was BLOCKED (auto-deny) — rerun the enable command when user is present.

## THIS SLICE (2026-08-25 16:30-17:30) — "verify 100% accuracy + stale-page plan"
1. **ROOT CAUSE — burst counter inflation**: old dedupe checked pos i or i-1;
   a 13-spin recovery burst slides the old tail down 13 slots → EVERY entry
   looked new each frame → gid counter +13. obs re-listings (truth 2565-2573
   stored as 2578-2586) got backfilled into canonical by the reconcile →
   phantom rows with WRONG counters. Numbers stayed correct (capture path
   good), identity/sequence corrupted.
2. **FIXED (commit c199abb, PUSHED, deployed to worker-01)**:
   - `tail_slide()`: slide-aligned new-spin detection (burst-robust) — burst
     of 13 yields exactly 13 new gids, zero inflation. 7 new unit tests.
   - Reconcile window alignment: obs authority filtered to the local counter
     span + fetch limit 2000 — the perpetual missing+extras (score stuck 65,
     556 failed passes) resolves; ok can be true.
   - Reconcile task hardened: task error can never die silently again.
   - Aware-UTC timestamps: naive-local stamps parsed as UTC → false
     "server_ts 7200s in the future" (timestamps score 0.0). Fixed.
   - Full suite: 267 pass (1 pre-existing fail: test_watchdog L7 rung — the
     ladder prints L0-L6 only; add "[RECOVERY] L7" print or fix the test).
3. **Audit cron NEVER installed** (locked requirement missing) — see step 4
   above for the systemd timer fix.
4. **STALE-PAGE PLAN (user's ask)**: dual collector instances, same DB (WAL),
   each own browser + OFFSET refresh schedule (primary 20min / secondary 30min)
   so both never reload together; obs dedupes by gid (UNIQUE payload_hash);
   per-instance heartbeat staleness detection (60s no-spin while other live =
   reload only that page); both silent = upstream outage → backfill on recovery.
   worker-01 RAM OK (15Gi, 14 free). Implement AFTER the post-restart repair.
5. **Do NOT run `standalone_reconcile.run_once` on backfill-era data** —
   reconstruct_ordering cascade renumbered the tail into 200k range on
   2026-08-25 (same pitfall as 2026-08-24; the 203k max_seq rows are part of
   the corrupt band being purged by restore_tail_truth.py).

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
