# Travel Agent

A personal group travel-planning agent. It helps decide where to go, compares real
options, builds a geographically and temporally feasible itinerary, explains why it
recommended what it did, and replans *part* of a trip when you ask.

It does not book anything, does not touch payments, and does not treat LLM-generated
facts as authoritative.

## Status: Milestones 1-5 complete

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

602 tests, no network, no API keys. The `tests/scenarios/` files map one-to-one onto each
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
