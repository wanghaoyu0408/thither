# 问津 · Whither

A personal group travel-planning agent. It helps decide where to go, compares real
options, builds a geographically and temporally feasible itinerary, explains why it
recommended what it did, and replans *part* of a trip when you ask.

It does not book anything, does not touch payments, and does not treat LLM-generated
facts as authoritative.

*问津* is to ask the way at a river crossing — 陶渊明's *后遂无问津者*, the line that
closes Peach Blossom Spring once nobody asks after the ford any more. *Whither* is the
same question in English: **whither goest thou?** Both are a question, not a promise,
which is the right register for something whose whole discipline is saying what it does
not know.

## Status: Milestones 1-10 complete

The architectural claim this project rests on is that **`TripState` is the source of
truth and the LLM may never overwrite it freely**. Every mutation goes through a
validated `TripPatch` with revision control, lock enforcement and rejection memory.

| Milestone | Scope | Status |
|---|---|---|
| 1 | TripState, patch engine, locks, rejections, persistence, REST | done |
| 2 | Google Places + Routes behind replaceable providers | done |
| 3 | Itinerary generation, validator, scoped local replanning, agent | done |
| 4 | Web research: Xiaohongshu / Reddit / blogs, resolved against Google | done |
| 5 | Flights (Duffel), airport comparison, ranking with trade-offs | done |
| 6 | Hotels: neighbourhood decided first, then priced inventory | done |
| 7 | Multi-traveler preferences, group scoring, fairness | done |
| 9 | Evolving travel twin: learning signals, consent, Travel DNA | done |
| 10 | Calibration: the agent measures its own error against what happened | done |

## Setup

Python 3.12+. This repo uses a project-local virtualenv.

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env`. `DATABASE_URL` defaults to SQLite, so the only thing you
must fill in is `GOOGLE_MAPS_API_KEY` — and only for Milestone 2 onwards.

That key needs **both** of these enabled in Google Cloud Console, on a project with
billing on:

- **Places API (New)** — the legacy "Places API" will not work; the field-mask header is
  a New-API concept
- **Routes API**
- **Weather API** — only for forecasts. Another separate API. Without it the
  forecast half simply does not run and the historical half still does, because
  Open-Meteo needs no credential at all.
- **Maps JavaScript API** — only for the map in the web UI. It is a *separate* API, so a
  key that plans trips perfectly well can still be refused by the map. If it is, the map
  panel says so rather than showing a grey box.

`MAPS_BROWSER_API_KEY` is optional and falls back to `GOOGLE_MAPS_API_KEY`. Set it when
this app is reachable by anyone but you: the page publishes whatever key it loads, so a
shared key hands out your Places and Routes budget along with the map, and you cannot fix
that with an HTTP-referrer restriction because the same restriction would break the
server's own calls. Two keys — one referrer-restricted for the browser, one unrestricted
for the server — is the only arrangement that restricts both. While it is shared, the UI
says so under the map.

From Milestone 3 the conversation endpoint also needs `OPENAI_API_KEY`, and
`OPENAI_MODEL` if you want something other than the default.

The offline test suite needs no key at all.

### Run

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

Then open **http://127.0.0.1:8000** for the web UI, or `/docs` for the raw API.

The UI is one file (`app/web/index.html`) with no CDN and no key baked into it — it fetches
what it needs from `/ui-config` at runtime. It loads exactly one thing from outside: Google's
Maps JavaScript, and only when a key is configured; without one the workspace still works and
the map panel explains its own absence. It shows the trip,
the itinerary, the validation checks, every preference conflict with each person's position
stated separately — and, under each reply, the actual tool calls the agent made. A turn takes
minutes because it is really calling Google, Duffel, SerpApi and an LLM, so it shows a clock.

### Test

```bash
.venv/Scripts/python.exe -m pytest -q
```

982 tests, no network, no API keys. The `tests/scenarios/` files map one-to-one onto each
milestone's acceptance criteria.

Contract tests against the real Google APIs are opt-in, since they cost quota:

```bash
.venv/Scripts/python.exe -m pytest -m live --override-ini addopts=
```

### Milestone 2 acceptance

```bash
.venv/Scripts/python.exe scripts/build_tokyo_trip.py
```

Searches real places in Shibuya and Asakusa, ranks them, fetches details for the
shortlist only, resolves them into entities, computes real walking times, and persists
ranked shortlists — every write going through the patch engine. Re-run it with
`--trip-id <id>` to confirm resolution is idempotent: the entity count must not grow.

### Milestone 3 acceptance

```bash
.venv/Scripts/python.exe scripts/plan_tokyo_trip.py
```

Runs the two-turn conversation — *"Plan 5 days in Tokyo."* then *"Day 3 is too busy. Make
it easier."* — and prints a per-day before/after diff so "only Day 3 changed" is visible
rather than asserted. The same criterion is also proved without an LLM in
`tests/scenarios/test_milestone3_acceptance.py`, so a model having an off day cannot make
the milestone look broken.

### Milestone 4 acceptance

```bash
.venv/Scripts/python.exe scripts/discover_restaurants.py
```

Runs the spec §20 pipeline twice — once with every source, once with Xiaohongshu removed
entirely — and prints each recommendation's Google facts beside its community sources,
plus which mentions could not be matched to a real place. Each tier reports what it did,
so "no Xiaohongshu links" is distinguishable from "Xiaohongshu found nothing".

### Milestone 5 acceptance, and its caveat

```bash
.venv/Scripts/python.exe scripts/compare_airports.py
```

Real driving times, then a real search from all three airports, then the trade-off in
figures that all came from a tool:

```
Best from each airport:
   SFO      916 USD  0 stop(s)  10h35m  56 min drive   54 option(s)  score 0.773
   OAK      922 USD  1 stop(s)  17h29m  28 min drive    9 option(s)  score 0.570
   SJC      927 USD  1 stop(s)  14h00m  76 min drive   37 option(s)  score 0.558

Why the recommendation wins  (real fares)
   - 390 USD more per person
   - 17h15m shorter
   - nonstop instead of one stop at MNL
```

That answers the question a traveller actually asks: Oakland is half the drive, but it
costs seven more hours and a connection.

The per-airport block exists because the flat ranking hides the comparison — when one
airport dominates it takes every slot, and "should we drive to Oakland instead?" goes
unanswered.

**Searching never costs anything at Duffel** — only ticketing does — and this codebase has
no path to ticket: `DuffelProvider` exposes exactly one public method, `search_offers`, and
a test asserts the module contains nothing named for orders, payment, seats or
cancellation. A live token needs the `air.offer_requests.create` scope; without it the live
tests skip naming that remedy rather than passing vacuously.

### Milestone 6 acceptance, and which half is live

```bash
.venv/Scripts/python.exe scripts/choose_hotel_area.py
```

Spec §25 does not ask for a hotel search; it dictates an order. *Destination → anchor POIs
→ candidate neighbourhoods → rank them on real travel time → recommend an area → **then**
search hotels in it.* The area half needs only Google, so it is verified live against real
Tokyo geography:

```
times are driving minutes

area                         mean   worst   reach   score
Ueno                          12m     18m   5/5     0.818
Asakusa                       12m     21m   5/5     0.804
Shibuya                       25m     30m   5/5     0.584
Shinjuku                      29m     33m   5/5     0.542
```

Every trip anchor is in east Tokyo, so Ueno wins and Shinjuku loses — by measured minutes,
not by reputation.

**The ordering is enforced server-side.** `search_hotels` refuses without a resolved area
and names `recommend_hotel_areas`; integrity fails a selected hotel that sits outside the
selected area. Skipping the step stays possible through `bypass_area_decision`, which
requires a reason and stores it on the option — "book the Park Hyatt" still works, it just
leaves a trace.

**Amadeus is gone.** Self-Service was decommissioned on 17 July 2026, so the spec's chosen
hotel provider no longer exists. Replacing it cost one class, because nothing above
`HotelProvider` knew its name — which is the argument for §3's structure made concrete.
`SerpApiGoogleHotelsProvider` is the initial live price source; Booking.com Demand API is
documented as the next one. No provider under that interface has, or will gain, a booking
method.

Then, and only then, hotels inside it:

```
Ueno Urban Hotel Tokyo
   71 USD/night   11 min avg   score 0.780
   rating: 3-star (Google Hotels)
   rating: 3.9/5 from 417 reviews (Google Hotels)
   price:  71 USD/night at Ikyu.com
   price:  73 USD/night at klook

Too close to call
   Nothing measured meaningfully separates Ueno Urban Hotel Tokyo from APA HOTEL UENO-EKIKITA:
   - 1 USD less per night
   - the same 3.9/5, but from only 417 reviews against 1,762
```

**Two ratings are never one number.** A star category and a guest score measure different
things, so `HotelOptionData.ratings` is a list of typed, sourced `HotelRating`s rather than
the single float it started as. A five-star with no reviews is not highly rated — it is
unreviewed, and scores nothing at all on guest rating.

**A price with no source behind it is not a price.** The search advertises a property "from
$70"; the detail call names who will actually take the booking. Those disagree often enough
to matter, so the advertised figure is kept aside as `headline_nightly`, ranking uses the
cheapest rate attributable to a named site, and a gap between them is stated rather than
met at checkout. `featured_prices` — `aclk` links carrying a `gclid` — are advertisements
and never read as quotes.

**A one-dollar gap is not a recommendation.** When nothing measured separates two hotels,
the trade-off says so instead of dressing up a rounding difference as a winner. Review
depth is reported rather than scored: 3.9 from 1,762 reviews is a firmer 3.9 than 3.9 from
417, which is a statement about confidence, not quality.

**What has no data is said, not scored.** Spec §23 lists quietness as a hotel dimension and
no provider publishes it, so it is not in the ranking and a traveller who cares is told so.
Same for room size.

**Enrichment is spent on the shortlist.** Five properties get a price-detail call, a Places
lookup and a route matrix; the other fifteen get none. Ranking first and enriching after is
what makes that ordering structural rather than remembered.

Four things the live runs turned up that no offline test could:

- **Google has no transit routing data for Japan.** Both Routes endpoints answer transit
  queries in the US and the UK and return no route at all in Tokyo. So a mode with no
  coverage is retried once in a fallback mode and the substitution is declared — the
  minutes above say "driving" because that is what was measured. A driving minute must
  never be read as a train minute, so `route_mode` travels with every figure and two
  differently-measured times are never compared.
- **`compute_route` had never worked.** It wrapped waypoints the way only the matrix
  endpoint accepts, so every call would have been rejected. Dead code until now.
- **A hotel search returns almost no per-vendor prices.** On a live Ueno search, two of
  twenty properties carried a `prices[]` array; the named booking sites live behind a
  per-property detail call. The interface gained a second method for it, because who pays
  for that call is the caller's decision, not the provider's.
- **The agent had no headroom.** A successful five-day plan spent 13 tool calls against a
  12-round cap, so the acceptance turned on whether the model happened to be economical.
  The cap is now 16, and — more importantly — running out of rounds sets
  `hit_iteration_limit` instead of returning a half-finished turn that looks finished.

Nothing here needs a key the project does not have: `SERPAPI_API_KEY` covers the hotel
half. Without it the script prints the area decision and stops, saying so.

### Milestone 7 acceptance

```bash
.venv/Scripts/python.exe scripts/plan_for_the_group.py
```

The acceptance is the only one in this project phrased as a prohibition — *identify major
preference conflicts **instead of hiding them behind an average score*** — and the thing
prohibited is the obvious implementation. Four friends, live Tokyo data:

```
Grids Tokyo Ueno Hotel & Hostel     153 USD/night   4.1/5 from 460 reviews
   group: 0.55, and nobody has much to go on (0.30 apart)
   each:  Ann 0.77  Bo 0.62  Cy 0.73  Dee 0.46

A plain average would have recommended NEO ART HOTEL Akihabara (mean 0.68 against 0.64).
It is not recommended, because it leaves Bo at 0.54.
```

**A mean cannot tell a fight from a consensus.** Three travellers at 0.9 and one at 0.1
average to the same 0.5 as four at 0.5. So `GroupScore` keeps every person's score, names
who is worst served, and sorts on `0.6·mean + 0.4·worst`. Its `describe()` is the only
rendering, and a split cannot be described without saying whose.

**Conflicts are derived, not stored**, in the mould of `signals_from_evidence` — so what is
on screen can never drift from the preferences behind it. Each one records `positions` per
person, which is the anti-averaging device: there is no form of the record in which the two
sides have already been merged. Their ids are content hashes, because a record recomputed
on every read needs a stable identity or an answer can never be matched to the question.

**Preferences are snapshotted into the trip**, stamped with the profile revision they came
from. Editing a profile next year cannot rewrite why last year's trip was planned that way.
A refresh diffs first, applies only on confirmation, and marks *only the affected*
decisions stale rather than replanning anything.

**A blocking conflict stops one thing: the claim that the trip is ready.** Enforced in the
patch pipeline, not the prompt. Research, generation and replanning all carry on.

**Google attests that a place serves vegetarian food; it never attests that one does not.**
Measured live: 8/8 Indian restaurants in Shinjuku are confirmed, but only 1/8 Shibuya
pizzerias — and every pizzeria serves a margherita. So `False` means *not attested*, is
normalised to `None`, and a dietary conflict raises a blocking question rather than
filtering. Deleting most of Tokyo from a vegetarian's trip on the strength of a field that
only ever means "nobody said" would be exactly the confident wrongness this project exists
to avoid. Both fields ride on the FULL details call the planner already makes for the
shortlist, so they cost nothing extra.

Defects the live runs exposed, most of them older than this milestone:

- **An option could win on ignorance.** A hotel with three reviews has no usable guest
  rating, no star category and no measured travel time, so price was its only dimension —
  and being cheapest made it a flawless 1.00 that beat a hotel we knew five things about.
  `combine()` now reports `coverage` and pulls a thinly-evidenced score toward neutral. You
  cannot recommend what nobody has looked at.
- **A rating could be untrustworthy and authoritative at once.** 5.0-from-3-reviews was too
  thin to score on, yet still cleared a traveller's stated 4.5 floor.
- **A preference could influence nothing.** `min_rating` was a filter for one traveller and
  absent from the group path; and the taste-to-weights mapping scaled every dimension by
  one factor, which under renormalisation is exactly a no-op.
- **`apply_trip_patch` discarded staged places** when called without a proposal — which the
  agent does routinely, having just discovered thirty of them. It then described a plan it
  had not saved. Fixed, and a missing-but-named `proposal_id` is still an error rather than
  a partial write reported as success.
- **Nothing told the model a proposal changes nothing until applied.** It generated twice
  and stopped. The prompt now says so, and `review_group_preferences` returns immediately
  for a solo trip rather than spending a planning round proving one person agrees with
  themselves.

### A turn's findings no longer depend on the model remembering

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

### A return trip is two flights

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

### Nobody was ever in the trip

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

### Reachable, and still empty

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

### It never checked whether it was right

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

### A fork in the road is a card, not a paragraph

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

### Two airports of one city are two different airports

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

### The trip you have, then what is still being asked

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

### A button called "Start planning" now starts planning

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

### Choosing a card is the traveller saying "go on"

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

### Milestone 9 acceptance

```bash
.venv/Scripts/python.exe scripts/learn_from_trips.py
```

Offline, keyless, deterministic: two trips' worth of behaviour, a reflection,
a consent, a durable "no", and a generated day that finally starts later.

```
Prefers later mornings     proposable  strength=moderate  confidence=likely
    - Kyoto in July — moved 'Fushimi Inari at dawn' from 08:00 to 11:00
    - Kyoto in July — moved 'Arashiyama bamboo early' from 08:30 to 10:30
    - Kyoto again, August — reflection: skipped Kiyomizu at sunrise (07:30)

old profile      first slot of the day: 10:00
learned profile  first slot of the day: 11:00
```

**Learning proposes; only the traveller applies.** Signals are stored facts —
an activity dragged later, a sentence said, a reflection submitted. Hypotheses
are derived from them on every read, never stored, with content-hash ids so
"not really" said today still matches the same pattern derived tomorrow. The
only path into `TravelerProfile` is the accept endpoint, which writes the
value and its provenance in one revision-guarded update. The trip that taught
the lesson keeps its own snapshot untouched.

**Strength is not confidence.** How intensely a preference was expressed (a
click is `weak`, a post-trip statement `moderate`, words `strong`) and how
much evidence there is (`emerging` / `likely` / `strong`, needing
`learning_min_signals` across `learning_min_trips` distinct trips) are two
ordinal words, reported apart and never folded into a number — a percentage
computed from three clicks would be fake precision wearing a decimal point.

**Attribution or abstention.** Behavioural signals are recorded only on a trip
with exactly one profiled traveller, because only there does "who did that?"
have an answer. On a group trip the agent's `record_stated_preference` must
name the speaker, and the reflection asks who is answering. A preference
pinned on the wrong person is worse than one never recorded.

**A "no" is durable.** Dismissing a proposal appends a profile-scope
`RejectionRecord` — the first real use of the scope the model always
promised — and however much evidence arrives later, the same hypothesis
derives as dismissed. Removing a learned value reverts to what the field held
*before* learning touched it, and appends the same dismissal so the untouched
evidence cannot re-propose it on the next read.

**The learned field finally moves something.** `preferred_start_time` had
been stored, snapshotted, diffed and displayed — and consumed by nothing (the
ledger-10 defect class). Generation now shifts every day template to the most
morning-averse traveller's floor, capped so dinner never slips past 20:00,
and `parking_sensitive` doubles parking friction in substitute ranking. The
four weights learned from card choices — nonstop and price for flights,
location and price for hotels — are the numbers `flight_ranking` and
`hotel_ranking` already multiply their dimensions by. Every key in the
learnable catalogue names its consumer in code, because a preference that
influences nothing is a lie told slowly.

### Milestone 10 acceptance

```bash
.venv/Scripts/python.exe scripts/check_our_own_numbers.py
```

Offline, keyless, deterministic. Seven acts: a trip's checkable figures derived
without a row being written to make it possible; the honest "never checked";
an advertised rate refuting itself from the same fetch; the card rationed to
two questions; answers accumulating until the record will speak; a 95-minute
journey against a 14-minute estimate moving the median by nothing; and the
trips deleted, after which the predictions are gone and the record remains,
carrying no trip id and nothing finer than a region.

```
5. Answers accumulate, and it refuses to speak until they add up
   after 4 check(s): uncalibrated nothing yet
   after 5 check(s): provisional  off by +32%, 8 in 10 between -3% and +92%

6. One road closure is not a finding
   a 14-minute estimate that took 95 minutes is a +579% error
   median before: +32.5%    after: +32.5%
```

## How a change reaches the database

```
user message
    -> load TripState
    -> LLM proposes a TripPatch          (milestone 3+)
    -> revision match                    else 409
    -> protected paths refused
    -> operations applied to a copy      (RFC 6901 JSON Pointer)
    -> whole state re-validated          -> schema errors surface here
    -> locks enforced                    else 422 LOCK_VIOLATION
    -> rejections enforced               else 422 REJECTION_VIOLATION
    -> hard constraints checked          else 422 CONSTRAINT_VIOLATION
    -> referential integrity checked     else 422 INTEGRITY_ERROR
    -> revision += 1, persisted, audited
```

Any failure aborts the whole patch. There is no partial application, and no path that
lets a caller hand back a replacement state.

## Design decisions worth knowing

The load-bearing rules — and one test per defect that a live run turned up — are
written down in **[INVARIANTS.md](INVARIANTS.md)** rather than left in anyone's head:
absence is not negation, a score is not a confidence, a write is all-or-nothing and
success is reported only after re-reading the row, a group score never hides a split,
and conflict detection cannot be moved by tuning a ranker.

**Locks address ids, not paths.** A path-based lock silently stops protecting its target
the moment an array index shifts. `LockRecord` names `(target_kind, target_id)`, and
enforcement diffs the target's serialized content between the pre- and post-patch states
— so a change is caught regardless of which pointer reached it. Locks are enforced from
the *pre-patch* state, and deleting a lock record requires naming its `lock_id` in
`unlock_targets`; otherwise a patch could drop its own obstacle and edit freely.

**Rejections block re-introduction only.** References are compared by `(context, id)`,
so an entity that already sits in the registry but is newly *scheduled* still counts as a
fresh recommendation. `allow_rejected` is the explicit override for "actually, reconsider
it".

**Hard-constraint checks say when they cannot check.** Most constraint categories need
data that does not exist until a later milestone. Rather than return a misleading "ok",
checkers return `ok | violated | not_checkable`, and `not_checkable` surfaces as a
warning on the patch result. Milestone 1 implements `budget` and `schedule`; the rest
report honestly that no checker exists yet.

**Optimistic concurrency lives in SQL.** The write is
`UPDATE trips ... WHERE id = :id AND revision = :base_revision`; zero matched rows means
another writer won. The Python check alone would leave a race between read and write.

**Caching is classified by content, not by endpoint.** Google's terms permit storing
place ids indefinitely and lat/lng for at most 30 days, and nothing else. So
`SqliteCache` accepts only those two classes and *raises* on anything else; names,
ratings, hours and route durations live in an in-process cache that dies with the
process. A policy that is merely documented erodes; one that throws holds. The main
practical saving is request dedupe, which collapses concurrent identical calls and is
unconditionally safe.

**Place facts in `TripState.entities` are a snapshot with an expiry date.** That registry
is the user's saved itinerary rather than a cache, but facts older than 30 days are
treated as stale and must be re-fetched before being quoted — which is also what spec
§30 requires, since no number should reach the user from a stale snapshot.

**Field-mask tiers are explicit per call.** Google bills by the most expensive field
requested, and `rating` / `userRatingCount` are Enterprise-tier — so "search cheap, then
fetch details" cannot work literally: a Pro-tier search returns nothing to rank on. The
saving comes instead from fetching `FULL` for the 3-5 shortlisted places rather than all
20 candidates.

**Transit route matrices are capped at 100 elements**, against 625 for walking and
driving. A 12×12 transit matrix fails as one call, so `route_service` chunks per mode,
issues the sub-matrices concurrently, and remaps chunk-local indices back to the
caller's.

**"Only Day 3 changed" is enforced, not hoped for.** `TripPatch.scope` names a day and
the server canonically diffs everything outside it; a patch that reaches further is
rejected with `SCOPE_VIOLATION`. Like locks, it addresses the day by date rather than by
path, because `/itinerary/days/2` stops meaning day 3 the moment an earlier day is
removed. Entities may be *added* under scope — a replan sometimes needs a place the trip
has never seen — but not rewritten, so refreshing a venue's opening hours is split off
into its own unscoped patch rather than weakening the rule.

**The model plans nothing by hand.** It picks areas, themes and what to sacrifice; the
clustering, scheduling, routing, validation and patching are ordinary code (spec §26,
§44). `generate_itinerary` fetches the opening hours and walking times for what it is
about to schedule, rather than relying on the model to remember — an itinerary that is
checked beats one that merely reports itself unverified.

**Sandbox flight data is labelled everywhere it goes.** `FlightOptionData.live_mode` has
no default, so no construction site can forget to say which it is. It persists into
`TripState`, the tool result carries a disclaimer, and the validator raises a warning when
a trip holds sandbox options — a warning that becomes louder if one is selected. A prompt
rule alone would be a hope; the stored flag and the validator are what make it hold.

**Explicit preferences are not outbid by small savings.** An airline the traveller asked
to avoid is filtered out rather than discounted, and "avoid red-eyes" is its own dimension
rather than a fraction of a schedule weight.

**Price scoring took three attempts, and the two failures are instructive.** Normalizing
across the candidate range made a $10 gap score exactly like a $500 one. Replacing that
with a proportional score and a floor at +30% then made every expensive fare score zero,
so a $916 and a $1072 fare tied and the tie fell to whichever offer id sorted first — the
live run recommended the dearer of two identical flights. It is now a smooth decay that
never saturates, and price breaks a score tie before the id does.

**Airport convenience is a real driving time.** `search_airports` geocodes the location,
finds airports in range, and asks the Routes API for actual drive times — the only honest
basis for "SFO or SJC?". The dataset's own `scheduled_service` flag is not trustworthy
(it is set on Buchanan Field and San Carlos, which no airline serves), so major airports
are preferred and secondary ones appear only when there is no major one in range.

**Facts and opinions are stored in different places.** `PlaceEntity` holds only what
Google asserts. What people said lives in `TripState.evidence`, referenced by
`DecisionOption.evidence_refs`. Keeping them apart is structural, not stylistic: it is
what stops "someone on Reddit liked it" being handled like "it opens at 11:30".

**Source URLs come only from citation annotations, never from model prose.** A model asked
for a source will write a plausible URL that does not exist. The research provider runs
two passes — a `web_search` call whose `url_citation` annotations are the only source of
URLs, then a structured pass that refers to those citations *by index*. An index that does
not resolve is dropped rather than guessed.

**Community signal reorders; it never rescues.** Hard filters run before signal is
consulted, so a place that is permanently closed or below the rating floor cannot be
saved by being fashionable. A place nobody mentioned is not penalised for it — the
dimension is simply absent, the same way a missing price level is.

**Xiaohongshu is never load-bearing** (spec §36). The community tier and the open-web tier
are separate searches, and each tier's outcome — found, empty, failed, or not run — is
recorded rather than inferred from the absence of links.

**Opening hours are read from `periods`, never from `openNow`.** That flag is a snapshot
from whenever the place was fetched; ours says `false` about an afternoon in August and
would mark half the shortlist shut for a trip in October. The parser handles the cases
real Tokyo data contains: several periods a day (the lunch/dinner split), periods
crossing midnight, 24-hour venues with no `close` at all — and treats missing hours as
*unknown*, which is not *closed*.

## Layout

```
app/
  models/      TripState, brief, constraints, decisions, entities, itinerary,
               locks, rejections, patches, tool/place/route/chat shapes
  providers/   google_places, google_routes, openai_llm, shared HTTP + errors
  services/    patch_service (the pipeline above), lock_service, scope_service,
               rejection_service, constraint_service, integrity_service,
               json_pointer, state_walk, place_service, route_service,
               entity_service, ranking_service, clustering, opening_hours,
               itinerary_service, validation_service, proposal_store,
               cache, toolbox
  agent/       runner (the loop), prompts, tool_registry, context
  db/          SQLAlchemy tables, repositories, session
  api/         REST endpoints, /tools debug probes, chat
migrations/    Alembic (sync driver; app runs async)
scripts/       runnable milestone demos
tests/         unit / integration / scenarios / live
```

Provider code sits below the service layer so the agent calls `search_places()` rather
than a Google endpoint — and so providers stay replaceable. Nothing above the service
layer ever sees a provider exception: failures become `ToolResult.error`, which is what
lets the agent tell "nowhere here matches" from "the API is down" instead of inventing
an answer.

## API

| Method | Path | |
|---|---|---|
| POST GET PATCH | `/profiles`, `/profiles/{id}` | long-term traveler preferences |
| POST GET | `/trips`, `/trips/{id}` | create returns revision 0 |
| POST | `/trips/{id}/patch` | the only way to mutate a trip |
| GET | `/trips/{id}/state`, `/trips/{id}/events` | debug: current state, audit trail |
| POST | `/tools/search_places`, `/tools/place_details`, `/tools/get_routes` | read-only probes; never write to a trip |
| POST GET | `/trips/{id}/messages` | talk to the agent; transcript kept for audit only |

## Storage

Three tables (`traveler_profiles`, `trips`, `trip_events`) with `TripState` stored whole
as JSON. The JSON column carries a Postgres variant, so moving from the SQLite dev
default to Postgres is a `DATABASE_URL` change and an Alembic run:

```bash
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/travel_agent .venv/Scripts/python.exe -m alembic upgrade head
```

`trip_events` is append-only and names what changed (`constraint_added`, `lock_added`,
`itinerary_updated`, ...) alongside the raw operations. `tool_cache` holds the durable
half of the cache and is safe to delete at any time — including the research entries,
because anything that actually backed a recommendation was promoted into
`TripState.evidence` when it did so, carrying its original `observed_at`.
