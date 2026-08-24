# SESSION_HANDOFF — RoulCollector448

Status: MILESTONE 2 COMPLETE (Service 1 + Service 2) — 2026-08-24 17:30 SAST

## CURRENT OBJECTIVE
Collect Table 448 (Auto-Roulette R2) spins 24/7 with 100% accuracy; serve via dashboard; then analysis layer (Service 3).

## LOCKED REQUIREMENTS
- Table 448 only (lobby key 48z5pjps3ntvqc1b — STABLE, confirmed by user numbers)
- Capture via /en/play/auto-roulette SPA → evo-games iframe CDP → lobby.historyUpdated
- Save in 25-spin batches (RC_SAVE_EVERY, default 25)
- 30-min accuracy audit cron (audit_448.py) — distinguishes SAVE-LAG from real problems
- Deploy dashboard ONLY on worker-01 (user: "no need to deploy on this box only on worker")

## SYSTEM STATE
| Box | Collector | Dashboard | DB |
|-----|-----------|-----------|-----|
| gdi (this box) | ACTIVE, 42+ spins, healthy | not deployed (per user) | /home/gdi/roulette2 (21 spins) |
| worker-01 (.100) | ACTIVE but flaky (crashed @17:10 after 2 spins, auto-restarted 17:25) | ACTIVE :4480, health ok | /home/wa/roulette2_spins.db (0 spins — crash before first batch) |

## COMPLETED SLICES
- Service 1 (collector+DB+watchdog): TRACER BULLET ✅ — source→login→game→CDP→lobby→filter 448→save→audit cron. Commits 6cc0154, fb14709, pushed.
- Service 2 (dashboard API :4480): ✅ deployed worker-01, /api/health ok, frontend serves. Reads RC_DB_PATH.

## FILES CHANGED
- collector/roulette2_collector.py — lobby capture + SAVE_EVERY + RC_LOBBY_TABLE filter
- collector/history.py — parse_lobby_history
- scripts/audit_448.py — accuracy audit (new)
- worker-01: ~/.config/systemd/user/roulette-dashboard.service (RC_DB_PATH=/home/wa/roulette2_spins.db)
- worker-01: `loginctl enable-linger abwarren` — user services (dashboard+collector) were dying every ~45s when SSH sessions ended (user manager teardown). Linger keeps them alive. Dashboard now stable at http://192.168.1.100:4480/

## TESTS & RESULTS
- Ad-hoc verify: parse_lobby_history (newest per table), save-batch fires at SAVE_EVERY, order check flags non-monotonic — ALL PASSED
- Live: gdi saves verified (+2 new to DB, DB growing), audit reports correctly

## CRITICAL DISCOVERIES
- GAP-CLOSING DONE (2026-08-24 18:15): canonical spins + tail observations share unique game_ids (lobby-<key>-<n>-<cnt>, counter seeded from DB on restart). Reconcile now detects missing spins (107 found) and repairer backfills them (107 inserted, status=REPAIRED). Recovery L0 runs this automatically on >=1min stall — no refresh (refresh demoted to L3, rare).
- Old pre-fix rows (lobby-<key>-<n> no counter) show as UNVERIFIED extras — legacy residue, not gaps; cleaned by rotation over time.
- worker-01 collector unit has NO RC_DATA_DIR override → writes /home/wa/ (default). Dashboard RC_DB_PATH must match: /home/wa/roulette2_spins.db
- worker-01 crashed after 2 spins @17:10, heartbeat froze, systemd restarted @17:25 (slow backoff). Watch: if it keeps crashing, investigate its login/game load vs gdi.
- validation_issues: 2 on worker-01's 2 spins — check what validator flagged (integrity_events).

## KNOWN ISSUES
- worker-01 collector was flaky (session teardown killed it) — FIXED via lingering; now capturing (2 spins in 3 min)
- worker-01 DB still 0 rows — first 25-batch pending (~18 min after stable capture)
- Audit cron (0f0433f19b69) delivers local — check via cronjob list

## NEXT VERTICAL SLICE (Service 3 — analysis layer)
- Point transition_analysis.py / sequence_model.py at worker-01's DB (or gdi's, once worker-01 stabilizes)
- Verify: read DB → compute neighbors/streaks/sleepers → dashboard /api/stats serves them
- Depends on: worker-01 DB accumulating spins (needs collector stability first)

## FIRST ACTION ON RESUME
ssh -o BatchMode=yes abwarren@worker-01.local 'export XDG_RUNTIME_DIR=/run/user/$(id -u); export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus; journalctl --user -u roulette-collector2.service -n 20 --no-pager'
(Check if worker-01 collector is capturing post-restart; if 0 spins >10min, investigate crash.)
