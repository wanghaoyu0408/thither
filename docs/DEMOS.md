# Demos — see it work

Every milestone ships a script that proves its acceptance criteria by running,
not by asserting. **Three of them need no API key, no network and no server** and are the
fastest way to see what this project is about:

```bash
python scripts/learn_from_trips.py       # learning proposes, only the traveller applies
python scripts/check_our_own_numbers.py  # the agent measuring its own error
python scripts/preview_the_trip.py       # the stress test, day by day
```

`demo_milestone1.py` needs no key either, but it walks the HTTP surface, so
the server has to be up in another terminal. It is re-runnable: the second
run finds the demo profile already there and says so.

The rest hit real providers and cost quota. They need the keys described in
[the README](../README.md#running-it-for-real).

---

## Milestone 2 acceptance

```bash
python scripts/build_tokyo_trip.py
```

Searches real places in Shibuya and Asakusa, ranks them, fetches details for the
shortlist only, resolves them into entities, computes real walking times, and persists
ranked shortlists — every write going through the patch engine. Re-run it with
`--trip-id <id>` to confirm resolution is idempotent: the entity count must not grow.

## Milestone 3 acceptance

```bash
python scripts/plan_tokyo_trip.py
```

Runs the two-turn conversation — *"Plan 5 days in Tokyo."* then *"Day 3 is too busy. Make
it easier."* — and prints a per-day before/after diff so "only Day 3 changed" is visible
rather than asserted. The same criterion is also proved without an LLM in
`tests/scenarios/test_milestone3_acceptance.py`, so a model having an off day cannot make
the milestone look broken.

## Milestone 4 acceptance

```bash
python scripts/discover_restaurants.py
```

Runs the spec §20 pipeline twice — once with every source, once with Xiaohongshu removed
entirely — and prints each recommendation's Google facts beside its community sources,
plus which mentions could not be matched to a real place. Each tier reports what it did,
so "no Xiaohongshu links" is distinguishable from "Xiaohongshu found nothing".

## Milestone 5 acceptance, and its caveat

```bash
python scripts/compare_airports.py
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

## Milestone 6 acceptance, and which half is live

```bash
python scripts/choose_hotel_area.py
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

## Milestone 7 acceptance

```bash
python scripts/plan_for_the_group.py
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

## Milestone 9 acceptance

```bash
python scripts/learn_from_trips.py
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

## Milestone 10 acceptance

```bash
python scripts/check_our_own_numbers.py
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

## Milestone 11 acceptance

```bash
python scripts/preview_the_trip.py
```

Offline, keyless, deterministic. Six acts: a day that validates cleanly still
cracking under its own error bars; every input wearing measured / assumption /
unknown on its sleeve; an unmeasured leg that is unknown rather than zero; an
earned calibration band replacing the assumption spread while a provisional
one moves nothing; "Make this day safer" running through the existing scoped
replan with the locked dinner byte-identical and the other day untouched; and
a verdict vocabulary you can count on one hand.
