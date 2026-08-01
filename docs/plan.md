# Implementation Plan — RoulCollector448 Dashboard

Target: dense dark spin grid + click-to-highlight + stats panels, backed by a
read-only API, live on localhost, portable to an Ubuntu server.

## Stack (defaults — confirm in next grill round)

- **API:** FastAPI + uvicorn, single process, read-only over SQLite
  (`roulette2_spins.db`). Serves the static frontend too.
- **Frontend:** vanilla JS SPA, no build step. Chart.js vendored locally
  (no CDN dependency). Dark theme.
- **Config:** port + DB path in one small config, env-overridable. **Port: 4480**
  (chosen over the default 8080 per user — "use different internal port for
  localhost"; nothing else bound to it).

## API endpoints (read-only)

| Endpoint | Returns |
|----------|---------|
| `GET /api/health` | DB reachable, total spins, last spin + captured_at, age in seconds, collector liveness (DB mtime) |
| `GET /api/spins?offset=0&limit=500` | Spins in chronological order for the requested window (oldest-of-window first). Default = most recent 500. |
| `GET /api/spins/count` | Total spin count (for pagination math) |
| `GET /api/stats/numbers` | Per-number: hits, expected, **Z-score**, hits in last 100, color. Sorted by \|Z\| desc. |
| `GET /api/stats/sleepers` | Per-number current drought (spins since last hit), sorted desc |
| `GET /api/stats/streaks` | Longest red/black streaks (historical), current running streak, top number repeats |
| `GET /api/stats/rolling?window=500` | Per-window summary: counts, color/dozen/parity balance, top/bottom numbers, neighbor rate |

All stats computed in SQL/Python from the same schema the collector writes:
`roulette_spins(id, number, description, color, game_id, server_ts, captured_at, created_at)`.
`game_id` UNIQUE — idempotent reads, no duplicate risk.

## Frontend layout

```
┌────────────────────────────────────────────────────────┐
│ Header: Table 448 · LIVE · last spin 23 Red · 34s ago  │
├────────────────────────────────────────────────────────┤
│ Spin grid (dark)                                       │
│   row 1:  [0][32][15][19][4] … 50 numbers             │
│   row 2:  …                                           │
│   …      (10 rows visible initially = 500 spins)       │
│   [ Show more (older) ]                               │
├────────────────────────────────────────────────────────┤
│ Stats: Z-scores table │ Sleepers │ Streaks │ Rolling   │
│ charts (50/100/200/500/1000)                           │
└────────────────────────────────────────────────────────┘
```

### Grid rules
- 50 numbers per row, chronological, oldest top-left → newest bottom-right.
- Black background. Red numbers = red fill; green = green fill; **black numbers
  get a light border** so they're visible on black (glossary: reverse black).
- Live ticker at the top; new spin appends to the grid on refresh.
- "Show more" appends the next 500 older spins (10 rows) without reload.

### Highlight interactions
- **Mode A (default, "Number"):** click a cell (e.g. 10) → every occurrence of
  that number across the whole loaded grid highlights in one color. Click again
  or press Esc to clear.
- **Mode B ("Neighbors"):** toggle button switches mode. Click 10 →
  Nn(10) = {8, 23, 10, 5, 24} highlights. **Clicked number = distinct color
  (e.g. cyan); the 4 wheel neighbours = another color (e.g. yellow).** Same for
  any N. Verified against user example: 0 → {0, 3, 15, 32, 26}.
- Both modes: any occurrence of the highlighted number in the **stats tables**
  also highlights, for cross-reference.
- Grid cells are clickable buttons (keyboard-accessible), not divs.

### Neighbour cluster derivation
From the European wheel layout (glossary). Compute Nn at runtime client-side
from the fixed wheel array — no per-number hardcoding:
`0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26`.
Nn(N) = [N, two-before, two-after] in this array. (This is the established
Nn convention from the roulette analysis work — see `roulette-neighbor-notation`
skill.)

## Phases

1. **Repo + plan** (this commit): README, grill session, glossary, ADRs, plan.
2. **API layer:** FastAPI app + endpoints, config, pytest for endpoints against
   a fixture copy of the DB. Verify with curl against live DB.
3. **Grid shell:** static SPA, dark theme, rows of 50, pagination ("show more"),
   live ticker via `/api/health` poll (5s).
4. **Highlight interactions:** mode A, mode B, color distinction, Esc/clear,
   stats-table cross-highlight.
5. **Stats panels:** Z-score table, sleepers, streaks, rolling-window charts
   (Chart.js, line/bar as appropriate).
6. **Integration:** run API on localhost, browser-verify every interaction
   against live data, check freshness rules (last capture vs now).
7. **Ubuntu server deploy (later):** point API at this box's network address or
   move the DB; systemd unit; LAN access. Not in phase 1 scope.

## Verification (phase 1 done when…)
- `curl /api/spins` returns the live last 500 with correct ordering/count.
- Grid renders 50/row, "show more" works, ticker updates ≤5s.
- Click 10 → all 10s highlight; neighbors mode → 0 gives exactly
  {0, 3, 15, 32, 26} with two distinct colors.
- Z-scores match a trusted reference computation on the same DB.
- Last-capture freshness shown (skill rule: never present stale data as live).
