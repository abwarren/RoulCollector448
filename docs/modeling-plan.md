# Modeling Plan — Transition Matrices, Features, Backtest

Goal: turn the Table 448 spin history into normalized rows, transition
matrices, rolling-window features, and honest walk-forward backtests. The
dashboard stays as-is; this adds an analysis/modeling layer on the same
read-only DB (phase D wires results into the API).

Data: `/home/wa/roulette2_spins.db` — `roulette_spins` table.
22,123 spins as of 2026-08-10 (Jul 17 → Aug 10). Sequence order = `id`
(the collector appends per spin; ids accumulate across restarts, `ORDER BY
id` is capture order).

## Phase A — Transition matrices + Markov backtest (numpy + sqlite3 only, no new deps)

`scripts/transition_analysis.py`, run against the live DB. Outputs a compact
report. This is the tracer bullet everything else is measured against.

1. **Normalize every spin** into one row: number, color (R/B/G), dozen
   (0 / 1-12 / 13-24 / 25-36), column (0 / 1-3), wheel index (position in
   the European wheel array), prev_number, prev_color, wheel distance
   (circular gap between successive pockets, 0-18), time diff vs previous
   spin.
2. **Count matrices + Markov normalization:**
   - 37x37 number transitions A[i][j] = count of i followed by j;
     row-normalized M[i][j] = P(next=j | prev=i).
   - 3x3 color transitions, 4x4 dozen (zero as own class), 4x4 column,
     8x8 wheel-sector (fixed blocks of 5/5/5/5/5/4/4/4 in wheel order).
   - Top from→to pairs by count and by M deviation from 1/37.
3. **Fairness checks:** chi-squared on number distribution vs uniform
   (thresholds 51/95%, 58/99%), green rate vs 1/37, per-number Z-scores.
4. **Walk-forward backtest (no shuffle, chronological):** train first 70%,
   test last 30%:
   - Order-1 Markov: top-1 / top-3 / top-5 accuracy + log-loss vs uniform
     baseline (1/37 = 2.70%, ln 37 ≈ 3.61).
   - Order-2 Markov: state = last 2 numbers, fallback to order-1 when the
     pair count < 5 (smoothing), same metrics.
   - Verdict line: which model (if any) beats uniform on held-out spins.
5. **Rolling-window features (demo of phase B inputs):** last-50 window —
   per-number counts + Z-scores, hottest/coldest, current color streak.

Known ground truth from prior analyses: at 7.9K (Table 529) and 11.8K
(Table 448) spins the wheel tested fair — expect Markov ≈ uniform. The
backtest exists to prove it per-slice and catch collection artifacts (e.g.
the limping state skewing recent windows), not to find an edge.

## Phase B — Engineered features + gradient boosting (needs pip installs)

`scripts/feature_model.py` — LightGBM multiclass (37) and binary targets
(color/dozen/parity) on engineered features:
- Lags: prev 1-3 numbers (as 37-dim one-hot), colors, dozens, wheel indices.
- Windows 5/10/25/50: per-number counts + Z-scores, color/dozen balance,
  entropy, longest streak, sleeper gaps, hot-neighbor counts (Nn cluster
  hits in window).
- Distances: numeric |n-m|, wheel distance, sector distance (0-7),
  dozen/column jumps.
- Frequency decay: exponentially weighted recent counts per number.
- Time features: hour-of-day, minute-in-hour, day-of-week.

Deps: `pip install lightgbm` into the repo `.venv` (PyPI, not CDN).
Evaluation: same 70/30 chronological split, top-1/3/5 + log-loss, feature
importance (gain) + SHAP on the top features. Only a test-set win over
uniform is signal; training-set gains are overfit by definition.

**Phase B status (2026-08-10): DONE — result: fair wheel, no signal.**
`scripts/feature_model.py` (37-class next-number) + `scripts/predict_next.py`
(live top-5), venv `.venv-ml` (numpy/pandas/lightgbm — kept separate from the
dashboard `.venv`), models saved to `ml/`. Walk-forward 70/30 on 22.4K spins:
number acc 2.62% vs uniform 2.70% (best_iter=1 → learned nothing), color
50.0% vs 48.65% fair, Markov order-1 2.72%, Nn follow 13.87% vs 13.51%.
First build had a label-leaking markov feature (33.9% acc — impossible);
fixed to train-frozen argmax/conf, sanity rule: ≥5% next-number acc = leakage.
Deviations from plan: color as 3-class instead of binary targets, no
time-of-day features / exponential decay / SHAP yet — add only if a future
signal justifies it. SHAP at 22K rows with all features near-zero gain is
noise; the gain importances already answer the exploration question.

## Phase C — Sequence model (B250/P100 rig, not this box)

GRU/LSTM (or small transformer) in PyTorch fp16: embed(number) +
embed(color) + embed(dozen/sector) → 1-2 layers hidden 64-128 → softmax(37),
sliding windows of last 10-25 spins. Trains in seconds at 22K samples.
Same walk-forward split and metrics. Include only if Phase B shows any
test-set lift — the data, not the GPU, is the constraint.

## Phase D — Dashboard integration (optional, after A-C validated)

New API endpoints: `/api/transitions` (37x37 matrix or top pairs),
`/api/backtest` (cached walk-forward report), maybe `/api/features/last50`.
Frontend: hand-rolled SVG matrix heatmap + a "model vs uniform" panel.
Follows existing patterns: read-only, no-cache on /api, positional-rank
math, journald live-merge if gaps matter.

## Evaluation rules (all phases)

- Chronological split only — never random shuffle.
- Every metric shown beside the uniform baseline (2.70% top-1, 48.65%
  color, 33.3% dozen).
- No compensation claims. A deviation that doesn't reproduce in the test
  block is noise.
- Results are signal exploration / collection-quality checks, not
  prediction certainty (fair wheel has no memory).
