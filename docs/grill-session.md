# Grill Session — RoulCollector448 Dashboard

Logged during grilling before any code. Questions verbatim, answers verbatim,
corrections noted. Companion docs: `glossary.md`, `plan.md`, `adr/`.

## Session 2026-08-01

### Q1. Where will you actually open this dashboard?
**A (voice):** "to connect one of my other PCs as a server but I first need to boot it as Ubuntu server"

**Correction/refinement:** Target deployment is a second PC booted as Ubuntu
Server (home LAN). Which box (Dell Vostro / B250 rig) still TBD.

### Q2. Until the server is ready — deploy where?
**A (voice):** "for now until the server is setup deploy on localhost but first we must have a shared understanding and the plan must be in github before we touch code"

**Decision:** Phase 1 = localhost on collector box. Phase 2 = Ubuntu server.
Plan doc must land in GitHub before any implementation code. Shared understanding
is the deliverable of this grill.

### Q3. Which repo?
**A (voice):** "git@github.com:abwarren/RoulCollector448.git"

**Decision:** New repo `RoulCollector448`, currently empty. All plan docs +
implementation go here.

### Q4. Should the dashboard read the SQLite DB directly, or via an API?
**A (voice):** "whats the best option in case we deploy this somewhere else"

**Decision (agent recommendation, accepted):** Small read-only REST API layer
between the DB and the frontend. When the dashboard moves to the Ubuntu server,
it points at this box's API over the network — nothing in the frontend changes.
Also avoids SQLite file-locking when multiple readers exist. See ADR-0001.

### Q5. Frontend requirements (dense voice dump)
**A (voice), verbatim fragments:**
- "on the front end chart I want as many numbers on the page as possible and the option to see more but hide it initially"
- "get 50 spins in one row … rows of 50"
- "I like the reverse black"
- "make the numbers clickable so when I click number 10 all the 10 highlights"
- "option for me to click 10 neighbors and all the 10 neighbors highlight … if I click 0 neighbors 0 3 15 32 and 26 all highlight"
- "if I just click on 10 and I want all the 10s to highlight then all the 10s will highlight distinguish them between colors"
- "provide Z scores sleepers streaks rolling window charts all of them"

**Parsed requirements:**

1. **Spin grid:** dense grid of recent spins, **50 numbers per row**,
   chronological. Dark theme ("reverse black" = black background).
2. **Pagination:** a limited set visible initially (default: 500 = 10 rows);
   a "show more" control appends older spins without page reload.
3. **Click-to-highlight, mode A (number):** clicking a number highlights **all
   occurrences of that number** in the grid.
4. **Click-to-highlight, mode B (neighbors):** with neighbors mode on, clicking
   a number highlights its **Nn cluster** (5-number wheel cluster, N + 2 left +
   2 right). **User's own example verified:** clicking 0 highlights
   {0, 3, 15, 32, 26} — matches the European wheel layout exactly.
5. **Color distinction:** the clicked number and its neighbors render in
   **different highlight colors** so they're distinguishable at a glance.
6. **Stats panels (all of them):** per-number **Z-scores**, **sleepers**
   (droughts), **streaks**, **rolling window** charts (50/100/200/500/1000).

### Open questions (next grill round)
- Port number: **4480** (confirmed 2026-08-01 — user rejected 8080, "use different internal port for localhost")
- Live refresh mechanism (default proposal: 5s poll — collector commits every spin)
- Initial visible rows (default proposal: 10 rows = 500 spins, +10 rows per click)
- Confirm stack: FastAPI + vanilla JS SPA + Chart.js, no build step (BLM precedent)
- Phase 2 server: which PC, Ubuntu Server version — deferred until server is booted
