# RoulCollector448 — Dashboard

Live dashboard for the Table 448 Auto Roulette spin collector (Sunbet / Evolution Gaming).

- **Data source:** `/home/wa/roulette2_spins.db` (SQLite, written per-spin by the collector)
- **Deploy (phase 1):** localhost on the collector box
- **Deploy (phase 2):** Ubuntu Server box on the LAN — dashboard must move without code changes

## Architecture

```
┌─────────────────────┐     writes      ┌──────────────────────┐
│ roulette2_collector │ ───────────────▶│ roulette2_spins.db   │
│  (systemd, 24/7)    │   per spin      │  (SQLite, untouched) │
└─────────────────────┘                 └──────────┬───────────┘
                                                   │ read-only
                                                   ▼
                                        ┌──────────────────────┐
                                        │  FastAPI REST API    │
                                        │  + static SPA served │
                                        └──────────┬───────────┘
                                                   │ JSON
                                                   ▼
                                        ┌──────────────────────┐
                                        │  Browser dashboard   │
                                        │  dark grid + charts  │
                                        └──────────────────────┘
```

The collector stays a dumb writer. Nothing in it changes. The dashboard talks to a
read-only API so it can be pointed at this box over the network when the Ubuntu
server comes up — no SQLite file access, no code changes.

## Docs

- `docs/grill-session.md` — requirements Q&A log (verbatim)
- `docs/plan.md` — implementation plan
- `docs/glossary.md` — domain terms (Nn, sleepers, Z-score, …)
- `docs/adr/` — architectural decision records

## Status

- [x] Plan + requirements in GitHub (this commit)
- [x] API layer (FastAPI, read-only, port 4480, systemd: `roulette-dashboard.service`)
- [x] Spin grid (50/row, 40 rows initial = 2000 spins, dark, show-more appends 2000)
- [x] Click-to-highlight (Number mode) + neighbors mode (Nn, distinct colors)
- [x] Stats panels (Z-scores, sleepers, streaks, rolling windows, hourly audit)
- [x] Live refresh (5s poll; liveness via journald — collector commits to DB in
      batches of 25 spins ~18min, so DB-only freshness would false-alarm)
- [x] Localhost deploy + verification (16 pytest + live smoke + browser checks)
- [ ] Ubuntu server deploy (phase 2)

## Running

```bash
# already installed as a user systemd service
systemctl --user status roulette-dashboard.service
# open
http://127.0.0.1:4480
```

API is at `:4480`; static frontend served from the same process.
Test suite: `.venv/bin/python -m pytest tests/ -q` (uses a fixture DB).
Live smoke test: `.venv/bin/python scripts/verify_live.py` (hits the running API).
