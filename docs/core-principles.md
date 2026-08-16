# RoulCollector448 — Five Non-Negotiable Principles

These five principles are the contract for the deep-pattern-mining,
cycle-analysis, and blind-prediction layers. Every module, feature, and
PRD section in this system is subordinate to them. Any design decision
that conflicts with a principle here is wrong by definition.

> A sufficiently sophisticated pattern-mining system can become very good
> at finding evidence for its own assumptions rather than finding what is
> actually present in the data. These principles exist to prevent that.

---

## P001 — Data must be correct.

The integrity pipeline (reconciliation, repair, verification, audit) is
the foundation. No analysis is trusted unless the data it consumes is
verified. Never repair from inference — only from authoritative identity.
Never destroy raw evidence. The rolling-500 trust indicator is the gate:
analysis may only run over VERIFIED windows.

## P002 — Discovery and prediction must be separated.

The pattern-discovery subsystem and the prediction engine are distinct
systems with distinct code paths, distinct data access, and distinct
audit trails. Discovery may inform prediction via an explicit, versioned
hand-off — it may never be conflated with it. A discovery is a hypothesis;
a prediction is a bet. They answer different questions.

## P003 — Predictions must be blind and out-of-sample.

The prediction engine may use only information available before the
outcome it predicts. No lookahead, no leakage, no post-hoc refitting on
the predicted window. Training/validation/test splits are immutable per
model version. Out-of-sample performance is the only performance that
counts — in-sample performance is reported but never claimed.

## P004 — Every claimed edge must survive statistical challenge.

Any discovered structure that is claimed as real must:
- survive multiple-testing correction (a pattern found by scanning many
  candidates is cheap; a pattern that survives Bonferroni/Holm-style
  correction is evidence),
- be reproducible (the effect must appear in a fresh sample, not just the
  window that suggested it),
- be compared against a simpler baseline (uniform-37 outcome,
  historical frequency, recent frequency) and beat it,
- and be evaluated across multiple temporal scales (micro/short/medium/
  long/extended) before any claim is made.

A claim that fails any of these is recorded as tested-and-refuted, not
deleted — refutations are research data.

## P005 — The system must be willing to change its mind.

No assumption, pattern, or model is ever protected from contradictory
evidence. VALIDATED means "supported by current evidence", not
"permanently proven". New verified data can strengthen, weaken, or
contradict any prior conclusion. The research ledger records every
assumption's lifecycle (SUPPORTED → WEAKENING → CONTRADICTED → REJECTED/
REVISED) and every model version is immutable — but the system's beliefs
are always revisable. The system must never defend an existing theory;
it must test it.

---

## How the principles map to the build

| Principle | Enforced by |
|---|---|
| P001 data correct | integrity pipeline (§11-§39): reconciler, repairer, rolling-500, audit trail |
| P002 discovery/prediction separation | separate modules: `pattern_miner` vs `prediction_engine`; versioned hand-off |
| P003 blind out-of-sample | immutable train/test splits per model version; no-lookahead data access |
| P004 statistical challenge | multiple-testing correction, reproducibility gate, baseline comparison, multi-scale evaluation |
| P005 change its mind | research ledger (assumption lifecycle), model versioning, hypothesis tracking |
