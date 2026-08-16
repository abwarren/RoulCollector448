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

objective: predict the probability distribution of the next roulette
outcome using only information available before that outcome occurs
(P003: blind and out-of-sample).

mandatory_rule: EVERY prediction must be generated, timestamped, frozen,
and persisted BEFORE the actual next spin is observed. A prediction that
is created or modified after the outcome is visible is void — the frozen
pre-spin record is the only admissible prediction (this is the P003
enforcement at the storage level: no post-hoc prediction can ever enter
the ledger).

prediction_output:
- all_37_probabilities: true (the output is a full 37-way distribution
  over 0-36, never a single number)
- required fields:
  - prediction_id
  - timestamp
  - model_version
  - current_regime
  - current_cycle
  - feature_snapshot
  - probability_distribution
  - top_1
  - top_3
  - top_5
  - top_10

## Prediction models

model_lifecycle: [fragment pending — continues on the next feed]
- DISCOVERED (a candidate model structure proposed by the miner/model
  search — not yet trained, evaluated, or trusted; carries no claim)
- TESTING (the candidate is being trained + evaluated on the discovery
  split and validated on the validation split — walk-forward rules apply,
  no future data, results recorded but not yet trusted)
- VALIDATED (passed in-sample statistical challenge — corrected p-value,
  material effect size, CI excluding the null, beat the baseline)
- OUT_OF_SAMPLE_VERIFIED (passed the held-out out-of-sample evaluation —
  the effect reproduces outside the discovery period; the only state that
  may inform ACTIVE prediction per P003)
- ACTIVE (currently contributing to the ensemble/predictions)
- DECAYING (rolling performance declining / significance lost / regime
  change — weight reduced, monitored for FAILED or recovery)
- RETIRED (no longer contributing; version preserved immutably with all
  validation/OOS records)
- REACTIVATED (a RETIRED model returns when fresh walk-forward evidence
  re-validates it — re-verified against baseline before reactivation)

### no_signal_state
enabled: true
behavior: if no model provides evidence materially better than baseline,
the probability distribution must move toward the appropriate baseline
and the system must EXPLICITLY report that no validated predictive signal
is currently detected. (Honesty over confidence — the default answer is
"no signal", never a fabricated edge.)

## Simulation engine

objective: establish how often apparently strong patterns, cycles,
regimes, and prediction results occur NATURALLY in random data — the null
calibration for the whole research engine.

monte_carlo:
- enabled: true
- preserve_appropriate_baseline: true (each simulation preserves the
  appropriate null — fair wheel, markov, or randomized sequence)
- simulations: configurable, default 10000
- outputs: observed_statistic, simulation_mean, simulation_standard_
  deviation, 95_percentile, 99_percentile, empirical_p_value, effect_size

requirement: run the SAME pattern-discovery and cycle-mining logic against
simulated fair datasets to estimate the FALSE-DISCOVERY RATE of the
research engine itself. (If the miner finds "strong" patterns in random
data 30% of the time, its discovery threshold is calibrated to that —
the engine's own FDR is measured, not assumed.)

## Cycle and pattern prediction

objective: test whether discovered cycles, nested cycles, pattern regimes,
or recurring structures provide MEASURABLE predictive information about
the next spin.

features (the per-spin feature set the prediction models consume):
- current_cycle_position
- cycle_completion_percentage
- coverage_velocity
- coverage_acceleration
- completion_tail
- active_overlapping_cycles
- cycle_fingerprint
- historical_cycle_similarity
- active_pattern_count
- pattern_activation_strength
- pattern_reactivation
- current_regime
- historical_regime_similarity
- neighbour_activity
- pair_chain_activity
- double_activity
- colour_activity
- wheel_distance_activity
- entropy

cycle_conclusions:
- requirement: The system must not conclude that because a wheel is
  balanced or because a cycle is incomplete, a particular number is
  mathematically due. Instead it must measure whether cycle position or
  cycle structure has historically changed the conditional distribution
  of the next outcome. ("Due" is a gambler's fallacy until validated
  evidence shows a real conditional effect.)

required_comparison (how the cycle/prediction claim is established):
- cycle_feature_present_vs_absent (same data, cycle features on vs off)
- cycle_phase_comparison (across cycle positions/phases)
- cycle_to_cycle_comparison (across different cycles)
- nested_cycle_comparison (with vs without nested-cycle features)
- random_baseline_comparison (vs simulated fair data)
- out_of_sample_prediction_performance (the only acceptance test)

## Entropy and information

metrics:
- number_entropy
- transition_entropy
- colour_entropy
- neighbour_entropy
- wheel_distance_entropy
- pattern_entropy
- regime_entropy

purpose: detect periods where activity becomes more CONCENTRATED or more
DISTRIBUTED than the relevant baseline.

rule: entropy changes are DESCRIPTIVE signals and must be independently
validated before being treated as predictive features (no entropy-based
edge is claimed without its own validation).

## Return time analysis

track (the intervals being measured):
- number_return_time
- pair_return_time
- sequence_return_time
- neighbour_cluster_return_time
- colour_run_return_time
- cycle_return_time

analyze (per tracked interval):
- mean
- median
- variance
- distribution
- clustering
- short_returns
- long_returns
- historical_comparison

## Pattern reactivation

objective: detect when a historically observed pattern becomes active
again after a period of inactivity.

record:
- pattern_id
- previous_activation_period
- current_activation_period (the ongoing activation: when it started, how
  long it has run, and how the current episode compares to the previous
  one — same structure, different duration, stronger/weaker activation)
- activation_strength (the measured intensity of the current activation —
  e.g. how far the pattern's frequency deviates from its baseline during
  the episode, or the pattern's current contribution to predictions;
  recorded per episode so reactivations can be compared quantitatively)
- recurrence_interval (the time/spin gap between activations — how long
  the pattern was INACTIVE before this reactivation; feeds the return-time
  distribution and the pattern-reactivation predictions)
- historical_prediction_performance (how the pattern's activations have
  performed as PREDICTORS across past episodes — hit rates, log loss,
  contribution to ensemble performance during each activation — so a
  reactivation is only trusted if the pattern's historical predictive
  record justifies it)
- current_prediction_performance (the pattern's live prediction record
  during the CURRENT activation — being measured in real time against the
  same metrics, so the reactivation's performance is tracked from the
  first spin it informs)

pattern_activation_statuses: INACTIVE, EMERGING, ACTIVE, REACTIVATING,
DECAYING.

## Research ledger

objective: maintain an IMMUTABLE scientific-style record of every
important hypothesis, model, pattern, cycle, prediction system, validation
result, rejection, and reactivation.

### pattern entries (required)
- pattern_id
- definition
- discovery_time
- dataset_range
- sample_size
- baseline
- observed_statistic
- statistical_test
- validation_result
- out_of_sample_result
- current_status

### model entries (required)
- model_id
- version
- features
- training_range
- validation_range
- out_of_sample_range
- prediction_count
- top_1
- top_3
- top_5
- log_loss
- brier_score
- calibration
- baseline_difference
- status

rule: historical research results must NEVER be rewritten to make a model
or pattern appear more successful than it actually was. (The ledger is
append-only; corrections are new entries with a reference, never in-place
edits — P005's honesty guarantee at the record level.)

## Prediction explainability

objective: every prediction should be explainable in terms of the
information and model components that influenced its probability
distribution.

output: [continues in the next feed fragment]
- top_predicted_numbers (the top-N numbers from the probability
  distribution with their probabilities — the headline of any
  explanation: what the model actually predicted)
- probability_distribution (the full 37-way distribution the explanation
  refers to — the complete set of probabilities the top-N is drawn from,
  so the explanation can be checked against the actual prediction)
- dominant_features (the feature groups that most influenced this
  prediction — e.g. regime state, cycle position, neighbour activity —
  ranked by their contribution, per the model_attribution record)
- active_patterns (the currently-active validated patterns that shaped
  this prediction — which patterns were in play, their activation
  strength, and how each contributed to the distribution)
- active_cycle (the current coverage-cycle state that shaped this
  prediction — cycle id/position, completion percentage, velocity, and
  the cycle-based contribution to the distribution)
- active_regime (the current regime state that shaped this prediction —
  regime id, duration, and the regime-conditional contribution)
- historical_regime_matches (the similar historical regimes the current
  one is being compared against, with their similarity scores)
- model_component_contributions (per-component contribution weights from
  the ensemble — how much each model shaped the final distribution)
- confidence (the model's own confidence in this prediction — calibrated
  where possible, never inflated)
- baseline_difference (the distribution's divergence from the baseline —
  uniform-37 / historical / recent / markov — the honest statement of how
  unusual this prediction is)

rule: the explanation must describe the EVIDENCE the model used and must
NOT imply causation merely because a feature was associated with a
prediction. (Association ≠ cause; the explanation reports influence, not
mechanism.)

## Data quality requirements

### canonical_dataset target
- recent_500_verified: "500/500"
- unresolved_recent_discrepancies: 0

### historical_dataset requirements
- retain_verified_records_indefinitely
- retain_raw_observations_for_forensic_audit
- retain_repair_history
- track_unverified_records_explicitly

### provenance
every_canonical_spin_traceable_to:
- raw_observation
- collector_session
- source
- timestamp

## Dashboard sections

### data_integrity — display
- integrity_score
- verified_count
- unverified_count
- missing_count
- duplicate_count
- conflict_count
- recent_repairs
- last_reconciliation

### current_regime — display
- regime_id
- duration
- active_patterns
- decaying_patterns
- historical_regime_matches

### cycles — display:
- active_cycles (the currently-active coverage cycles — including
  overlapping ones — with id, position, completion %, and velocity)
- cycle_completion (per-cycle completion progress — unique count vs 37,
  missing numbers remaining, coverage curve at the standard coverage
  points)
- coverage_velocity (current coverage speed per active cycle)
- completion_tail (the current completion-tail state — which numbers are
  still missing as the cycle approaches completion)
- nested_cycles (the nested/inner cycles currently in play and their
  position within the outer cycle)
- historical_cycle_matches (similar past cycles with similarity scores)

### patterns — display:
- active_patterns
- reactivating_patterns
- pattern_strength
- sample_size
- historical_performance
- out_of_sample_status

### prediction_lab — display:
- next_spin_probability_distribution
- top_1
- top_3
- top_5
- current_model_version
- current_regime
- current_cycle
- rolling_accuracy
- baseline_accuracy
- log_loss
- brier_score
- calibration
- model_status

### research — display:
- validated_patterns
- rejected_patterns
- decaying_patterns
- model_comparison
- out_of_sample_results
- simulation_results

## Testing

### unit_tests — data_integrity (the integrity test matrix)
- missing_spin
- duplicate_spin
- conflicting_game_id
- invalid_number
- invalid_color
- timestamp_anomaly
- out_of_order_spin
- source_disagreement

## Loop / recurrence detector

- objective: determine whether an apparent loop exists in:
  - overlapping_repeating_block (the same block overlapping across the
    sequence)
  - partial_sequence_recurrence (a partial sequence recurring)
  - nested_sequence (a sequence nested inside a larger one)
  - wheel_relative_recurrence (recurrence in wheel-relative terms)
  - transition_matrix_recurrence (recurrence in the transition matrix)
  - cycle_fingerprint_recurrence (recurrence of cycle fingerprints)

### validation (required)
- operate_on_raw_chronological_order (the analysis runs on the raw
  chronological sequence, never on a reordered/displayed view)
- compare_against_random_baseline
- measure_occurrence_count
- measure_sequence_length
- measure_recurrence_interval
- measure_overlap
- measure_statistical_significance
- validate_out_of_sample

### display_order_warning
rule: never infer a temporal pattern solely from the visual arrangement of
rows — e.g. "9 8 7 6 5 3 2 1 0 36 35 34 33 32 ..." descending display order
must never be mistaken for the actual chronological order of outcomes.

- prediction_ui_visible: true
- evaluation_engine_operational: true
- automated_tests_passing: true
- prediction_integrity rule: the Prediction Lab must display the live
  frozen prediction, but displaying the prediction must NEVER allow the
  actual next result to influence or modify that prediction. (The display
  is a one-way mirror — read-only onto the frozen record; the outcome
  lands after the freeze and only feeds the evaluation, never the
  prediction.)

framing (the three-question distinction that defines the system's shape):
- Discovery: What has the data discovered?
- Prediction: What does the independent model predict next?
- Evaluation: Was the prediction actually any good?

- rule: predictions, once generated, are immutable. New data may change
  future predictions, but must NEVER rewrite previous predictions (the
  frozen pre-spin record is the only admissible prediction — P003).
- discovery_prediction_boundary: the discovery engine may produce
  validated features that the prediction engine can use, but the
  prediction engine must consume them through a versioned, timestamped
  interface. Prediction-time data must be frozen and auditable.
- prediction: "WHAT THE MODEL PREDICTS NEXT" (the human-readable headline
  of the frozen prediction — the top outcome(s) the model expects next,
  drawn from the frozen probability_distribution, displayed with the
  versioned/timestamped provenance so it is always auditable)
- evaluation: "HOW THE PREDICTION PERFORMED" (the post-outcome scoring
  headline — filled in after the spin lands: did the frozen prediction
  hit (top-1/3/5/10), what were the log loss / Brier / calibration
  contributions, and how does this compare to the baselines; always
  scored against the SAME frozen pre-spin prediction, never a revised one)

critical_rule: the user must ALWAYS be able to see the prediction, but
visibility must never compromise the blind nature of the prediction
experiment. (Observing a prediction must not allow it to be revised —
the freeze happens before the outcome, and the display is a read-only
window onto the frozen record.)

final_requirement: prediction is an INDEPENDENT experimental system. The
user can observe every prediction in real time, including the predicted
number, ranked alternatives, probabilities, model version, current
regime, current cycle, and confidence. After the next spin occurs, the
system automatically records whether the prediction was correct and
updates the prediction-performance history.

The UI must have three clearly separated areas:

### DISCOVERY ENGINE
- Current regime: R-184
- Active patterns: 172022
- Coverage cycle: 73%
- Nested cycles: 3

### PREDICTION LAB
- NEXT SPIN (ranked probabilities, frozen):
  #1  20   8.4%
  #2  22   7.7%
  #3  19   6.9%
  #4  17   6.2%
  #5  32   5.8%
- Status: FROZEN

### PREDICTION PERFORMANCE
- Scope: Last 10k predictions (every figure below is measured over the
  most recent 10,000 frozen predictions — the standard reporting window)
- Top-1: 3.81% [continues on the next feed fragment]
- Top-3: 11.4%
- Log loss: [value pending — continues on the next feed fragment]
- Baseline: 2.70% (the uniform single-number baseline — shown alongside
  every performance figure so the edge over random is always visible)
- [performance graph] — the rolling performance visualization (accuracy /
  log-loss / calibration curves over time vs the baseline, so decay,
  improvement, and regime shifts are visible at a glance)

## Self-critical adaptive learning

## Confidence decay

rule: confidence in a pattern or model must naturally decline when new
evidence fails to support it. (Confidence is earned continuously; it is
not a stored entitlement. Absence of new supporting evidence is itself a
negative signal.)

factors (what drives the decay):
- time_since_last_confirmation
- new_sample_size
- prediction_failure_rate
- effect_size_decline
- regime_change
- baseline_performance
- out_of_sample_failure

## Evidence weighting

priority (the evidence hierarchy — how much each kind of evidence counts
when challenging or supporting a conclusion):
- verified_recent_data (highest — the current verified window)
- large_out_of_sample_dataset
- large_historical_dataset
- validated_simulation
- small_sample_observation (weakest real evidence)
- human_assumption (lowest — an assumption carries no evidentiary weight
  on its own)

rule: human assumptions are hypotheses only. They must NEVER override
sufficiently strong contradictory evidence from verified data.

objective: the system must continuously challenge its own assumptions,
models, pattern definitions, cycle interpretations, regime classifications,
and predictive conclusions. No conclusion is permanent. New verified data
must be capable of changing or invalidating previous conclusions.

core_principle: evidence OUTRANKS assumptions. The system must never
preserve an existing hypothesis merely because it was previously
considered valid.

### assumption_registry
purpose: maintain an explicit registry of every assumption used anywhere
in the discovery, cycle, regime, statistical, or prediction systems.

fields:
- assumption_id
- assumption_description
- created_at
- created_from_evidence
- supporting_dataset
- sample_size
- confidence
- current_status
- last_challenged
- last_validated
- contradictory_evidence
- revision_history

statuses: PROVISIONAL, SUPPORTED, WEAKENING, CONTRADICTED, REJECTED,
REPLACED

### assumption_challenge
trigger: new_verified_data, regime_change, pattern_decay,
prediction_failure, out_of_sample_failure, statistical_significance_loss,
baseline_improvement, new_competing_hypothesis, data_source_change

process: identify_assumptions_affected_by_new_data →
recalculate_assumption_evidence → search_for_contradictory_evidence →
compare_old_and_new_results → determine_if_assumption_still_holds →
downgrade_or_reject_if_required → generate_replacement_hypothesis_if_supported
→ retest_replacement → record_decision_in_research_ledger

### no_permanent_truth
rule: a pattern or model classified as VALIDATED must remain subject to
future testing. VALIDATED means supported by the current evidence, not
permanently proven.

### change_policy
new_data_can: strengthen_existing_hypothesis, weaken_existing_hypothesis,
change_pattern_definition, change_regime_boundary,
change_cycle_interpretation, change_model_weight, retire_model,
reactivate_old_model, create_new_model, invalidate_previous_conclusion

### model_challenge
requirement: every active predictive model must continuously compete
against simpler models, historical models, alternative hypotheses, and
baseline models.
actions: retest_active_models, compare_against_baselines,
compare_against_previous_model_version, test_new_candidate_models,
measure_out_of_sample_performance, retire_if_evidence_deteriorates

### pattern_challenge
requirement: every active pattern must periodically be challenged with
newly accumulated data. The system must actively search for evidence that
the pattern no longer exists.
tests: current_frequency_vs_historical, current_effect_size_vs_historical,
out_of_sample_performance, regime_specific_performance,
random_baseline_comparison, alternative_pattern_explanation

### regime_adaptation
requirement: when the statistical structure of the data changes, the
system must be able to abandon assumptions derived from the previous
regime and rebuild its understanding from the new regime.
behavior:
- old_regime: preserve true, continue_learning false (preserved
  historically, no longer learned from)
- new_regime: detect true, create_new_regime true,
  rebuild_pattern_distribution true, reevaluate_model_weights true,
  reevaluate_active_hypotheses true

### concept_drift
objective: detect when relationships that were historically useful no
longer describe the current data.
detection: prediction_performance_decay, distribution_shift,
transition_shift, entropy_shift, cycle_structure_shift,
pattern_frequency_shift, regime_change
response: flag_concept_drift, reduce_confidence, reduce_model_weight,
retrain, search_for_new_patterns, compare_new_and_old_models

### contradiction_engine
objective: actively search for evidence that contradicts the system's
STRONGEST current conclusions.
requirements: maintain_top_active_hypotheses,
generate_counter_hypotheses, search_for_counter_examples,
test_counter_hypotheses, record_results (every counter-test outcome —
whether it confirms or refutes the hypothesis — is recorded in the
research ledger; a failed contradiction search is itself evidence, and a
successful one is a rejection)

rule: the system must spend analytical resources trying to disprove
strong conclusions, not merely confirming them. (Confirmation is cheap
and self-serving; the analytical budget must be weighted toward
attempted refutation of the strongest claims — a conclusion that has
never been seriously attacked is not yet trusted.)

## Model learning

model_comparison:
- requirement: Complex models must be compared against simpler models.
  Complexity must not be treated as evidence of superiority. (A complex
  model earns its place ONLY by a demonstrated, validated out-of-sample
  improvement over the simpler model it subsumes — P004's
  simpler_model_explains_same_effect clause; the comparison is recorded
  with every model's lifecycle record.)

objective: determine which models and discovered structures are USEFUL
under which regimes and temporal conditions.

### adaptive_weights
inputs:
- walk_forward_accuracy
- log_loss
- brier_score
- calibration
- out_of_sample_performance
- current_regime_performance
- historical_regime_performance
- sample_size

rules:
- increase_weight_only_after_validated_improvement
- reduce_weight_when_performance_decays
- do_not_reweight_using_future_data (P003)
- regularize_weight_changes (no spikes)

### model_decay
detect:
- rolling_performance_decline
- loss_of_statistical_significance
- regime_change
- out_of_sample_failure

statuses: STABLE, IMPROVING, DECAYING, FAILED

### regime_specific_performance
requirement: measure each model INDEPENDENTLY BY REGIME rather than
assuming a model has constant performance across all historical
conditions. A model may be excellent in one regime and noise in another;
only regime-conditional performance justifies regime-conditional
weighting.

### baseline (the simplest comparators — every specialist/ensemble claim
must beat these, per P004)
- uniform_37 (1/37 each)
- historical_frequency (marginal frequency over all verified history)
- recent_frequency (marginal frequency over the recent window)
- simple_markov (first-order transition model)

### specialist (each a distinct hypothesis about the sequence)
- transition_model (from-current-number transition probabilities)
- higher_order_transition_model (N-gram transitions)
- neighbour_model (wheel-adjacency structure)
- pair_chain_model (consecutive-pair chains)
- sequence_model (sequential pattern hypotheses)
- wheel_relative_model (wheel-relative sequence structure)
- cycle_model (coverage-cycle position)
- regime_model (current-regime-conditional distribution)
- coverage_model (coverage state / missing-number structure)
- deep_pattern_model (the pattern-miner's validated hypotheses)

### ensemble
objective: combine model probability distributions while allowing model
weights to adapt according to VALIDATED WALK-FORWARD performance.

requirements:
- weights_must_be_learned_from_historical_performance
- weights_must_not_use_future_results (walk-forward only — no lookahead,
  P003)
- weights_must_be_regularized (no overfit weight spikes)
- no_single_component_should_dominate_without_validation (a component may
  dominate only while its validated walk-forward edge justifies it)

### model_attribution
every prediction should record:
- component_contribution (how much each component model shaped the final
  distribution)
- feature_contribution (how much each feature group contributed)
- regime_contribution (the share of the prediction attributable to the
  current-regime-conditional component)
- cycle_contribution (the share attributable to the cycle-position
  component)
- neighbour_contribution (the share attributable to the wheel-neighbour
  component)
- sequence_contribution (the share attributable to the sequential-pattern
  component)

## Blind prediction evaluation

walk_forward:
- enabled: true
- rule: training and discovery information must ALWAYS precede the
  prediction. No future observations may leak into feature generation,
  model selection, weighting, or hyperparameter selection (P003 — the
  walk-forward discipline makes leakage structurally impossible).
- workflow:
  1. train_on_available_history (only data before the prediction point)
  2. freeze_model (the exact trained state, versioned)
  3. generate_next_spin_prediction
  4. freeze_prediction (timestamped, persisted pre-spin)
  5. observe_actual_spin
  6. score_prediction (top-N hits, log loss, brier, calibration)
  7. append_result (to the prediction record + performance tracking)
  8. retrain_or_update (only with data now available — never the just-
     scored future beyond this point)
  9. repeat

data_splits:
- default: discovery 0.70 / validation 0.15 / out_of_sample 0.15
- rule: exact proportions may change for rolling time-series validation,
  but future data must NEVER be used to improve a model before that
  future period is evaluated.

performance_tracking:
- measure_over: 100 / 500 / 1000 / 5000 / 10000 / 25000 / 50000 /
  100000 predictions, all_available
- track:
  - rolling_accuracy
  - cumulative_accuracy
  - baseline_difference (vs uniform-37 + the other baselines)
  - rolling_log_loss
  - rolling_brier_score
  - calibration
  - confidence_interval
  - model_weight (ensemble weights over time — drift visible)
  - model_decay (per-model performance decay, the DECAYING signal)

objective: determine whether the prediction engine provides GENUINE
predictive information and whether performance improves, remains stable,
or decays over time (persistence/changes measured across the walk-forward
timeline, feeding the assumption-ledger triggers: prediction_failure,
out_of_sample_failure, statistical_significance_loss).

mandatory_evaluation (every model version is scored on ALL of these):
- top_1_accuracy
- top_3_accuracy
- top_5_accuracy
- top_10_accuracy
- log_loss
- brier_score
- calibration (reliability: do predicted probabilities match observed
  frequencies?)
- confidence_intervals
- effect_size
- baseline_comparison (vs the baselines below — the ONLY way "genuine
  predictive information" is established)

baseline:
- uniform_probability_per_number: 0.027027027 (37 outcomes → ~2.70% per
  spin; a single-number uniform baseline. Every claimed improvement must
  beat this AND the other baselines (historical/recent frequency, simple
  markov) with a material effect size and a CI that excludes the null.)

prediction_record (the frozen per-prediction row — written BEFORE the
outcome, then the outcome columns filled in after the spin lands):
- prediction_id
- prediction_timestamp
- model_version
- model_components
- regime (the current regime at prediction time — regime-conditional
  context frozen into the record, mirroring prediction_output.current_regime)
- cycle (the current coverage-cycle position at prediction time —
  cycle-conditional context frozen into the record, mirroring
  prediction_output.current_cycle)
- feature_snapshot (the exact feature values the model consumed for THIS
  prediction — the state the probabilities were computed from, frozen
  verbatim so any prediction can be reproduced or audited later)
- probability_distribution
- top_1
- top_3
- top_5
- actual_number
- top_1_hit
- top_3_hit
- top_5_hit
- top_10_hit
- log_loss
- brier_score

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
- requirement: NEVER overwrite an old model. Create a new version whenever
  assumptions, features, weights, architecture, or training methodology
  materially change. (The old version stays immutable — the audit trail of
  what was believed and when.)
- each_version_records: version_id, created_at, training_dataset,
  assumptions, features, weights, validation_results,
  out_of_sample_results, reason_for_change, previous_version
- events: ASSUMPTION_REJECTED, PATTERN_REVISED, CONCEPT_DRIFT_DETECTED
- track_rejected_hypotheses (rejected hypotheses are research data, never
  deleted; old assumptions replaced are recorded)
- display: patterns_reactivated, old_hypotheses_replaced
- session_identification

## Research ledger — adaptive events

The ledger records every adaptive event as an entry:
- ASSUMPTION_CREATED
- ASSUMPTION_SUPPORTED
- ASSUMPTION_WEAKENED
- ASSUMPTION_CONTRADICTED
- ASSUMPTION_REJECTED
- NEW_HYPOTHESIS_CREATED
- PATTERN_REVISED
- PATTERN_RETIRED
- MODEL_UPDATED
- MODEL_RETIRED
- REGIME_CHANGED
- CONCEPT_DRIFT_DETECTED

decision_rule: when new verified data materially contradicts an existing
assumption, the system must prefer changing the assumption over forcing
the new data to fit the old assumption. (The data is the ground truth;
the assumption yields — P005's decision rule in one sentence.)

## Anti-confirmation bias

requirements (the structural anti-bias guarantees):
- actively_search_for_negative_evidence (the system must look FOR
  disconfirming evidence, not wait for it to arrive)
- track_failed_predictions (failures are recorded and analyzed, never
  dropped)
- track_failed_patterns (patterns that failed validation are kept and
  counted)
- track_rejected_hypotheses (rejected hypotheses remain as research data)
- test_competing_explanations (alternatives are tested, not ignored)
- avoid_selective_reporting (the dashboard and ledger show failures
  alongside successes — no cherry-picking)
- display_conflicting_evidence (contradictions are surfaced, not hidden)

## Self-criticism dashboard

display:
- strongest_current_assumptions
- assumptions_under_pressure
- contradictory_evidence
- models_improving
- models_decaying
- patterns_reactivated
- patterns_rejected
- recent_concept_drift
- recent_prediction_failures
- new_hypotheses
- old_hypotheses_replaced

ultimate_rule: the system must be designed to change its mind. New
verified evidence must be able to alter pattern definitions, cycle
interpretations, regime boundaries, model weights, prediction strategies,
and previously accepted conclusions. (Nothing is beyond revision —
P005 as the system's ultimate rule.)

## Learning from failure

requirement: prediction failures must be treated as INFORMATION about the
model rather than merely as incorrect predictions. (A failure is a
measurement, not a verdict — the system's response to failure is to learn,
per P005.)

after_failure (the post-failure learning sequence):
- record_prediction
- identify_contributing_features
- identify_active_regime
- identify_active_cycle
- identify_active_patterns
- determine_which_assumptions_were_involved
- test_whether_assumptions_still_hold
- update_model_if_supported
- record_learning_event

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

quality_gate:
- do_not_declare_complete_if:
  - tests are failing
  - critical errors remain
  - required functionality is stubbed
  - required functionality is mocked instead of implemented
  - data integrity is unreliable
  - prediction pipeline leaks future data
  - critical APIs are broken
  - frontend cannot load
  - database cannot connect

completion_rule: do not stop merely because the specification,
documentation, schemas, or individual modules are complete. Continue until
the integrated application [is complete, operational, tested, and
accessible at http://localhost:4480/ — fragment truncated; continues on
the next feed]

objective: take RoulCollector448 from its current state through
implementation, integration, testing, debugging, validation, and local
deployment. Continue working until the application is complete,
operational, tested, and accessible at http://localhost:4480/.

autonomy:
- enabled: true
- non_stop_execution: true
- rule: do not stop and ask the user for permission between
  implementation steps. Continue autonomously through the entire
  development lifecycle.
- do_not_ask_permission_for: creating files, modifying source code,
  refactoring code, installing required dependencies, creating database
  tables or migrations, adding tests, running tests, fixing test
  failures, debugging runtime errors, starting or restarting local
  services, creating configuration files, creating environment files
  where safe and appropriate, implementing APIs, implementing frontend
  components, implementing background workers, implementing data
  reconciliation, implementing pattern mining, implementing cycle
  detection, implementing regime detection, implementing prediction
  evaluation, performing local backtesting, performing Monte-Carlo
  testing, running linting, running type checks, running integration
  tests, fixing discovered defects, restarting failed services,
  re-running failed tests after fixes

exception: ask the user only when an action requires information,
credentials, authorization, an external service, or a decision that
cannot reasonably be inferred or safely completed autonomously.

completion_definition — the application is NOT complete until:
- backend is implemented
- frontend is implemented
- database layer is operational
- data integrity layer is operational
- 500-spin reconciliation is operational
- repair workflow is operational
- deep pattern mining is operational
- cycle engine is operational
- overlapping cycle detection is operational
- nested cycle detection is operational
- regime engine is operational
- statistical validation is operational
- Monte-Carlo/random baseline testing is operational
- blind prediction engine is operational
- prediction evaluation is operational
- walk-forward evaluation is operational
- model/version tracking is operational
- self-critical assumption engine is operational
- research ledger is operational
- dashboard is operational
- automated tests pass
- integration tests pass
- application starts successfully
- application remains running
- application is accessible over HTTP
- application is accessible at http://localhost:4480/
- critical workflows have been manually or programmatically verified

## Development loop

repeat_until_complete:
- inspect_current_state [continues on the next feed]

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

port_conflict_rules:
- safely resolve the conflict rather than silently selecting another port
- do NOT declare completion while the application is only available on
  another port
- verify the endpoint programmatically before declaring completion

local_deployment:
- required: true
- final_verification (all must pass before completion is declared):
  - start_application
  - verify_backend_health
  - verify_frontend_load
  - verify_database_connection
  - verify_required_background_services
  - HTTP GET http://localhost:4480/
  - verify_expected HTTP status
  - verify application content
  - verify critical API endpoints
  - verify no fatal runtime errors
  - run final regression suite

autonomous_debugging (the diagnostic toolkit used without asking):
- read_logs
- inspect_stack_traces
- inspect_processes
- inspect_ports
- inspect_database_state
- inspect_network_requests
- inspect_browser_console_errors
- inspect_application_health
- make_reasonable_repairs (apply safe, reversible fixes autonomously —
  restart a service, correct a config, patch an obvious defect — and
  verify the fix; escalate only per the exception rule)
- retest_after_repairs (every repair is followed by a re-test — re-run
  the failing tests / health checks / endpoint probes — and the repair is
  only considered successful when the re-test passes)

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
- fails_validation (the hypothesis fails its statistical validation: the
  corrected p-value does not clear the family-wise threshold, or the
  effect size is not material, or the CI straddles the null — the primary
  rejection condition; a failed validation sends the hypothesis to
  REJECTED with the full test record preserved)
- fails_out_of_sample (the hypothesis passed validation but fails on the
  held-out/out-of-sample data: the effect does not reproduce, the
  out-of-sample effect size is materially smaller or not significant, or
  the out-of-sample CI does not contain the discovery effect size —
  per P003, out-of-sample performance is the only performance that
  counts; an OOS failure demotes an OUT_OF_SAMPLE_VERIFIED hypothesis to
  REJECTED with the OOS record appended)
- not_significantly_different_from_baseline (the hypothesis's effect is
  not statistically distinguishable from its chosen baseline — uniform-37
  outcome, historical frequency, recent frequency, or the markov/
  randomized/fair-wheel nulls: even if internally "significant" against a
  straw null, a pattern that the appropriate baseline reproduces equally
  well is NOT a discovery — per P004, every claimed edge must beat the
  simpler baseline; this rejection is recorded with the baseline + the
  comparison statistic)

All comparisons must also respect P004: multiple-testing correction when
many candidate patterns are scanned, reproducibility in a fresh sample,
and beat the simpler baseline.
