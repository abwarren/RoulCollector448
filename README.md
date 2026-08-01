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
- [ ] API layer
- [ ] Spin grid (50/row, dark)
- [ ] Click-to-highlight + neighbors mode
- [ ] Stats panels (Z-scores, sleepers, streaks, rolling windows)
- [ ] Live refresh
- [ ] Localhost deploy + verification
- [ ] Ubuntu server deploy
