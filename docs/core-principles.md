# RoulCollector448 — Core Principles (P001–P013)

Source of truth for the whole platform. Every module, feature, and PRD
section is subordinate to these. Any design decision that conflicts with a
principle here is wrong by definition.

## Project

- name: RoulCollector448
- repository: https://github.com/abwarren/RoulCollector448
- local_url: http://localhost:4480/

## Directive

Build, complete, test, self-monitor, and locally deploy RoulCollector448
as a research-grade roulette data integrity, deep pattern discovery, cycle
analysis, regime detection, blind prediction, prediction evaluation, and
continuous model-improvement platform.

The system must derive conclusions from verified data rather than
assumptions. It must be self-critical, capable of changing its assumptions
when new evidence contradicts them, and capable of rejecting its own
hypotheses, patterns, models, and predictions.

Discovery and prediction must remain architecturally separate. Predictions
must nevertheless be prominently visible in the application. A separate
CLI optimization agent must continuously measure prediction accuracy and
search for legitimate improvements. (Prediction accuracy must be
continuously measured and improved over time.)

## Core principles

### P001 — Data integrity first
No analytical conclusion or prediction may rely on data that has not
passed the applicable integrity checks.

### P002 — Discovery and prediction separation
Discovery determines what the data has discovered. Prediction
independently determines what the model predicts next. Evaluation
determines whether the prediction was actually any good.

### P003 — Blind prediction
Every next-spin prediction must be generated, persisted, timestamped, and
frozen before the actual next result is observed.

### P004 — Evidence over assumptions
No pattern, cycle, neighbour relationship, regime, model, or predictive
edge may be assumed to exist. It must be discovered and validated from
data.

### P005 — Self-critical
The system must actively search for evidence that its strongest
conclusions are wrong.

### P006 — Adaptability
New verified data must be capable of changing, weakening, replacing, or
completely invalidating previous assumptions, patterns, models, cycle
interpretations, regime definitions, and predictions.

### P007 — Out-of-sample validation
No predictive claim may be promoted based only on the data used to
discover it.

### P008 — No fabricated data
Missing roulette results must never be inferred from probability,
hotness, coldness, cycles, neighbours, or model predictions.

### P009 — Immutable history
Raw observations and historical predictions are immutable. Corrections
are represented as auditable reconciliation events.

### P010 — No forced signal
The system must be allowed to conclude that no meaningful predictive
signal currently exists.

### P011 — Simple models must compete
Complex models must continuously compete against simple baselines.
Complexity is never evidence of superiority.

### P012 — The system must be willing to change its mind
Previously validated does not mean permanently true. Every conclusion
remains subject to future evidence.

### P013 — No premature completion
Documentation, specification, compilation, or partial implementation does
not constitute completion. The application must be operational, tested,
and demonstrably running at http://localhost:4480/ before completion may
be declared.
