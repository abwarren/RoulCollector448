# Gap Markers — time-break notes in the dashboard grid

## Goal

When there is a break in time between consecutive spins (collector stall,
restart, downtime), the frontend must show a visible note at exactly that
position in the grid, so the user can see the data has holes instead of
assuming the sequence is continuous.

Context: normal Table 448 cadence is median 44s (legit range 40-57s). A
"break" is anything beyond that — collector limping, session abandons,
restarts, downtime. The user just reset the dataset (2026-08-11) for
accuracy reasons; knowing where the timeline has holes is now part of
trusting the data.

## Design

### Detection (pure frontend, no backend change)

- Timestamps already exist on every spin:
  - DB spins (`/api/spins`): `captured_at` ISO string
  - Live journald spins (`/api/health` `live_spins`): `time` HH:MM:SS
- Chronological sequence = `state.spins` (oldest→newest) concatenated with
  the live overlay in chronological order.
- A break exists between consecutive spins when
  `delta(t_i, t_{i+1}) > GAP_S` where `GAP_S = 120` (2 min).
  - 120s cleanly separates a true break from cadence variance (max legit
    ~57s). The 44s stall-detection threshold already false-fires at 45-46s;
    the gap marker threshold must NOT inherit that sensitivity.
  - No minimum break count — a single 2+ min gap shows its note.

### Rendering

- During `renderGrid()`, walk the newest-first array as today. When a cell
  is the first cell AFTER a break, close the current row, emit a full-width
  band, and start a new row:

  ```
  ⏱ 18m gap · 01:32:43 → 01:50:12
  ```

  - Band = duration + boundary times (older → newer).
  - Band splits the row at the break point; cells on each side stay dense
    (rows may be shorter than 25 on either side of a band — intentional,
    the band IS the break).
- Live overlay spins participate (breaks in the uncommitted window show too).
- Header badge: `N breaks` next to the meta line (0 = hidden), so a break
  anywhere in the loaded view is visible even if it scrolled off.

### Styling

- `.gapmark`: full-width band, amber background (#fff3e0), dark amber
  border + text (#e65100), small monospace, letter-spaced, hairline margin.
  Distinct from every cell state (red/black/green text, orange recent fill,
  purple doubles, blue highlight) — amber band reads as "warning: hole".

### Files touched

- `frontend/app.js` — detection + band rendering in `renderGrid()`
- `frontend/style.css` — `.gapmark` + header badge
- No backend/API changes (timestamps already served)

## Verification

Headless Chrome script (same pattern as `verify_latest_arrow.py`): serve a
fixture DB containing an injected 18-minute gap, assert:
1. A `.gapmark` element exists in the grid DOM.
2. Its text contains the expected duration ("18m") and both boundary times.
3. A control fixture WITHOUT gaps renders zero `.gapmark` elements.
