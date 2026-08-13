# Invariants

Rules this codebase enforces rather than remembers. Each one exists because
breaking it produced a specific wrong answer, usually in a live run against a
real provider or a real model. Each is stated, pointed at the code that enforces
it, and pinned by a named test.

Most of them are the same failure in different clothes: **a value implying
knowledge it does not have.**

This file is the durable record. It is not a summary of anyone's memory — if a
rule here is not enforced by the named code and pinned by the named test, the
rule is not real.

---

## 1. Absence is not negation

> A provider that does not assert something has not denied it. Attributes where
> this matters are stored as `Attestation` — `confirmed_true` /
> `confirmed_false` / `unknown` — never as `bool | None`.

**Why.** Google returns `servesVegetarianFood: false` for most Shibuya
pizzerias, and every pizzeria serves a margherita. Measured live: **8/8** Indian
restaurants in Shinjuku attested, **1/8** Shibuya pizzerias. Its `false` means
"not attested". Treating it as a denial would have deleted most of a city from a
vegetarian's trip on no evidence.

| provider value | maps to | reasoning |
|---|---|---|
| `True` | `confirmed_true` | a positive assertion |
| `False` | `unknown` | Google cannot assert the negative |
| absent | `unknown` | nothing was said |
| — | `confirmed_false` | no current provider emits this; the state exists so one *could*, and so the code that reacts to it is written and tested |

**Enforced in.** `app/models/common.py` (`Attestation`, `as_attestation`,
`AttestationField`/`AttestationMap` — the validators also coerce trip JSON
written before the tri-state existed); `app/providers/google_places.py`
(`_attested`, `_accessibility`); `app/services/entity_service.py`
(`keep_attested` — an `unknown` from a cheap search never overwrites a
`confirmed_true` from a details fetch); `app/services/conflict_service.py`
(`_dietary` branches on all three states).

**Consequences.**
- `unknown` → a **blocking `OpenQuestion`**, and the option is *never* filtered.
- `confirmed_false` → a confirmed violation: worded as such, resolved by
  swapping rather than checking, and filtered out of *new* shortlists
  (`_discover_restaurants`). Scheduled items cannot be filtered, so they surface
  as a validator error instead.

**Pinned by.** `tests/unit/test_invariants.py`:
`test_a_provider_positive_is_the_only_confirmation`,
`test_old_bool_json_still_validates`,
`test_unknown_never_overwrites_a_confirmed_value`,
`test_a_later_search_keeps_an_earlier_attestation`.
`tests/scenarios/test_milestone7_acceptance.py`:
`test_a_confirmed_denial_is_a_different_conflict_from_an_unknown`,
`test_unknown_and_confirmed_denials_are_reported_separately`.
`tests/live/test_group_live.py`: `test_silence_is_not_a_denial`.

---

## 2. A score is not a confidence

> `DecisionScore.total` says what the evidence said. `DecisionScore.coverage`
> says how much evidence there was. They are never folded into one another in
> stored state. The discount for thin evidence is applied exactly once, at
> **ordering** time, and never persisted.

**Why.** A hotel with three reviews had no usable guest rating, no star category
and no measured travel time — so price was its only dimension, and being
cheapest made it a flawless `1.00` that outranked a hotel we knew five things
about. That is not a better hotel; it is a hotel nobody has looked at. But
damping the stored score instead would understate what *was* measured, so one
number would still be doing two jobs.

**Enforced in.** `app/services/scoring.py`: `combine` (undamped total +
coverage), `ranking_value` (the sole discount), `damp_for_coverage`. Every
ranker sorts on `ranking_value` / `group_ranking_value`, never on `total`:
`flight_ranking.rank_flights`, `hotel_ranking.rank_hotels`,
`ranking_service.rank_places`, `hotel_area_service.recommend_areas`,
`group_scoring.score_for_group`, and both group sorts in `agent/tool_registry.py`.

**Corollary — a factual floor cannot be met by sparse data.** `floor_check`
returns an `Attestation`: a 5.0 from three reviews is `unknown`, which neither
passes nor fails. It was previously refused as a score *and* accepted as proof
of a stated 4.5 floor — untrustworthy and authoritative at once. Only
`confirmed_false` is filtered (`hotel_ranking.meets_min_rating`); `unknown` is
kept and raised by the conflict layer.

**Pinned by.** `tests/unit/test_invariants.py`:
`test_a_stored_score_is_never_damped`, `test_ordering_discounts_thin_evidence`,
`test_full_coverage_orders_on_the_score_itself`,
`test_a_floor_is_answered_in_the_tri_state`, `test_places_now_report_coverage`.
`tests/scenarios/test_milestone7_acceptance.py`:
`test_an_option_cannot_win_on_ignorance`,
`test_score_and_confidence_are_never_folded_together`.

---

## 3. A write is all-or-nothing, and success is only reported after a reload

> A turn's patches persist together or not at all. A success payload is built
> from the row re-read after commit — never from the in-memory candidate.

**Why.** A turn can need more than one patch: refreshing a place's facts before
a day-scoped replan, because a scoped patch may not rewrite existing entities.
Committing them one at a time meant the refresh could land and the replan be
rejected, leaving a trip nobody asked for. And a revision reported without being
read back is a claim, not a fact.

**Enforced in.** `app/db/repository.py::TripRepository.apply_patches` —
1. load once; 2. chain the pure `patch_service.apply_patch` in memory (any
rejection aborts before a row is written); 3. **one** conditional
`UPDATE … WHERE revision = :base` covering the whole batch; 4. per-patch audit
events; 5. one commit; 6. `expire_all()` + re-`get`, and the final result's
`state`/`revision` come from that re-read. `apply_patch` is the n=1 case.
`app/agent/runner.py::_apply_all` builds the chained patches and makes one call.

**Boundary.** The guarantee is *our commit persisted*. A concurrent writer can
bump the revision between commit and reload; the reported revision is then the
store's at re-read, which is still a fact about the store rather than a hope.
Single-writer tests assert equality.

**Pinned by.** `tests/unit/test_invariants.py`: `test_a_batch_is_all_or_nothing`,
`test_a_whole_batch_persists_together`,
`test_success_is_reported_from_the_reloaded_row`,
`test_a_stale_base_revision_lands_nothing`,
`test_a_rejected_batch_leaves_the_turns_staged_work_intact`,
`test_a_successful_batch_reports_the_persisted_revision`,
`test_the_audit_trail_records_every_patch_in_a_batch`.

---

## 4. A group score never hides a split, and the formula is configuration

> `GroupScore` keeps every traveller's score. `describe()` is the only rendering
> and names the worst-served whenever the group is split. The fairness weight is
> a setting, recorded on every score.

**Why.** Three travellers at 0.9 and one at 0.1 average to the same 0.5 as four
at 0.5. One is a fight, the other is consensus, and the mean cannot tell them
apart. How much the unhappiest person counts is a judgement about the group, not
a fact about the options — so it is `Settings.group_worst_weight` (default 0.4,
`total = (1-w)·mean + w·worst`), and it is stored alongside the number it
produced, because `0.30` means nothing without it.

**Enforced in.** `app/models/group.py` (`GroupScore`, `worst_weight`,
`describe`); `app/services/group_scoring.py` (`build_group_score`,
`group_ranking_value`, `worst_weight` threaded through all three
`rank_*_for_group`); `app/config.py`; `agent/tool_registry.py::_group_view`
returns the verdict sentence, the coverage and the weight alongside the scalar.

**Pinned by.** `tests/unit/test_invariants.py`:
`test_the_worst_weight_moves_the_total_and_is_recorded`,
`test_the_setting_is_bounded`.
`tests/scenarios/test_milestone7_acceptance.py`:
`test_a_split_and_a_consensus_with_the_same_mean_are_told_apart`,
`test_a_group_score_never_renders_a_split_as_a_bare_number`,
`test_the_configured_weight_reaches_the_group_ranker`.

---

## 5. Conflict detection is independent of the ranking scalar

> What the group is *told they disagree about* is derived from preferences and
> entities alone. No scoring formula, weight or threshold can change it.

**Why.** If the fairness weight could move which conflicts get reported, tuning
the ranker would quietly change which arguments the group hears about — the
averaging failure wearing a different hat. `conflict_service` imports neither
`scoring` nor `group_scoring`, and conflicts are recomputed from stored state on
every read (like `signals_from_evidence`) so they cannot drift.

**Corollary.** A derived record needs a *derived* identity:
`PreferenceConflict.conflict_id` is a content hash of `(kind, travellers,
affects)`. A random id per derivation would mean an answered question could
never be matched to the next derivation of the same disagreement, and the group
would be asked forever.

**Enforced in.** `app/services/conflict_service.py`; `app/models/group.py`
(`PreferenceConflict._identify`). The independence is *structural* and checked
by walking the import graph — no ranking module is reachable from
`conflict_service`, transitively, across the 15 modules it can see.

**Pinned by.** `tests/unit/test_invariants.py`:
`test_conflict_detection_cannot_reach_any_ranking_code`.
`tests/scenarios/test_milestone7_acceptance.py`:
`test_conflicts_do_not_depend_on_the_ranking_formula`,
`test_changing_a_preference_changes_the_conflicts_with_no_patch_between`,
`test_unresolved_blocking_ignores_conflicts_the_group_has_settled`.

---

## 6. Learning proposes; only the traveller applies

> A learning hypothesis is derived from stored signals on every read, with a
> content-hash identity, and is never stored. The only write path from
> learning into a `TravelerProfile` is the accept endpoint — revision-guarded,
> writing the value and its provenance in the same update. A dismissal is a
> profile-scope `RejectionRecord` consulted at derivation, so "no" survives
> any amount of new evidence. Trip snapshots move only through the existing
> explicit refresh.

**Why.** A system that quietly rewrites who it thinks you are is the confident
wrongness of invariant 1 applied to a person instead of a pizzeria. One
dragged activity is scheduling, not personality; the profile's own docstring
("never from weak inference") predates this milestone, and until now nothing
enforced it. Strength (how intensely expressed) and confidence (how much
evidence) stay separate ordinal words for the same reason score and coverage
do in invariant 2 — and behavioural signals are recorded only where "who did
that?" has an answer: a solo trip, a named speaker, a signed reflection.

**A choice teaches only what it cost.** Picking a card is read as a signal
(ledger 53), but only where the winner gave something up: passing over cheaper
money for fewer stops, or over a closer hotel for a cheaper one. A winner that
is both the cheapest and the best gave up nothing, so there is no priority in
it to read, and reading one out anyway is how a profile fills with preferences
its owner never held. When they paid more *and* took the worse routing, they
bought something this does not measure, and silence is the honest record.
Neighbourhood and airport cards carry no price at all, so no choice between
them is a tradeoff — inventing an axis in order to have something to learn
would be worse than learning nothing.

**Enforced in.** `app/services/learning_service.py` (`derive_hypotheses` —
pure, statuses `dismissed > applied > proposable/emerging`;
`behavioral_signal_allowed` — the attribution gate; `profile_changes_for` —
deep-merges the sub-model so the repository's shallow merge cannot reset
siblings); `app/api/learning.py` (accept / dismiss / remove, all taking
`expected_revision`); `app/db/repository.py::ProfileRepository.update`
(`ProfileRevisionConflict`); `app/agent/tool_registry.py`
(`_record_stated_preference` refuses unnamed speakers,
`_review_learned_preferences` is read-only); the consumption seams in
`app/services/itinerary_service.py` (`effective_start`/`_shifted`,
`arrival_penalty`).

**Pinned by.** `tests/scenarios/test_milestone9_acceptance.py`: all nine
criteria, one test each —
`test_moving_one_early_activity_later_never_touches_the_profile`,
`test_the_same_late_start_pattern_across_trips_becomes_a_hypothesis`,
`test_the_agent_proposes_and_never_applies`,
`test_rejecting_the_proposal_leaves_the_profile_unchanged`,
`test_accepting_updates_the_long_term_profile_with_provenance`,
`test_the_current_trips_snapshot_survives_acceptance`,
`test_a_future_trip_starts_later_because_of_what_was_learned`,
`test_why_traces_a_learned_preference_to_persisted_evidence_only`,
`test_signals_are_never_assigned_to_the_wrong_traveler`.
`tests/unit/test_learning_service.py`:
`test_a_dismissed_hypothesis_never_becomes_proposable_however_much_evidence_arrives`,
`test_strength_is_the_strongest_expression_not_an_average`,
`test_every_catalogue_key_names_its_consumer`,
`test_every_learnable_key_is_in_the_catalogue_and_can_actually_be_written`,
`test_a_choice_that_gave_nothing_up_teaches_nothing`,
`test_paying_more_for_more_stops_teaches_nothing`,
`test_an_accepted_choice_preference_lands_on_the_ranker_s_own_weight`.
`tests/integration/test_decisions_api.py`:
`test_choosing_a_card_is_recorded_as_a_learning_signal`,
`test_a_choice_on_a_group_trip_is_attributed_to_nobody`,
`test_a_failed_signal_does_not_fail_the_choice`.
`tests/unit/test_slot_shift.py`:
`test_default_preferences_leave_every_template_exactly_as_authored`,
`test_dinner_never_slips_past_eight`.

---

## 7. A figure is only worth something if something could contradict it

> Every dimension this system judges itself on must name **who could check
> it**. Predictions are derived from `TripState` and never stored; only
> outcomes are, and they carry no trip id and nothing finer than a region. A
> calibration is derived on every read, reports which rung of the backoff
> chain answered it and how many checks stood behind that, and below
> `calibration_min_samples` it makes no claim at all. The raw figure is never
> rewritten, and calibration may add a warning but never clear one.

**Why.** The rest of this file is about being honest with what is known:
absence is not negation, a score is not a confidence, a norm may not speak
about a Tuesday. None of it ever asked whether the numbers were *right*.
Eighty-three travel-time estimates sat in the database, all from one provider
that already has a ledger entry against it for being regionally wrong, and not
one had been checked. A dimension with no checker is a claim that can never be
wrong - the same defect as invariant 6's preference that influences nothing,
one level up.

**Predictions are derived because the figures were already there.** A hotel
area's `mean_minutes` sits beside its `travel_mode`, an airport's
`ground_travel_minutes` beside its `ground_travel_source`. Deriving them means
no new write path, retroactive coverage of every trip planned before this
existed, no way for a prediction to drift from the state that produced it, and
no way for one to outlive its trip - because it was never anywhere else.
Outcomes must outlive their trips (the `learning_signals` reasoning), so they
denormalize what calibration needs and nothing more: a durable table keyed to
where somebody went is not a thing to create while measuring a routing API.

**The band is quantiles, not a spread around the median.** Live data proved
why: of thirty advertised hotel rates, fifteen matched exactly and three were
understated by 13%, 20% and 67%. Median absolute deviation collapsed to ±1.6%
on the strength of the fifteen and reported an advertised $200 as "more likely
$197-$203". A tight interval wrapped around a fat tail is a more confident lie
than no interval at all. The band is asymmetric wherever the errors are, and
claims only what it can - eight checks in ten, with the count beside it.

**It annotates; it does not reorder.** The plan called for a ranking
correction where a shortlist mixes travel modes. Checked against the live
database, none does: a shortlist is measured in a single route matrix and the
transit-to-driving fallback applies to the whole matrix, so every option on a
card shares a bias and correcting them would multiply them all by the same
number. There is a comment where that function would have been.

**No consent gate, and that is the point.** Invariant 6 needs the traveller's
word for every write because it changes what the system thinks of a *person*.
This is about providers. Nothing here touches a `TravelerProfile`, and
acceptance 9 holds that shut by checking the profile's revision does not move.

**Enforced in.** `app/models/calibration.py` (the closed `DIMENSIONS`
catalogue, each naming its checker; `Outcome` with no trip id);
`app/services/calibration_service.py` (`predictions_from` - derived, and
refusing to check a `historical_norm` against one day; `_bias_and_band`;
`calibration_for` - the backoff chain and the honest `uncalibrated`;
`Calibrations.note` - never silent); `app/db/models.py::OutcomeRow`;
`app/services/validation_service.py::_likely_minutes` (may warn, never clear);
`app/api/calibration.py` (read-only, not trip-scoped).

**Pinned by.** `tests/scenarios/test_milestone10_acceptance.py`: all ten
criteria, one test each. `tests/unit/test_calibration_service.py`, especially
`test_a_mostly_right_provider_still_reports_its_bad_days`,
`test_one_road_closure_does_not_become_a_finding`,
`test_a_historical_norm_is_never_checked_against_one_day`,
`test_an_outcome_carries_no_trip_and_no_place`, and
`test_every_dimension_names_who_checks_it`.

---

## 8. The engine computes; the model explains

> Every schedule figure a traveller sees — an arrival window, a verdict, a
> finding — was computed deterministically from stored measurements and
> stated assumptions. The language model runs `run_stress_test` and explains
> its output; it never adds minutes, estimates a journey, or adjusts a window
> itself. Every simulation input carries a provenance
> (`measured | calibrated | assumption | unknown`), an unknown never
> contributes zero, and the verdict vocabulary is four words with no score.

**Why.** A model doing clock arithmetic in prose is confidently wrong in the
exact register a traveller cannot check — ledger 33 was a model *correctly*
narrating a wrong stored number, and the lesson generalises: numbers must
come from code that can be tested, and the model's job is the part code
cannot do, which is saying what the numbers mean. The three-scenario
propagation is honest about its own inputs the way invariant 7 demands:
an earned band (`calibrated` only) may widen a window, a stated assumption
must show itself as one, and a journey nobody measured resets the chain to
the schedule and says so rather than inventing a zero — a day resting on
unknowns can be called neither safe nor dangerous, so it caps at `workable`
from both directions.

**One validator.** The simulation never re-judges what `validate_itinerary`
already judges: errors decide `blocking`, warnings fold into findings under
simulation names with the validator's own messages as evidence, and no
threshold exists in two places. Calibration keeps its M10 rule — it may add
a warning, never clear a validator error.

**Enforced in.** `app/services/simulation_service.py` (pure; invokes the
validator itself so a forecast cannot forget to consult it; `_FOLDS`;
`ASSUMPTIONS` with `overridable_by` naming only fields that exist);
`app/models/simulation.py` (closed vocabularies); the `run_stress_test`
tool and its returned `note`; the prompt section "Whether a day will
actually work is a computation, not an opinion";
`POST /trips/{id}/stress-test` (read-only, no revision, no rows).

**Pinned by.** `tests/scenarios/test_milestone11_acceptance.py` (all twelve
criteria, one test each — unmeasured-never-zero, expected-safe /
conservative-fragile, parking uncertainty, weather fragility, locked items
byte-for-byte through "Make this day safer", unrelated days byte-identical,
provenance end to end, the prompt fence, zero writes, four-word verdicts,
single-day preview). `tests/unit/test_simulation_service.py` (the interval
arithmetic, the cascade, folds carrying the validator's words, provisional
moving nothing).

---

## Ledger — defects found by running it

Reasoning about the code did not find these. Running it did. Each has a test so
it cannot come back.

| # | defect | found by | pinned by |
|---|---|---|---|
| 1 | Price normalised across the candidate range, so a \$10 gap scored like a \$500 one; then a floor made \$916 and \$1072 tie and the dearer win | M5 live fares | `scoring.price_score` docstring; `test_milestone5_acceptance` |
| 2 | Google has **no transit routing data for Japan** — both Routes endpoints answer in the US/UK and return nothing in Tokyo | M6 live | `hotel_area_service.measure_travel`; `test_transit_falls_back_to_driving_where_google_has_no_transit_data` |
| 3 | `compute_route` wrapped waypoints the way only the *matrix* endpoint accepts — every call would have 400'd | M6 live | `google_routes.compute_route` comment |
| 4 | A hotel's advertised "from \$70" was matched by no listed booking site (all wanted \$90) | M6 live | `serpapi_hotels._headline_note`; `test_an_advertised_rate_no_site_matches_is_said_out_loud` |
| 5 | Per-vendor prices barely exist in a search — 2 of 20 properties; they live behind a per-property detail call | M6 live | `HotelProvider.fetch_quotes`; `test_vendor_quotes_are_fetched_for_the_shortlist_only` |
| 6 | A one-dollar gap between near-identical hotels was announced as a winner | M6 live | `explain_hotel_choice(close_call)`; `test_two_near_identical_hotels_are_reported_as_a_close_call` |
| 7 | The agent loop had **zero headroom** — a successful 5-day plan spent 13 tool calls against a 12-round cap — and running out looked identical to finishing | M6/M7 live | `AgentRun.hit_iteration_limit`; `test_the_loop_stops_at_the_iteration_cap` |
| 8 | An option could win on ignorance: 1.00 from one dimension beat a well-evidenced 0.8 | M7 live | invariant 2 |
| 9 | A rating too thin to score on still cleared a stated 4.5 floor | M7 live | invariant 2 (`floor_check`) |
| 10 | A stated preference influenced nothing: `min_rating` was a filter for one traveller and absent from the group path | M7 live | `hotel_ranking._minimum_score`; `test_a_thin_rating_cannot_clear_a_stated_floor` |
| 11 | The taste→weights mapping scaled every present dimension by one factor — under renormalisation, exactly a no-op | M7 live | `group_scoring._weights_for`; `test_places_are_scored_per_traveler_too` |
| 12 | `apply_trip_patch` built entity operations only on the branch that had a proposal, so committing without one discarded thirty discovered places and reported "no such proposal" — the agent then described a plan it had not saved | M7 live | `test_places_gathered_this_turn_commit_without_an_itinerary_proposal` |
| 13 | Fixing 12 naively made a *named but missing* proposal_id report success for a partial write | M7 live | `test_a_missing_proposal_id_is_an_error_even_with_places_staged` |
| 14 | Nothing told the model a proposal changes nothing until applied — it generated twice and stopped | M7 live | `test_the_prompt_tells_the_model_to_apply_its_proposal` |
| 15 | Reviewing *group* preferences on a **one-person** trip spent a planning round and derailed the plan | M7 live | `test_a_solo_trip_gets_none_of_this_machinery` |
| 16 | Japanese place names + a cp1252 pipe killed a script on its own output | M7 live | `test_the_scripts_survive_being_piped` |
| 17 | Multi-patch turns committed per patch, so one could land and the next be rejected | hardening pass | invariant 3 |
| 18 | A replan that arrived at the day it started from still committed, spending a revision on an empty diff — the number moved and nothing explained it | frontend P0 live | `test_a_replan_that_changes_nothing_does_not_spend_a_revision` |
| 19 | The UI's `post` helper was declared `(path, body)` while all eight call sites pass the body first, so every POST carried `{}`. Move failed outright; replan silently ignored the requested pace, planned a balanced day, and reported success | frontend P0 live | `test_the_post_helper_takes_the_body_first` |
| 20 | `PartySpec.adults` defaulted to `1`, so a trip whose party size nobody had given was indistinguishable from a solo trip - and `search_flights` compounded it with `party.adults or 1`, pricing a real fare for an invented passenger | intake | `test_a_flight_search_refuses_an_invented_passenger_count` |
| 21 | The agent was never told the current date, so it could not resolve "8/10-8/14" to a year and asked the traveller to repeat dates they had just given | intake live | acceptance 2 in `test_intake_acceptance.py` |
| 22 | The intake tools staged their work for `apply_trip_patch` like every other tool. Twice the agent recorded the answers, composed the right questions, replied "已经记录", and never applied - leaving an empty workspace beside a chat listing three questions. Strengthening the prompt did not fix it; the tools now commit through the same path | intake live | `test_intake_writes_itself_rather_than_waiting_to_be_applied` |
| 23 | `ask_clarifications` would ask anything the model thought of: the party size on a trip where it was not blocking, and "which area is your hotel in?" on a trip with nothing left to ask. Questions must now name an outstanding `requirement_id` - an exact id, not a prefix test on a JSON pointer, which could not tell `/brief/party` from `/brief/party/adults` | intake live | `test_a_question_without_a_known_requirement_id_is_not_asked`, `test_nothing_is_asked_once_nothing_is_blocking` |
| 24 | "United States" was recorded as the destination of a Maui trip, because the brief tool offered city or country and Maui is an island. A country alone is now refused, and the year of a date like `8/10` is resolved from the runtime clock in the destination's timezone rather than asked about | intake live | `test_a_country_on_its_own_is_refused`, `test_a_year_less_date_resolves_against_the_current_date`, `test_today_follows_the_destination_timezone` |
| 25 | `_ensure_routes` fetched every leg as walking while `mode_between` looked up transit above 1.5 km, so no long leg was ever measured and the day's travel total was silently zero. The mode heuristic also assumed transit for a trip with a rental car, and Google publishes no transit for Maui | M8 Pass C | `test_the_fetcher_asks_for_the_mode_the_validator_reads`, `test_a_trip_with_a_car_drives_rather_than_assuming_transit` |
| 29 | The intake gate blocked a trip that was already researching: recording one brief fact moved it from `collecting` to `awaiting_confirmation`, which defeated the grandfather clause. The agent blocked itself by writing something down | M8 final acceptance | `test_a_trip_that_predates_intake_still_researches` |
| 30 | The agent had no way to replace a stop. `substitute_item` existed, was tested, was parking- and weather-aware, and was reachable only from a button - so "replace it, the parking is bad" could only be served by `replan_day` dropping the stop and leaving a hole | M8 final acceptance | `test_a_replacement_records_why_it_was_chosen` |
| 31 | Weather and parking were absent from the state projection, so the agent could act on "this place has bad parking" only by taking the traveller's word for a fact the trip already held | M8 final acceptance | `test_the_model_can_see_parking_and_weather_at_all` |
| 33 | A question whose gap had closed by another route still counted as outstanding, so a traveller who answered "找酒店" in the chat could never confirm their brief: `ready_to_confirm` was false forever, the workspace rendered the questions *instead of* the summary, and the confirm button was never in the page at all — while the agent went on asking for a confirmation it had made impossible. `POST /confirm` had always checked only the gaps, so the browser was enforcing a stricter rule than the server and hiding a button the server would have honoured | UI complaint, live | `test_a_question_answered_in_the_chat_stops_holding_the_brief_shut`, `test_ready_to_confirm_predicts_what_confirm_actually_does`, `test_the_model_is_not_shown_a_question_it_no_longer_needs_to_ask` |
| 32 | Candidates for a swap never had parking measured - the parking pass covers scheduled stops only - so the ranking could avoid a known-bad option but never choose a known-good one, and a swap traded a measured 16-minute walk for an unmeasured one | M8 final acceptance | `test_easier_parking_beats_a_better_rating` |
| 28 | Missing parking data must never become "no parking". A place Google publishes no parking field for is a place nobody has told us about, and marking it `unavailable` would delete real beaches from real trips on no evidence - the same failure as reading `servesVegetarianFood: false` as a denial | M8 Pass E | `test_nothing_found_leaves_it_unknown_never_unavailable`, `test_a_failed_lookup_is_also_unknown_and_says_which_it_was` |
| 27 | Google Weather caps a response at five days whatever `days` asks for, and starts from the *location's* yesterday - so a five-day trip inside the horizon silently lost its last two days | M8 Pass D live | `tests/unit/test_weather.py` fixtures mirror the leading-day behaviour |
| 26 | The map joined two pins with a line and let it be read as a driving route. Geometry now comes from Routes, and where it cannot, the line is labelled straight - with "Google knows of no route" and "the lookup failed" reported apart | M8 Pass C | `test_a_day_route_never_claims_a_road_it_did_not_fetch` |
| 34 | **The same defect as 12, 14 and 22, a fourth time.** A live turn ran twelve tools - two airport searches, flights, three place searches, three restaurant discoveries, neighbourhoods - then wrote a reply telling the traveller to choose from "the cards just below", and never called `apply_trip_patch`. `run_log` showed `patches: None`; the audit trail held thirteen revisions of brief edits and not one decision event; every option died in a buffer. There were no cards, and the prompt's own `## When you have staged options` had promised there would be, without naming the tool that made it true. Twice before the answer was a stronger prompt; twice it came back. The runner now flushes staged findings at the end of every turn | user report, live | `test_a_turn_that_never_applies_still_saves_what_it_found`, `test_stopping_a_turn_saves_nothing`, `test_a_failed_flush_is_reported_not_swallowed` |
| 35 | `_apply_all` cleared all ten `pending_*` buffers on success, not just the ones its plan consumed - so any tool committing mid-turn would silently discard what an earlier tool had staged and never written. Unreached only because the intake tools, the sole users of `_commit_now`, happen to run first. A commit now drains the whole context, which is what makes the blanket clear correct rather than lucky | found while fixing 34 | `test_a_commit_mid_turn_does_not_discard_earlier_staged_places` |
| 36 | **Choosing a card had no consequence.** The traveller picked their departure airport, arrival airport and neighbourhood; three revisions committed correctly and the audit trail stopped dead. The card vanished, the attention bar went quiet, and planning ended - beside a reply promising "once you have chosen I will find flights and hotels". `app/api/decisions.py` never imported anything from `app.agent`, `ActionResult` had no field that could say what a choice unblocked, and the only code path in the browser that started a turn was the Send button. The last choice now starts the next turn, naming what was chosen | user report, live | `test_the_overview_says_what_a_choice_has_unblocked`, `test_the_page_carries_on_after_the_last_choice` |
| 37 | Three places answered "what is the next planning step" and disagreed: `SINGLETON_DECISIONS`, the browser's `CHOICE_ORDER`, and fifteen lines inside the progress strip. None encoded a dependency, so nothing could say a hotel waits on its neighbourhood - the rule `hotel_service.resolve_area` had been enforcing all along. `next_step.next_steps` is now the one answer, read by the overview, the model's projection and the progress strip | found while fixing 36 | `tests/unit/test_next_step.py`, especially `test_hotels_wait_for_a_neighbourhood` |
| 38 | The choice cards rendered `figuresHtml(o.figures)`, and `OptionView` has no `figures` field - so every card ever shown carried a name and its pros and cons and **not one number**, while the full decisions panel showed them correctly. The card's own comment calls the figures "what make it possible to disagree with the ranking". It is also why the agent copied whole rankings into its prose | found while fixing 36 | `test_the_choice_cards_show_their_figures` |
| 39 | **A button labelled `Start planning` started no planning.** It wrote four intake fields and returned; `app/api/intake.py` imports nothing from `app.agent`, and the only path to a turn in the browser was the Send button. Measured across the whole store: **8 of 8 confirmed trips** needed the traveller to type something after pressing it, and not one assistant turn was ever unprompted. The click now sends the first message itself - unconditionally, because pressing a button that says Start planning *is* the instruction | user report, live, 8/8 | `test_confirming_leaves_the_trip_with_somewhere_to_go`, `test_the_page_starts_planning_when_the_brief_is_confirmed` |
| 47 | **A return trip's two directions were summed and the total called one-way.** `duration_minutes = sum(s.duration_minutes for s in slices)`, so a nonstop Albany→Chicago return reported 4h59m — 2h43m out plus 2h16m back — printed beside the word "nonstop" and an outbound-only route. `arrival_at` was the *return* landing, so three offers departing at different times all showed one arrival time. Not a timezone bug: Duffel's ISO durations already carry the offset, and nothing subtracts timestamps. Every scalar now describes the outbound, and the renderers read `slices`, so offers stored before the fix come out right | user question, live | `test_a_return_trip_reports_the_outbound_not_the_sum`, `test_the_arrival_is_where_the_outbound_lands` |
| 48 | Because `arrival_at` was the return landing, `_arrival_score` and `_red_eye_score` **scored the wrong flight**: an offer reaching Chicago at 13:59 was docked for arriving at 23:08, three days later and in the other direction | found while fixing 47 | `test_a_late_return_does_not_penalise_a_daytime_outbound` |
| 49 | **Every airport fixture in the suite was single-slice while 100% of stored offers were two-slice** — `sum()` and `max()` over one element are the identity, so the tests exercised a shape production never produces and could not have caught 47 | found while fixing 47 | the `return_offer()` builder in `test_duffel_provider.py` |
| 50 | **Two milestones were unreachable.** Every M9 surface and all of M7's group machinery is keyed to a traveller with a `profile_id`, and **nothing in the application ever created either** — the new-trip form collected a head count and never a person, no UI posted to `/profiles`, and no agent tool could make one. Live: 13 trips, 0 travellers, 0 profiles, 0 learning signals. `_record_stated_preference` even told the model to "offer to create one", a capability that did not exist | user question, live, 13/13 | `tests/integration/test_trip_creation.py`, especially `test_that_traveller_can_carry_learning` |
| 51 | And the test that looked like coverage was a substring grep on the served HTML — `assert "Travel DNA" in body` passes whether or not any code path can render it. **It was green for the entire period the feature was unreachable**, which is why nobody noticed. A reachability test has to travel the route a user travels | found while fixing 50 | `test_a_trip_created_the_way_the_page_creates_one_has_a_traveller` |
| 52 | A solo trip's preference snapshot was **never resolved**: `review_group_preferences` returns immediately below two travellers, and nothing else filled `traveler.preferences`. So even once a profile existed, a learned `preferred_start_time` would have reached a future trip's day templates never — M9's acceptance 7 would have been empty in practice. The snapshot is resolved at creation | found while fixing 50 | `test_the_snapshot_is_resolved_at_creation` |
| 53 | **Choosing a card taught nothing.** A complete session — five agent turns, four cards chosen, an itinerary applied — left `learning_signals` empty and the Travel DNA panel on its "Nothing yet" state. Every implemented signal source needed something the traveller had no reason to do (drag a pre-10:00 activity an hour later, ask for an easier day, wait for the trip to end), while the richest and most frequent action in the app produced nothing. Yet a card records both sides of a tradeoff: `signals_for_choice` reads what the choice *cost* — passing over cheaper money for fewer stops, or over a closer hotel for a cheaper one — and maps it onto the four importance weights the rankers already multiply by. A winner that is cheapest *and* best gives up nothing and is recorded as nothing | user report ("travel DNA好像没东西？"), live | `test_choosing_a_card_is_recorded_as_a_learning_signal`, `test_a_choice_that_gave_nothing_up_teaches_nothing` |
| 54 | `_note_signals` guards the *write* against exceptions but its callers built the signals **outside that guard**, so any error while deriving one took the traveller's click down with it — the opposite of the "a signal is never a reason the click fails" contract in its own docstring. It now takes a callable and evaluates the derivation inside the try | found by the test written for 53 | `test_a_failed_signal_does_not_fail_the_choice` |
| 55 | Adding a catalogue key was **two silent failures wide**: a `PreferenceKey` with no catalogue entry is dropped by `derive_hypotheses` without a word, and an entry whose section is missing from `_SECTION_FOR_PATH` derives, proposes and shows a card that raises the moment the traveller presses Add. Both were one line away while 53 was being written | found while fixing 53 | `test_every_learnable_key_is_in_the_catalogue_and_can_actually_be_written` |
| 56 | `record_stated_preference` was called **zero times in five agent turns** of a real session, while a correct, well-argued section about it sat two thirds of the way down the prompt. A rule the model must remember to consult is a rule it will not; the instruction moved into a `## Every turn, before you reply` section at the top and says why it alone cannot be deferred — a click gets recorded by the app either way, a sentence is gone. The test pins the placement, which is the part that was wrong; it cannot pin compliance | found while fixing 53 | `test_recording_what_was_said_is_a_step_in_the_turn_not_an_optional_rule` |
| 57 | **`brief.timezone` had never been written by anything.** Its own docstring says it comes from the Places `timeZone` field; `today_at` reads it to answer "what is today where they are going"; the reflection gate, the model's daily context, the flight date logic and the trip-is-over check all go through it. Fourteen trips, zero with it set — so every trip that has ever existed worked out its dates in **UTC**, while all 332 of their entities sat in the database each carrying the correct IANA zone. Taken from the places now, once, by majority, never re-voted | found while looking for a calibration scope key | `test_the_trip_takes_its_timezone_from_the_places_it_finds`, `test_a_trip_that_already_knows_its_zone_is_not_re_voted` |
| 58 | The calibration band was **median absolute deviation** and the first thirty real checks showed why that is unsafe: fifteen advertised hotel rates matched exactly, so the deviation collapsed to ±1.6% and an advertised \$200 was reported as "more likely \$197–\$203" — while three of the same thirty were understated by 13%, 20% and **67%**. A tight interval wrapped around a fat tail is a more confident lie than no interval. Now quantiles of the observed errors, asymmetric, claiming eight checks in ten and carrying the count | live data, on the first run of the new code | `test_a_mostly_right_provider_still_reports_its_bad_days` |
| 59 | The "My accuracy" chip toggled a flag that **no render list read**, so the button opened nothing — every string the grep test looked for was already present and green. The artifact loop iterates a hardcoded list of kinds and the new one was not in it. This is ledger 41/50's shape a third time: a surface that exists everywhere except where it is drawn | opening the panel by hand | `test_the_page_can_show_how_close_our_own_numbers_run` (now asserts the kind is in the render list, not just that the words exist) |
| 60 | The accuracy panel printed **developer prose at the traveller**: "\`HotelOptionData.headline_gap()\` - the advertised rate…" and a sentence naming `app/models/weather.py`. `DimensionEntry.checker` was written as a note to whoever reads the catalogue and then rendered verbatim on a screen. Rewritten as sentences a person planning a holiday can read, with the identifiers moved into comments beside them | opening the panel by hand | the field's docstring now says it reaches a screen |
| 44 | **The agent worked out the way forward and could not offer it.** No airline flew ALB → MDW on those dates; the reply said, correctly, that the practical next step was to reconsider the arrival airport with ORD already shortlisted — and then stopped, with nothing on screen to do it with. Every surface refused the question: `ask_clarifications` only asks about an outstanding intake requirement, `OpenQuestion` has no choices and **no route anywhere can mark one answered**, the chooser hides a decision that is already settled (which the airport was), and nothing can re-open one. `AgentProposal` + `propose_next_step` turn a fork into buttons whose actions are a closed set | user report, live | `tests/unit/test_proposals.py`, especially `test_a_proposal_names_choices_that_exist` |
| 45 | A search that found nothing **staged nothing at all**, so "no airline flies this route on these dates" and "nobody has looked yet" were the same stored state — absence read as never-asked, one more wearing of invariant 1. And `next_steps` tested whether the decision *existed*, so the step vanished the moment a search came back empty, which is when it is most needed | found while fixing 44 | `test_a_flight_search_that_finds_nothing_records_that_it_looked`, `test_a_search_that_found_nothing_is_still_work` |
| 46 | "Park this for now" had no honest home: `not_needed` claims they are not flying, `already_arranged` claims tickets exist, and `unknown` re-opens a blocking intake gap and un-confirms the brief. `Decision.set_aside_reason` is a fourth kind of claim beside `status`, `booked` and the scope states — the outcome of a search rather than an instruction or a fact about the world — and `should_shop_for` reads all three | found while fixing 44 | `test_setting_a_part_aside_stops_it_being_a_next_step` (asserts `scope.flights` is still `plan`) |
| 41 | **Two airports of one city rendered as the same card.** ORD and MDW both read "Chicago", both "✓ 22 min drive", both "✓ serves Chicago" - while the stored options differed in `iata`, `name`, and `distance_km` (24.9 vs 14.7). Four renderers each dropped the distinguishing part: `label_for` returned at `city`, two attributes before its `iata` branch; `metrics_for` had no airport branch at all, so no figure ever reached a card; `_airport_pros` rounded 21.5 and 21.8 to the same "22"; and `serves {city}` is identical by construction. `label_for` also feeds `_selected_label`, so **the model could not tell them apart either** | user report, live | `tests/unit/test_decision_labels.py`, especially `test_two_airports_in_one_city_are_told_apart` |
| 42 | Every airport fixture in the suite set `city=iata`, which made 41 **structurally impossible to reproduce in a test** - two airports of one city could not exist. A fixture that cannot express the failure is not coverage | found while fixing 41 | the `chicago()` builder in `test_decision_labels.py` uses the real municipality |
| 43 | `brief.notes` was writable from the first milestone and read by nobody: the tool declared it (with an empty description), `_brief_ops` mapped it, and `summarize()` never carried it while no screen rendered it. Anything a traveller asked to be planned around went into the store and stopped there. It is now the record their words are kept in, projected to the model and shown on the brief card - and `record_constraints` exists so a reading of those words can be stored beside them, which nothing in the repo could do before | found while building the "anything else?" step | `test_other_requirements_reach_the_model`, `test_a_stated_requirement_becomes_a_constraint_without_losing_its_words` |
| 61 | **The two spending caps did not cap spending.** `agent_max_iterations` bounds *rounds*, but a round emits any number of tool calls — a real turn ran 22 calls in 14 rounds — and `planning_search_budget` was consulted by 3 of the 15 tools that reach a paid provider, none of them the expensive ones: flights, hotels, airports, hotel areas and every route matrix counted nothing. Meanwhile the model call carried no `max_output_tokens`, inherited the SDK's 600s timeout and its two silent retries, and `run.input_tokens` accumulated all turn without a single line reading it. The worst real turn measured 280,311 input tokens *while working correctly*. Counting now happens in `request_json`, the one function all six paid HTTP providers pass through, so a tool cannot escape it by forgetting to ask — the alternative, a check in each of twelve handlers, is ledger 41/50/59's shape waiting to happen a fourth time | asked whether anything stopped a runaway bill | `tests/unit/test_turn_budget.py`, especially `test_an_exhausted_budget_stops_the_call_before_the_network`; `test_a_turn_stops_when_it_has_spent_its_tokens` |
| 63 | Metering the agent's turns left **five HTTP endpoints spending unwatched** — every button that measures routes builds its own `Toolbox`, and none of them is a turn. Not a runaway risk (a click is bounded by the size of the trip, measured at 2 route calls for a whole four-day stress test) but it made "paid calls are counted" false with an asterisk, and the sixth endpoint written would not have remembered either. A `Toolbox` now opens a budget if nothing outside already has, so the agent's turn-wide count still wins and a click is still counted | found while measuring what a turn costs | `test_a_toolbox_opened_outside_a_turn_still_meters`, `test_a_toolbox_inside_a_turn_does_not_start_a_second_count` |
| 62 | Capping the model's output introduced the very defect this project catalogues: a reply stopped at `max_output_tokens` came back as a fragment that read exactly like a finished answer. Ledger 7's lesson, one layer down — `LLMTurn.truncated` now carries it and the turn says the answer was cut off | found while writing the cap | `test_a_reply_stopped_by_the_output_cap_is_marked`, `test_an_answer_cut_off_by_the_output_cap_says_so` |
| 40 | `next_steps` reported **nothing to do** for a trip whose flights and hotel were already booked: there was nothing to shop for, and building an itinerary needs places no search had yet found. So the traveller with the least left to arrange got the emptiest answer, and anything gated on "is there work?" would have refused them - the third time in a row that the same person got a screen that did nothing. Finding places is now a step in its own right, which is also what the model reads each turn | found while fixing 39 | `test_a_trip_whose_flights_and_hotel_are_booked_still_has_work` |

---

## Running the checks

```bash
python -m pytest -q                                  # offline
python -m pytest -m live --override-ini addopts=     # live, opt-in
```

The live suite is what finds the entries in the ledger above. It costs API quota
and is worth it.
