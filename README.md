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

The collector stays a dumb writer. The dashboard talks to a read-only API so
it can be pointed at this box over the network when the Ubuntu server comes
up — no SQLite file access, no code changes.

## Collector (`collector/`)

The collector lives in this repo (source of truth) and deploys to
`~/.hermes/scripts/roulette2_collector.py` (systemd user unit
`roulette-collector2.service`). See `docs/collector-reliability.md` for the
2026-08-12 no-gaps fix: 120s stall threshold, recovery ladder, CDP timeouts,
freshness watchdog.

**Credentials are NOT in the repo** (it's public). The collector reads
`SUNBET_USER`/`SUNBET_PASS` from env vars or `~/.config/roulette2_collector.env`
(KEY=VALUE, chmod 600).

## Docs

- `docs/grill-session.md` — requirements Q&A log (verbatim)
- `docs/plan.md` — implementation plan
- `docs/glossary.md` — domain terms (Nn, sleepers, Z-score, …)
- `docs/adr/` — architectural decision records

## Status

- [x] Plan + requirements in GitHub (this commit)
- [x] API layer (FastAPI, read-only, port 4480, systemd: `roulette-dashboard.service`)
- [x] Spin grid (25/row — whole row fits one page, NO gaps, white blocks with
      red/black/green text, 2000 spins initial, show-more appends 2000)
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

## Windows (this machine)

The pipeline runs natively on Windows (git-bash + Python 3.11+). All paths are
env-overridable; the default data dir is `%USERPROFILE%\.roulette2`
(`RC_DATA_DIR` to change; `RC_DB_PATH` / `RC_STATE_FILE` / `RC_CSV_FILE` /
`RC_CRED_FILE` / `RC_HEARTBEAT_FILE` / `RC_GAME_URL` override individually).

Setup:

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
```

Credentials: `SUNBET_USER` / `SUNBET_PASS` env vars, or
`%USERPROFILE%/.roulette2/roulette2_collector.env` (KEY=VALUE, protect the file).

Run (double-click or via Task Scheduler):

- `start_dashboard.bat` — API + UI on http://127.0.0.1:4480
- `start_collector.bat` — 24/7 capture loop (logs to `%USERPROFILE%\.roulette2\*.log`)
- `register_tasks.ps1` — registers both as logon scheduled tasks (the systemd
  equivalent); rerun with `-Remove` to uninstall.

Liveness on Windows: there is no journald, so the collector writes
`roulette2_heartbeat.json` every ~5s (status, last spins, counter) and the
dashboard reads it (`/api/health` reports `liveness_source: heartbeat`).
The Linux journald path is unchanged (`backend/liveness.py` picks per-OS).

Data-integrity endpoints (PRD §36-37):

- `GET /api/integrity` — verified window, health score/state, last
  reconciliation + repair, open incidents, per-component health
- `GET /api/incidents` — last N incidents with root-cause classification (§40)

Demo dataset for local dev: `.venv\Scripts\python scripts\build_demo_db.py`
then `set RC_DB_PATH=%USERPROFILE%\.roulette2\demo.db` before starting the
dashboard (creates 2200 spins + integrity events + a heartbeat).

