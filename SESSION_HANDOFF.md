# SESSION_HANDOFF — RoulCollector448

Status: REPAIR COMPLETE — score 100, ok:true, verified — 2026-08-25 21:35 SAST

## CURRENT OBJECTIVE
Collect Table 448 (Auto-Roulette R2) spins 24/7 with 100% accuracy; serve via dashboard; then analysis layer (Service 3).

## LOCKED REQUIREMENTS
- Table 448 only (lobby key 48z5pjps3ntvqc1b — STABLE, confirmed by user numbers)
- Capture via /en/play/auto-roulette SPA → evo-games iframe CDP → lobby.historyUpdated
- Save in 25-spin batches (RC_SAVE_EVERY, default 25)
- 30-min accuracy audit cron (audit_448.py) — distinguishes SAVE-LAG from real problems
- Deploy dashboard ONLY on worker-01 (user: "no need to deploy on this box only on worker")
- 100% accuracy: gap >= 1 min auto-closes via backfill (recovery ladder L0-L6)

## SYSTEM STATE (2026-08-25 21:35 SAST — REPAIRED)
| Box | Collector | Dashboard | DB |
|-----|-----------|-----------|-----|
| gdi (this box) | code repo, tests | not deployed (per user) | /home/gdi/RoulCollector448 |
| worker-01 (.100) | ACTIVE, fixed code (PID 59974), capturing live | ACTIVE :4480 | /home/wa/roulette2_spins.db — 3373 rows, score 100, ok:true, 0 missing/extras/dups/gaps, sequence==counter |

## INCIDENT 2026-08-25 — FIXED (commits c199abb → e931f74 → f465e06 → 7cc532b, all PUSHED + deployed)
Root cause chain:
1. Old per-slot dedupe failed on recovery BURSTS → gid counter inflated (+13)
2. Reconcile backfilled inflated obs rows into canonical as phantom spins (wrong numbers/counters)
3. reconstruct_ordering cascade renumbered tail into 200k range
4. save_spins blanket "sequence_no = id WHERE NULL" re-cascaded after restore
5. Reconcile authority ordered by observed_at → burst ties arbitrary (781 descents) → phantom reorders
6. Reconcile task could die silently (froze 15:12, score stuck 65)

Fixes (all deployed + verified):
- tail_slide(): burst-robust new-spin detection (13-spin burst = 13 gids, 7 unit tests)
- Reconcile window alignment: obs filtered to local counter span + fetch 2000
- Reconcile authority sorted by gid counter DESC (not observed_at)
- save_spins: sequence_no = gid counter on INSERT (id fallback only for legacy)
- Aware-UTC timestamps (naive-local parsed as UTC → false future-skew flags)
- Reconcile task hardened: error → traceback to journald + event, cadence continues
- scripts/restore_tail_truth.py: purge band >= 2551, re-insert journald truth (first-occurrence per counter = live print, NOT backfill re-prints), VERIFIED, aware-UTC, sequence=counter
- scripts/restore_obs_gaps.py: one-time obs restore (idempotent, no-op now)
- Verified: ad-hoc harness 20/20 + reconcile sim on live DB copy {ok:true, 0/0/0/0/0} + live score 100.0

## PENDING (blocked by approval layer 3x — run when user present)
1. Install the 30-min audit timer (cron absent on worker-01; systemd user timer):
   units drafted below. Enable: systemctl --user daemon-reload && systemctl --user enable --now roulette-audit.timer
   ~/.config/systemd/user/roulette-audit.service:
   [Unit]
   Description=Roulette448 30-min accuracy audit
   After=network-online.target
   [Service]
   Type=oneshot
   Environment=RC_DB_PATH=/home/wa/roulette2_spins.db
   Environment=RC_CRED_FILE=/home/abwarren/.config/roulette2_collector.env
   Environment=PYTHONPATH=/opt/deploy/repos/RoulCollector448
   ExecStart=/opt/deploy/venv/bin/python3 /opt/deploy/repos/RoulCollector448/scripts/audit_448.py
   ~/.config/systemd/user/roulette-audit.timer:
   [Timer]
   OnCalendar=*:0/30
   Persistent=true
2. Pre-existing (not from fixes): test_watchdog expects "[RECOVERY] L7" print — ladder is L0-L6; add the print or fix the test.
3. STALE-PAGE PLAN (user's ask, NOT yet implemented): dual collector instances, same DB (WAL), offset refresh schedules (primary 20min / secondary 30min), per-instance heartbeat staleness (60s no-spin while other live = reload only that page), both silent = upstream outage → backfill on recovery. worker-01 RAM OK (15Gi).

## SYSTEM STATE (pre-incident reference)
| Box | Collector | Dashboard | DB |
|-----|-----------|-----------|-----|
| gdi (this box) | ACTIVE | not deployed (per user) | /home/gdi/roulette2 |
| worker-01 (.100) | ACTIVE, capturing live (~44s cadence) | ACTIVE :4480 | /home/wa/roulette2_spins.db — 283+ rows (208 REPAIRED + 73 VALID), 21 legacy NULL-seq residue |

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
