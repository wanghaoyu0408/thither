# Architecture

How a change reaches the database, why the design is shaped this way, and
where everything lives.

The load-bearing rules have their own document —
[INVARIANTS.md](../INVARIANTS.md) — because they are the part that must not
drift. This is the map; that is the constitution.

---

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
written down in **[INVARIANTS.md](../INVARIANTS.md)** rather than left in anyone's head:
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
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/travel_agent python -m alembic upgrade head
```

`trip_events` is append-only and names what changed (`constraint_added`, `lock_added`,
`itinerary_updated`, ...) alongside the raw operations. `tool_cache` holds the durable
half of the cache and is safe to delete at any time — including the research entries,
because anything that actually backed a recommendation was promoted into
`TripState.evidence` when it did so, carrying its original `observed_at`.
