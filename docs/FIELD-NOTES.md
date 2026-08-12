# Field notes — what only showed up by running it

Reasoning about the code did not find any of these. Running it against real
providers, real fares and a real traveller did.

Each is the long version of a row in [INVARIANTS.md's ledger](../INVARIANTS.md#ledger--defects-found-by-running-it),
which is the terse index: one line per defect, how it was found, and the test
that pins it. These are the stories.

Newest first.

---

## A turn's findings no longer depend on the model remembering

The same defect returned a fourth time, reported by a traveller who saw a reply pointing
at flight and neighbourhood cards that were not there. The turn had run twelve tools
successfully; `run_log` said `patches: None`. Thirteen revisions of brief edits, not one
decision event, every option dead in a buffer.

The prompt had promised those cards — *"every decision still open appears to the
traveller as a card directly below your reply"* — without ever naming the tool that made
it true. The model did exactly what it was told.

**The runner now flushes staged findings at the end of every turn**, through the same
`apply_patches`, the same gates, the same atomicity and revision bump. Committing a
shortlist decides nothing: it puts the options in front of the traveller, and the choice
stays theirs. Stopping a turn still writes nothing — stop means stop. A refused flush is
recorded rather than swallowed, because silence is the failure this ends.

Fixing it surfaced a second, unreached bug: `_apply_all` cleared every staging buffer on
success, not just the ones its plan consumed, so any tool committing mid-turn would have
discarded an earlier tool's places. A commit now drains the whole context.

## A return trip is two flights

A card read "nonstop, 4h59m total travel time" for Albany → Chicago. The real
nonstop is 2h43m. `duration_minutes` was summing every slice of the offer, so
the number was the outbound *plus the flight home* — printed beside the word
"nonstop" and an outbound-only route, which reads as one five-hour flight.
Long-haul made it starker: SFO → Osaka came out at 36–41 hours and the agent
faithfully called it 单程.

Not a timezone bug, and worth saying why: Duffel's ISO durations already carry
the offset. Subtracting the local clock times would have given 1h43m — an hour
*short*, not three hours long.

`arrival_at` was worse. It was the **return** landing, so three offers leaving
at different times all showed the same arrival — and `_arrival_score` and the
red-eye check scored the outbound on it. An offer reaching Chicago at 13:59 was
docked for coming in at 23:08, three days later and in the other direction.

Every scalar on a flight option now describes the outbound, and the card shows
both directions:

```
Outbound   2h 43m nonstop   (30 Oct 12:16 → 13:59)
Return     2h 16m nonstop   (02 Nov 19:52 → 23:08)
```

The renderers read `slices`, so offers already stored come out right without
re-searching. No test could have caught this: every flight fixture in the suite
was single-slice while **100% of stored offers are two-slice**, and `sum()` over
one element is the identity.

## Nobody was ever in the trip

Milestone 9 built a learning layer — behavioural signals, preference
hypotheses, a consent card, a Travel DNA panel, a post-trip reflection — and
milestone 7 built per-traveller preferences, group scoring and conflict
detection. **Both were unreachable.** Every surface is keyed to a traveller with
a `profile_id`, and nothing in the application ever created either: the
new-trip form collected a head count and never a person, no screen posted to
`/profiles`, and no agent tool could make one.

The live database said it plainly: 13 trips, **0 travellers, 0 profiles, 0
learning signals**. `_record_stated_preference` was even telling the model to
"offer to create one" — a capability that did not exist.

The form now asks who the trip is for, reusing a profile across trips because
nothing is learned from one trip alone. The snapshot is resolved at creation
too: `review_group_preferences` returns immediately below two travellers, so a
solo trip's preferences were never resolved at all, and a learned start time
would have reached a future trip never.

The test that looked like coverage was `assert "Travel DNA" in body` — a
substring grep on the served HTML, green for the entire period the panel could
not be opened. A reachability test has to travel the route a user travels, and
`tests/integration/test_trip_creation.py` now does.

## Reachable, and still empty

With a traveller in the trip at last, a whole session ran: five agent turns,
twelve tool calls, four cards chosen, an itinerary generated and applied. The
Travel DNA panel still read *"Nothing yet."* — and it was telling the truth.
`learning_signals` held zero rows.

Every implemented signal needed something the traveller had no reason to do.
Drag an activity that starts before 10:00 at least an hour later. Ask for a
day to be made easier, in those words. Wait for the trip to end and fill in a
reflection. Or hope the model remembered to call `record_stated_preference` —
which, across five turns of someone stating preferences in two languages, it
did not once.

Meanwhile the richest thing a traveller does here happened four times and was
read by nobody. **A card records both sides of a tradeoff.** The chosen option
and the ones beside it carry prices, stop counts and travel minutes, so what a
choice *cost* is sitting in the stored state: pass over cheaper money for
fewer stops and that is a preference about flying; take the cheaper room
further out and that is a preference about money. `signals_for_choice` reads
exactly that, onto the four importance weights `flight_ranking` and
`hotel_ranking` already multiply by — no new key that nothing consumes.

The discipline is in what it declines to read. A winner that is both cheapest
and best gave nothing up, so nothing is recorded. Pay more *and* take the
longer routing and you bought something this does not measure, so nothing is
recorded. Neighbourhood and airport cards have no price on them at all, so a
choice between them is not a tradeoff and never becomes one. Learning less is
the whole point: a profile that fills up with preferences its owner never held
is worse than an empty one, and an empty one at least says so.

The prompt fix is the weaker half and is written down as such. The rule about
recording what someone says was correct, well argued, and two thirds of the
way down the page; it is now a `## Every turn, before you reply` section at
the top, saying why this one thing cannot wait — a click gets recorded by the
app whether the model participates or not, and a sentence does not. The test
pins where the instruction sits, which is what was wrong with it. No test can
pin that a model complies.

## It never checked whether it was right

Everything above is about being careful with what is known. Absence is not
negation, a score is not a confidence, a seasonal norm may not speak about a
Tuesday. None of it ever asked the other question.

Eighty-three travel-time estimates were sitting in the database — every hotel
area, every hotel, every airport — all from one provider that already has a
ledger entry against it for being regionally wrong. Not one had ever been
checked against what happened.

**Predictions are derived from the trip, not stored.** The figures were always
there with their own provenance: a neighbourhood's mean travel time beside the
mode it was measured in, an airport's drive time beside whether anyone actually
looked it up. So there is no new write path, every trip planned before this
existed is covered retroactively, a prediction cannot drift from the state that
produced it, and it cannot outlive its trip because it was never anywhere else.
Only the outcome is stored, and it carries no trip id and nothing finer than a
region — a durable table keyed to where somebody went is not a thing to create
by accident while measuring a routing API.

**Every dimension names who could contradict it.** A figure nobody can check is
a claim that can never be wrong, which is milestone 9's preference-that-changes-
nothing wearing a different hat. Travel time needs a person who was there.
An advertised hotel rate checks itself, because the rate and the cheapest price
a named booking site will actually honour arrive in the same fetch. A forecast
is checked against the weather archive — a *forecast*, never a seasonal norm,
because a norm is a claim about a season and scoring it against one Tuesday is
the exact category error the weather model exists to prevent.

Thirty checks were available immediately, from data already stored. They also
caught the arithmetic being wrong on its first run: fifteen of the thirty
advertised rates matched exactly, so median absolute deviation collapsed to
±1.6% and reported an advertised \$200 as "more likely \$197–\$203" — while
three of the same thirty were understated by 13%, 20% and 67%. The band is
quantiles of the observed errors now: \$176–\$217, asymmetric because reality
is, claiming eight checks in ten and carrying the sample count beside it.

**Below five checks it says nothing, and says that out loud.** A dimension
nobody has ever checked renders as "never checked against what actually
happened" rather than as blank space, because a screen that stays silent lets
never-once-checked and always-right look identical.

**It annotates and does not reorder.** The plan called for a ranking correction
where a shortlist mixes travel modes. Checked against the live database, none
does — a shortlist is measured in one route matrix, so every option on a card
shares a bias and correcting them would multiply them all by the same number.
There is a comment where that function would have been, and a test that the
order does not move. What calibration does change is a feasibility warning: a
gap that fits the estimate but not the time those journeys have actually taken
now says so. It may add a warning; it may never clear one.

Found on the way: **`brief.timezone` had never been written by anything.** Its
docstring says it comes from Places, `today_at` reads it to answer "what is
today where they are going", and the reflection gate, the flight date logic and
the model's daily context all go through it — so every trip that has ever
existed computed its dates in UTC, while all 332 entities in the database sat
there carrying the correct zone.

## A plan that validates can still be a plan that only works if nothing slips

The validator answers "is this schedule consistent as written" — one scenario,
raw figures. The stress test (旅行预演) answers the question after that one: when
every figure wanders inside the width it is known to wander, which days stay
comfortable and which quietly depend on everything going right?

Not prediction. The engine propagates the plan's own figures and this system's
**stated** assumptions through each day — previous departure plus route plus
parking plus check-in equals an arrival window — under three scenarios. Every
input is an interval wearing its provenance: a measured drive brackets its
claim by a per-mode spread, or by the earned calibration band where milestone
10 has one (`calibrated` only — provisional evidence moves nothing); the
parking buffer is an assumption that refuses to be a point, which is why even
the *expected* arrival is honestly a range:

```
Lunch reservation: planned 11:00 · expected 10:58 · conservative 10:58–11:02
   walking · 28 min · provider figure                              [measured]
   finding parking and walking in · 5–15 min                     [assumption]
⚠ late_arrival_risk — fragile in the conservative case, fine as expected
```

An unmeasured leg advances nothing. The chain resets to the schedule,
everything downstream says "assumes the schedule held", and no lateness is
ever derived from an invented zero — a day resting on unknowns caps at
`workable` from both directions, since it can be called neither safe nor
dangerous on data nobody has.

Verdicts are four words — comfortable, workable, fragile, blocking — with one
rule each and no score. The validator stays the single authority: its errors
*are* `blocking`, its warnings fold into findings carrying its own sentences,
and nothing is recomputed against a second set of thresholds. New ground is
only what nobody covered — meal gaps, and outdoor stops running past sunset
whatever their name says.

"Make this day safer" is the existing scoped replan behind a new button:
locked items survive byte-for-byte, unrelated days do not move a byte, one
revision is spent, and the open stress panel re-runs itself against the day
that now exists. The model's role is fenced in the prompt: it runs the tool
and explains the findings; it never does schedule arithmetic itself.

## A fork in the road is a card, not a paragraph

No airline flew ALB → MDW on the chosen dates. The agent worked out the right
answer and wrote it down:

> The practical next step is to reconsider the arrival airport — Chicago
> O'Hare (ORD) was the other airport previously shortlisted.

Then it stopped, with nothing on screen to do it with. Every existing surface
refused the question: intake questions must name an outstanding requirement,
open questions carry no choices and **no route anywhere can mark one
answered**, and the chooser card hides a decision that is already settled —
which the arrival airport was, with no way in the codebase to re-open it.

**`propose_next_step` turns a fork into buttons.** The actions are a closed
set — settle a decision on an option it already has, park a part, pick a parked
part back up, or simply carry on — so the model can phrase any question it
likes but can only ever offer something the system already knows how to do.
Every proposal must include a way to leave it: a question with one usable
answer is not a question.

Answering "leave flights for now" writes `Decision.set_aside_reason` and
planning continues with the neighbourhoods, the places and the days. It does
**not** touch `brief.scope.flights`, which stays `plan`: they do still want
flights, we just could not find any. `not_needed` would claim they are not
flying, `already_arranged` would claim tickets exist, and `unknown` would
re-open a blocking gap and un-confirm the brief.

Fixing it turned up a second thing: a search that found nothing used to store
nothing, so "no airline flies this route" and "nobody has looked" were the same
state — and `next_steps` tested whether the decision *existed*, so the step
disappeared exactly when it mattered most.

## Two airports of one city are two different airports

A traveller was shown two arrival-airport cards reading, in full:

```
Chicago                                Chicago
✓ 22 min drive from the pickup point   ✓ 22 min drive from the pickup point
✓ serves Chicago                       ✓ serves Chicago
```

The stored options were never that alike — ORD and MDW differ by name, by code,
and by **24.9 km against 14.7 km**. Four renderers each dropped the part that
separated them: `label_for` returned at `city`, two attributes before the
`iata` branch that existed for exactly this; `metrics_for` had no airport
branch, so no figure ever reached a card; the drive time was rounded to the
minute, turning 21.5 and 21.8 into one number; and `serves {city}` is identical
for every airport of a city by construction.

`label_for` also feeds the model's own state summary, so the agent could not
tell them apart either — which is why it wrote "Chicago" twice.

The card now reads `Chicago O'Hare International Airport (ORD)` with its
minutes to a decimal and its distance beside them. No test caught this because
every airport fixture set `city=iata`: two airports of one city could not exist
in the suite at all.

## The trip you have, then what is still being asked

The intake panel put the outstanding questions on top and the brief — the thing
being confirmed — below them. It reads the other way round now: what you have
already told us, what you can tick, what is still being asked, and last the one
button that starts the trip.

Beside that button is a final question: **anything else I should know?** Say no
and planning starts; say yes and you get a box. What you type is kept verbatim
in `brief.notes` — a field that had been writable since the first milestone and
read by nobody, so anything a traveller asked to be planned around went into
the store and stopped there. It now reaches the model every turn and appears on
the brief card, where it can be corrected.

The agent turns what is actionable in it into constraints with a new
`record_constraints` tool — nothing in the codebase could create a
`TripConstraint` before, though the model could see them and the prompt called
them non-negotiable. **The words stay exactly as written.** A constraint is a
reading of them, never a replacement: a misreading can be corrected from the
original sentence, and nothing can be recovered from a paraphrase.

## A button called "Start planning" now starts planning

The same failure, one step earlier and on every trip: the traveller answers
the intake questions, presses **Start planning**, and nothing happens. The
endpoint wrote four fields, lifted the research gate, and returned — and
nobody walked through the gate it had just opened.

Measured across the whole store, this had never once worked: **8 of 8**
confirmed trips have a user message *after* the confirmation, and not one
assistant turn was ever unprompted. Everybody clicked, saw nothing, and typed
again.

The click now sends the first message itself. **Unconditionally** — unlike the
card continuation, which waits until nothing is left to pick. Pressing a button
labelled Start planning is the instruction, and it does not need corroborating:
a trip whose flights and hotel are already booked has nothing to shop for, and
a check for "is there work?" would have refused exactly the traveller with the
least left to arrange.

That trip also exposed a hole in `next_steps`, which reported nothing to do for
it — no shopping, and an itinerary needs places no search had found yet.
Finding places is a step in its own right now, which is also what the model is
told each turn.

## Choosing a card is the traveller saying "go on"

Reported by the same traveller a day later: they picked their departure
airport, arrival airport and neighbourhood, and planning stopped. Three
revisions committed correctly; the audit trail ended there.

A selection was a state change with no consequence. The card vanished, the
attention bar went quiet, and the only code path in the browser that started
an agent turn was the Send button — beside a reply promising to find flights
and hotels once they had chosen.

**The last choice now starts the next turn**, naming what was chosen, and the
agent stops again at the next set of cards. Three cards cost one turn, not
three; re-choosing what is already chosen costs none. Stopping is still the
Send button, which becomes Stop while a turn runs.

Fixing it needed something that did not exist: **a single answer to "what is
this trip waiting for"**. Three places had their own — `SINGLETON_DECISIONS`,
the browser's `CHOICE_ORDER`, and fifteen lines inside the progress strip —
and none of them encoded a dependency, so nothing could say that a hotel waits
on its neighbourhood. `next_step.next_steps` is now that answer, derived
server-side and read by the overview, the model's own state projection, and
the progress strip. The line under the composer says it out loud:

```
Next: find flight options, then find a place to stay, then build the daily itinerary
```

And the choice cards show their figures again. They had rendered `o.figures`,
which `OptionView` does not have — so every card ever shown carried a name and
its pros and cons and not one number, while the full decisions panel showed
them correctly. That is why the agent kept copying whole rankings into its
prose: the cards it pointed at were empty.
