# Travel Agent

A personal group travel-planning agent. It helps decide where to go, compares real
options, builds a geographically and temporally feasible itinerary, explains why it
recommended what it did, and replans *part* of a trip when you ask.

It does not book anything, does not touch payments, and does not treat LLM-generated
facts as authoritative.

## Status: Milestones 1-3 complete

The architectural claim this project rests on is that **`TripState` is the source of
truth and the LLM may never overwrite it freely**. Every mutation goes through a
validated `TripPatch` with revision control, lock enforcement and rejection memory.

| Milestone | Scope | Status |
|---|---|---|
| 1 | TripState, patch engine, locks, rejections, persistence, REST | done |
| 2 | Google Places + Routes behind replaceable providers | done |
| 3 | Itinerary generation, validator, scoped local replanning, agent | done |
| 4 | Web research (Xiaohongshu / Reddit / blogs via web search) | not started |
| 5 | Flights (Duffel) | not started |
| 6 | Hotels (neighborhood first, then Amadeus inventory) | not started |
| 7 | Multi-traveler preferences, group scoring, fairness | not started |

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

From Milestone 3 the conversation endpoint also needs `OPENAI_API_KEY`, and
`OPENAI_MODEL` if you want something other than the default.

The offline test suite needs no key at all.

### Run

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000/docs.

### Test

```bash
.venv/Scripts/python.exe -m pytest -q
```

302 tests, no network, no API keys. The `tests/scenarios/` files map one-to-one onto each
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
half of the cache and is safe to delete at any time.
