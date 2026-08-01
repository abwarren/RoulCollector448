# ADR-0002: Vanilla JS SPA + Chart.js, no build step

**Status:** Accepted
**Date:** 2026-08-01

## Context
Frontend for the dashboard: dense interactive spin grid (50/row, click-to-
highlight, neighbours mode) plus stats charts (Z-scores, sleepers, streaks,
rolling windows). Must be trivial to run on a second Ubuntu server later.

## Decision
Single-page app in **vanilla HTML/CSS/JS** with **Chart.js vendored locally**.
No framework, no bundler, no CDN dependency at runtime. Served by the FastAPI
process as static files.

## Consequences
- **Easier:** zero install on the target box beyond the API's Python deps.
- **Easier:** grid/highlight logic is hand-rolled but small and fully
  controllable (custom interactions are the whole point).
- **Easier:** works offline on the LAN (vendored Chart.js).
- **Harder:** no framework ergonomics; keep the JS structured in a few modules,
  not one giant file.

## Alternatives considered
- *React/Next:* heavy for a single dense grid; adds a build step to every
  deploy; rejected.
- *Grafana:* cannot do the custom click-to-highlight interaction; rejected.
- *Chart.js via CDN:* needs internet; rejected for LAN deployment.
- *BLM precedent:* Chart.js dashboards already proven in this environment
  (user's own stack).
