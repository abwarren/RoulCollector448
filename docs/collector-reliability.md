# Collector Reliability — no-gaps fix (2026-08-12)

Incident analysis for the recurring time gaps in `roulette2_spins.db`, and the
fix now living in `collector/roulette2_collector.py` (v2).

## Symptom

Since the dataset reset (2026-08-11 01:32), ~2,500 spins showed **104 gaps
> 2 min**: recurring 5.6–6.4 min gaps every ~35–40 min, plus larger ones
(32.5, 22.8, 21.4, 19.2, 17.9 min).

## Root causes (from journald + DB forensics)

1. **False-fire stall detector.** Threshold was 44s, but Table 448's legit
   cadence is median 44s (range 40–57s). The detector fired on healthy
   intervals, and every false-fire re-armed CDP. That listener churn
   (~1/min) is what destabilised the WS stream and *caused* the recurring
   ~35-min stream deaths.
2. **Broken gentle-recovery path.** The refresh-button selectors never matched
   the real Evolution DOM ("Refresh button not found" every time), so recovery
   always fell to full page reloads: 4 reloads × ~60s, then abandon +
   re-login. Total dead time ≈ 5.8 min per incident — the recurring gap.
3. **Unbounded CDP calls.** On 2026-08-11 a `CDPSession.send()` hung with no
   timeout and froze the whole asyncio loop for ~26 min (the 32.5-min gap).
4. **Reload spiral without verification.** 4 blind reloads even when a reload
   had already failed — no check that frames actually resumed.

Backfill is impossible: the missed spins are gone (the page only shows the
last ~25 results and the gaps are days old), so per the unverifiable-history
rule the old holes stay marked. This fix is going-forward.

## The fix (collector v2)

| # | Change | Effect |
|---|--------|--------|
| 1 | Stall threshold 44s → **120s** (matches dashboard GAP_S) | zero false fires; no churn |
| 2 | Recovery **ladder**: passive wait → CDP re-arm → refresh click → page reload → full browser restart, verifying a new spin after every rung | most stalls recover < 1 min; no 4x-reload spiral |
| 3 | Every CDP call wrapped in `asyncio.wait_for` (15s) | the 26-min loop freeze is impossible |
| 4 | After reload/restart, verify frames resume within 90s, else escalate | no blind retry loops |
| 5 | DOM candidate dump when the refresh selector misses | next stall logs the real Evolution refresh control (self-learning) |
| 6 | Freshness watchdog (`collector/watchdog.py` + systemd timer, every 5 min) | restarts the unit if journald goes silent 12 min (belt-and-braces for process-level hangs) |

Credentials moved out of the script into env vars /
`~/.config/roulette2_collector.env` — the repo is **public**, never commit them.

## Deploy

```bash
# repo -> target (this box)
mkdir -p ~/.config && chmod 600 ~/.config/roulette2_collector.env   # SUNBET_USER=/SUNBET_PASS=
cp collector/roulette2_collector.py ~/.hermes/scripts/roulette2_collector.py
cp collector/watchdog.py ~/.hermes/scripts/roulette2_watchdog.py
cp collector/roulette2-watchdog.service collector/roulette2-watchdog.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now roulette2-watchdog.timer
systemctl --user restart roulette-collector2.service
```

Verify: `systemctl --user status roulette-collector2.service`, journald shows
`[CDP] WS interception enabled ✓`, and a new spin line within ~2 min.

## Expected residual

Worst-case dead time per stream death is now ~4 min (120s stall + ~90s ladder
+ restart), usually far less (rungs 0–2 resolve in <60s). A stream death every
~35–40 min is gone — that was an artifact of the false-fire churn. Real
disconnects (network, Sunbet session expiry, Evolution-side drops) still
happen, but recover in seconds-to-minutes instead of ~6 min.
