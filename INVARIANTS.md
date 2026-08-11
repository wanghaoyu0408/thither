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
`test_every_catalogue_key_names_its_consumer`.
`tests/unit/test_slot_shift.py`:
`test_default_preferences_leave_every_template_exactly_as_authored`,
`test_dinner_never_slips_past_eight`.

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
| 40 | `next_steps` reported **nothing to do** for a trip whose flights and hotel were already booked: there was nothing to shop for, and building an itinerary needs places no search had yet found. So the traveller with the least left to arrange got the emptiest answer, and anything gated on "is there work?" would have refused them - the third time in a row that the same person got a screen that did nothing. Finding places is now a step in its own right, which is also what the model reads each turn | found while fixing 39 | `test_a_trip_whose_flights_and_hotel_are_booked_still_has_work` |

---

## Running the checks

```bash
.venv/Scripts/python.exe -m pytest -q                                  # offline
.venv/Scripts/python.exe -m pytest -m live --override-ini addopts=     # live, opt-in
```

The live suite is what finds the entries in the ledger above. It costs API quota
and is worth it.
