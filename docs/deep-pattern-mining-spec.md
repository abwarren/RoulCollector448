# Deep Pattern Mining — Spec Capture (accumulated from the PRD feed)

> This document accumulates the deep-pattern-mining / cycle-analysis /
> blind-prediction spec exactly as fed, fragment by fragment, so no
> requirement is lost between sessions. It is the source of truth for the
> pattern-mining build. Five non-negotiable principles govern everything
> here: see docs/core-principles.md (P001-P005).

## Objective (verbatim)

Discover temporal, sequential, spatial, cyclic, colour, neighbour, repeat,
and structural patterns from verified historical data **without
predefining which patterns are meaningful.**

Discovered structures are real only when they are persistent, statistically
unusual, predictive out-of-sample, and the system is capable of proving its
own hypotheses wrong.

## Pattern families

### individual_numbers
- frequency
- return_time
- inter_arrival_distribution
- early_or_late_cycle_position

### repeats
- double
- triple
- quadruple
- long_repeat_run
- repeat_reactivation

### colors
- red_runs
- black_runs
- alternation
- color_regimes
- color_regime_changes
- color_entropy

### wheel_relationships
- clockwise_distance
- counter_clockwise_distance
- short_wheel_move
- long_wheel_move
- direction_continuation
- direction_reversal
- wheel_relative_sequence

## Baselines (the P004 comparison set)

- uniform_37_outcome
- historical_frequency
- recent_frequency

Every claimed effect must be compared against these simpler baselines and
beat them. A claim also dies if:
- effect_disappears_with_multiple_testing_correction
- performance_is_not_reproducible
- simpler_model_explains_same_effect

## Temporal scales (every important pattern must be evaluated across scales)

- micro: 20, 50
- short: 100, 150, 250
- medium: 500, 1000
- long: 2500, 5000, 10000
- extended: 50000, 100000, all_available

Purpose: detect short/medium/long/persistent structures without assuming
behaviour in one window represents the entire dataset.

## Recovery window

- size: 500
- purpose: treat the latest 500 spins as an ACTIVE VERIFICATION AND REPAIR
  window, not merely an analytics window.

## Observation model

- raw_observations: immutable; required fields: observation_id,
  session_id, observed_at, source, game_id, number, description,
  server_timestamp, payload_hash, raw_payload, validation_status
- canonical_spins: required fields: spin_id, sequence_number, game_id,
  number, color, server_timestamp, captured_at, committed_at, source,
  confidence, status; plus first_seen_at, last_verified_at,
  verification_version
- integrity_events: required fields: event_id, timestamp, event_type,
  severity, affected_game_ids, details, resolution, status

## Integrity layers (the pipeline the pattern miner consumes)

- identify_missing_records
- identify_duplicates
- identify_conflicts
- generate_repair_plan
- apply_deterministic_repairs_atomically
- re-run_reconciliation
- mark_verified_only_after_success

### repair_policy
automatically_repair:
- missing_authoritative_spin
- incorrect_canonical_value_with_authoritative_identity
- duplicate_canonical_record
- deterministic_ordering_error
never_infer: detect and repair recoverable missing, duplicated,
conflicting, malformed records — but never infer what is not evidenced.

## Recovery ladder (success condition)

ladder: passive_wait, cross_source_validation, reconcile_recent_history,
rearm_cdp, refresh_game, reload_page, restart_browser,
restart_collector, create_unresolved_incident

success_condition: Recovery is not successful merely because the browser
or process is alive. Recovery is successful only after data reconciliation
returns to an acceptable verified state.

## Pattern genealogy

Objective: track relationships between patterns so the system identifies
pattern FAMILIES rather than thousands of disconnected discoveries.

Relationships:
- parent_pattern
- child_pattern
- contained_pattern
- structurally_equivalent_pattern
- pattern_family
- pattern_similarity

Example:
- parent: "17 -> 20 -> 22"
- children: "17 -> 20", "20 -> 22"

## Prediction engine

Objective: predict the probability distribution of the next roulette
outcome using only information available before that outcome occurs
(P003: blind and out-of-sample).

## Self-critical adaptive learning

Assumption ledger entries carry: assumption_id, supporting_dataset,
sample_size, confidence. States include WEAKENING, CONTRADICTED.

Triggers for re-evaluation:
- new_verified_data
- regime_change
- pattern_decay
- prediction_failure
- out_of_sample_failure
- statistical_significance_loss
- baseline_improvement
- new_competing_hypothesis
- data_source_change

Process: identify_assumptions_affected_by_new_data →
recalculate_assumption_evidence → search_for_contradictory_evidence →
compare_old_and_new_results → determine_if_assumption_still_holds →
downgrade_or_reject_if_required → generate_replacement_hypothesis_if_supported
→ retest_replacement → record_decision_in_research_ledger

no_permanent_truth: a pattern/model classified VALIDATED must remain
subject to future testing. VALIDATED = supported by current evidence, not
permanently proven.

change_policy: new data can strengthen existing hypothesis or contradict
it. Strong contradictory evidence from VERIFIED data downgrades/rejects.
Evidence hierarchy includes validated_simulation, small_sample_observation.

after_failure: identify active patterns affected, reassess.

## Model versioning

- immutable_versions: true
- per version: version_id, validation_results, out_of_sample_results
- events: ASSUMPTION_REJECTED, PATTERN_REVISED, CONCEPT_DRIFT_DETECTED
- track_rejected_hypotheses (rejected hypotheses are research data, never
  deleted; old assumptions replaced are recorded)
- display: patterns_reactivated, old_hypotheses_replaced
- session_identification

## Reliability / capture (fed earlier)

- capture_latency_measurement
- cdp_timeout_protection
- browser_recovery
- collector_watchdog
- data_health_watchdog
