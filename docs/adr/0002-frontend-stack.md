# ADR-0002: Vanilla JS SPA + hand-rolled SVG charts, zero external deps

**Status:** Accepted
**Date:** 2026-08-01 (amended same day: Chart.js → hand-rolled SVG)

## Context
Frontend for the dashboard: dense interactive spin grid (50/row, click-to-
highlight, neighbours mode) plus stats charts (Z-scores, sleepers, streaks,
rolling windows). Must be trivial to run on a second Ubuntu server later.

## Decision
Single-page app in **vanilla HTML/CSS/JS**. Charts are **hand-rolled SVG**
(hit-count bars, color-balance lines) — no chart library, no bundler, no CDN
dependency at runtime. Served by the FastAPI process as static files.

## Consequences
- **Easier:** zero install on the target box beyond the API's Python deps.
- **Easier:** grid/highlight logic is hand-rolled but small and fully
  controllable (custom interactions are the whole point).
- **Easier:** works fully offline on the LAN — no internet needed ever.
- **Easier:** no 200KB vendor blob in the repo.
- **Harder:** no chart-library ergonomics (tooltips, zoom); charts stay
  simple (bars + lines) by design.

## Alternatives considered
- *React/Next:* heavy for a single dense grid; adds a build step; rejected.
- *Grafana:* cannot do the custom click-to-highlight interaction; rejected.
- *Chart.js vendored:* CDN download of chart.umd.min.js **denied by user**
  (2026-08-01); no local copy exists on this box (BLM uses the CDN at
  runtime, not vendored). Rejected.
- *Chart.js via CDN at runtime:* needs internet; rejected for LAN deployment.
