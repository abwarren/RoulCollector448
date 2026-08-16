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

## Neighbours (wheel-adjacency patterns)

Neighbour analysis on the wheel layout (roulette wheel order, e.g.
0-32-15-19-4-21-2-25-17-34-6-27-13-36-11-30-8-23-10-5-24-16-33-1-20-14-31-9-22-18-29-7-28-12-35-3-26 for a single-zero wheel).

Pattern shapes include:
- "Nn -> Nn -> Nn" (neighbour sequences — the physical wheel order)
- "R -> R -> B -> B" (colour sequences / structural forms)

## Agent execution directive

objective: take RoulCollector448 from its current state through
implementation, integration, testing, debugging, validation, and local
deployment. Continue working until the application is complete,
operational, tested, and accessible at http://localhost:4480/.

autonomy:
- enabled: true
- non_stop_execution: true

workflow: identify_missing_requirements → implement_next_requirement →
run_relevant_tests → inspect_failures → debug → fix → retest →
run_regression_tests → continue_to_next_requirement

failure_handling: a failure/exception/compile error/failed test/dependency
problem/port conflict/DB error/browser error/runtime crash is NOT a reason
to stop. Diagnose → fix → restart the affected component → continue.

escalation: first diagnose, second attempt_safe_automatic_fix, third
retest, fourth try_alternative_implementation, fifth — ask the user only
if the problem genuinely requires unavailable information, credentials,
hardware access, or an external decision.

port_requirement: frontend_or_application_port 4480;
required_url http://localhost:4480/; protocol HTTP. The final application
must be reachable at exactly http://localhost:4480/. If port 4480 is
occupied by a stale development process, diagnose and resolve (kill the
stale process, restart cleanly).

## Pattern identity rules

Exact patterns and structurally equivalent patterns must be tracked
separately and must NEVER be treated as identical without evidence.
(Example: "17 -> 20 -> 22" exact vs its structural colour/parity/neighbour
equivalents — a structural match is a hypothesis to test, not an identity.)

## Cycle engine

objective: discover coverage cycles and overlapping cycles across the
verified sequence, including cycles that begin before previous cycles
have completed.

roulette_outcomes: count 37, values 0-36.

cycle_definition:
- strict_cycle: starts at the first observed unique outcome after the
  previous cycle reset; completes when all 37 outcomes have appeared.
- overlapping_cycle: new candidate cycles may begin while earlier cycles
  are still incomplete; multiple active cycles tracked simultaneously.

cycle_metrics (per cycle): cycle_id, start_spin, end_spin, duration_spins,
unique_count, missing_numbers, completion_status, coverage_curve,
coverage_velocity, coverage_acceleration, completion_tail, duplicate_rate,
colour_distribution, neighbour_activity, double_activity, pair_activity,
sequence_activity, entropy, cycle_fingerprint.

coverage_points: 10, 20, 25, 30, 32, 34, 35, 36, 37 (the milestones a
cycle's coverage curve is sampled at).

cycle_analysis: cycle_length_distribution, cycle_similarity,
cycle_fingerprint_similarity (similarity of full cycle fingerprints —
coverage curve + velocity + duplicate rate + colour distribution + entropy
+ activity metrics — between cycles, the basis for cycle-family clustering),
cycle_completion_tail (the final stretch of a cycle — the last few missing
numbers, typically the slowest: how long the tail takes, which numbers are
persistent tail-dwellers, tail length vs cycle length distribution).

cycle_analysis_extended (further cycle dimensions):
- early_coverage_velocity (coverage speed in the first stretch of a cycle)
- late_coverage_velocity (coverage speed near completion — typically slower)
- missing_number_dynamics (how the missing-number set evolves: which leave
  the set, which linger, replacement rate)
- physical_cluster_of_missing_numbers (are the missing numbers clustered
  on the wheel or scattered? cluster size / arc span of the missing set)
- overlapping_cycle_interaction (how concurrent cycles interfere: shared
  numbers, velocity coupling, one cycle's tail vs another's start)
- cycle_regime_relationship (how cycles relate to detected regimes: do
  cycle lengths/velocities differ across regimes?)
- cycle_reactivation (cycles that re-enter the active set / repeat their
  structure after apparent completion)

## Nested cycle analysis

objective: detect cycles occurring INSIDE larger coverage cycles and
determine whether inner structures recur at comparable positions or
phases of the larger cycles.

examples:
- colour_cycle_inside_37_number_cycle (a colour-coverage cycle nested in a
  full 37-number cycle)
- neighbour_cycle_inside_coverage_cycle
- double_cycle_inside_neighbour_regime
- pair_chain_inside_cycle
- coverage_cycle_started_before_previous_cycle_completed (overlapping
  cycles as a nested structure)

## Regime engine

objective: detect periods during which the statistical structure of the
sequence changes, WITHOUT assuming fixed session boundaries or
predetermined regimes.

regime_features (the per-window statistical profile that defines a
regime's identity):
- number_distribution
- transition_distribution
- colour_distribution
- neighbour_distribution
- wheel_distance_distribution
- repeat_distribution
- cycle_features
- sequence_features
- entropy
- pattern_activation
- prediction_performance

change_detection:
- preferred_methods: Jensen_Shannon_divergence (between adjacent-window
  feature distributions) — plus drift-based detection of regime changes.
- CUSUM (cumulative sum control chart on the feature streams / prediction
  error / coverage velocity — flags sustained shifts that a threshold test
  would miss; a first-class regime-change detector alongside JS-divergence).
- Page_Hinkley (Page-Hinkley test — a sequential change-point detector on
  the feature streams: accumulates deviations from the running mean and
  fires when the cumulative positive/negative drift exceeds a threshold;
  complements CUSUM with a different statistic and a natural forgetting of
  old baselines).
- change_point_detection (the umbrella capability: precise identification
  of the SPIN at which a regime/statistical structure changes — feeds the
  regime segmentation boundaries and the assumption-ledger triggers; JS-
  divergence, CUSUM and Page-Hinkley are the detectors, this is the
  change-point localization they jointly produce).

regime_boundaries_requirement: regime boundaries must be DATA-DRIVEN and
must NOT simply be based on midnight, calendar day, or collector restart
(session boundaries are convenience labels, never regime evidence).

## Regime lifecycle

regime_lifecycle states:
- DISCOVERED (a new statistical structure detected)
- ACTIVE (the regime is currently governing the sequence)
- STRENGTHENING (evidence for the regime is growing)
- DECAYING (evidence weakening)
- INACTIVE (no longer governing; retained historically)
- REACTIVATED (an INACTIVE regime returns — compared against its stored
  fingerprint before the reactivation is accepted)

## Regime memory

retain_historical_regimes: true
compare_current_to_historical: true

Per stored regime:
- regime_fingerprint
- duration
- dominant_patterns
- cycle_characteristics
- prediction_performance
- similar_historical_regimes

## Regime comparison

output (when comparing current vs historical):
- current_regime
- most_similar_historical_regimes
- similarity_score
- differences
- current_active_patterns

## Statistical validation

objective: determine whether discovered patterns differ materially from
appropriate random baselines and whether those differences persist
outside the discovery [window/sample — truncated in the feed; completes as:
outside the discovery window/sample — i.e. out-of-sample persistence,
P003/P004].

baseline_types (the "appropriate random baselines" a discovered pattern is
tested against — each is a distinct null):
- simple_markov_transition (a first-order Markov null: transition
  probabilities estimated from the data, then sequences simulated from it —
  tests whether a pattern is more than ordinary chain structure)
- appropriate_randomized_sequence (a permutation/randomization null: the
  observed sequence's values reshuffled, preserving the value multiset but
  destroying order — tests pure order effects)
- monte_carlo_fair_wheel (a fair-wheel null: i.i.d. uniform over 0-36,
  simulated at scale — tests against the ideal physical wheel)

methods (the statistical machinery used to compare pattern vs baseline):
- permutation_testing (exact/approximate permutation tests: shuffle the
  labels/order N times, count how often the null produces an effect as
  extreme as observed → empirical p-value)
- monte_carlo_simulation (simulate each baseline type at scale, derive the
  null distribution of the test statistic)
- confidence_intervals (report effect sizes with CIs, not just point
  estimates; a pattern whose CI straddles the null is not a claim)
- effect_size (a standardized magnitude of the discovered effect — e.g.
  Cohen's d / odds ratio / relative-risk style measures vs the chosen
  baseline — reported ALONGSIDE any p-value: statistical significance
  without a material effect size is not a claim, and the effect size is
  the quantity that must reproduce out-of-sample)
- p_value (the probability of observing an effect at least as extreme
  under the chosen null — reported WITH the effect size and CI, corrected
  for multiple testing per P004 when many candidate patterns are scanned;
  a p-value alone is never a claim, and a corrected p-value that fails to
  clear the family-wise threshold kills the pattern)
- false_discovery_rate (FDR control — Benjamini-Hochberg style — across
  the family of scanned candidate patterns: the expected proportion of
  false discoveries among the patterns declared significant. When the
  miner scans hundreds of candidates, raw p-values overstate the evidence;
  FDR-adjusted q-values are the decision quantity for which patterns
  survive to the reproducibility gate)
- multiple_testing_correction (the umbrella requirement: the system MUST
  account for the fact that searching thousands or millions of possible
  patterns naturally generates apparently significant patterns by chance.
  Applies family-wise (Bonferroni/Holm) or FDR (Benjamini-Hochberg) control
  over the entire candidate family per scan, and records the correction
  method + family size with every claimed pattern)

## False discovery policy

The system must NOT promote a pattern to a claim when any of these hold:
- do_not_promote_pattern_based_only_on_raw_frequency (a number/colour/run
  appearing often is not evidence — compare against the baseline
  frequency, not the raw count)
- do_not_promote_pattern_based_only_on_single_period (an effect in one
  time period is not a pattern — it must persist across periods/scales)
- do_not_promote_pattern_without_holdout_validation (every promotion must
  pass a held-out validation set, never the discovery data)
- do_not_promote_pattern_without_multiple_testing_control_where_applicable
  (if the pattern came from a large candidate search, correction applies)

## Hypothesis engine

lifecycle states:
- DISCOVERED (candidate found by the miner)
- UNDER_TEST (entering statistical validation)
- VALIDATED (passed in-sample statistical challenge)
- OUT_OF_SAMPLE_VERIFIED (passed holdout/out-of-sample verification)
- ACTIVE (currently informing prediction)
- DECAYING (evidence weakening)
- REJECTED (failed validation — recorded, never deleted)
- RETIRED (previously ACTIVE, no longer supported)
- REACTIVATED (a RETIRED hypothesis returns with new evidence)

hypothesis_record required fields:
- hypothesis_id
- discovery_timestamp
- pattern_definition
- discovery_dataset
- discovery_sample_size
- baseline
- validation_dataset
- validation_result
- out_of_sample_dataset
- out_of_sample_result
- statistical_tests
- current_status
- last_evaluated
- performance_history

rejection_conditions: [continues in the next feed fragment]

All comparisons must also respect P004: multiple-testing correction when
many candidate patterns are scanned, reproducibility in a fresh sample,
and beat the simpler baseline.
