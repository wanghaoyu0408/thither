# Travel Agent

A personal group travel-planning agent. It helps decide where to go, compares real
options, builds a geographically and temporally feasible itinerary, explains why it
recommended what it did, and replans *part* of a trip when you ask.

It does not book anything, does not touch payments, and does not treat LLM-generated
facts as authoritative.

## Status: Milestone 1 complete — state core

The architectural claim this project rests on is that **`TripState` is the source of
truth and the LLM may never overwrite it freely**. Every mutation goes through a
validated `TripPatch` with revision control, lock enforcement and rejection memory.

Milestone 1 builds exactly that, with no LLM and no external APIs, because every later
milestone writes *through* this layer.

| Milestone | Scope | Status |
|---|---|---|
| 1 | TripState, patch engine, locks, rejections, persistence, REST | done |
| 2 | Google Places + Routes (`search_places`, `get_routes`) | not started |
| 3 | Itinerary generation, validator, local replanning | not started |
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

Copy `.env.example` to `.env`. Milestone 1 only reads `DATABASE_URL`, which defaults to
SQLite — nothing else needs filling in yet.

### Run

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000/docs.

### Test

```bash
.venv/Scripts/python.exe -m pytest -q
```

109 tests, no network, no API keys. `tests/scenarios/test_milestone1_acceptance.py`
maps one-to-one onto the milestone's acceptance criteria.

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

## Layout

```
app/
  models/      TripState, brief, constraints, decisions, entities, itinerary,
               locks, rejections, patches
  services/    json_pointer, patch_service (the pipeline above), lock_service,
               rejection_service, constraint_service, integrity_service
  db/          SQLAlchemy tables, repositories, session
  api/         REST endpoints
migrations/    Alembic (sync driver; app runs async)
tests/         unit / integration / scenarios
```

Provider-specific code will live under `app/providers/` from milestone 2, kept below the
service layer so the agent calls `search_flights()` rather than a Duffel endpoint — and
so providers stay replaceable.

## API

| Method | Path | |
|---|---|---|
| POST GET PATCH | `/profiles`, `/profiles/{id}` | long-term traveler preferences |
| POST GET | `/trips`, `/trips/{id}` | create returns revision 0 |
| POST | `/trips/{id}/patch` | the only way to mutate a trip |
| GET | `/trips/{id}/state`, `/trips/{id}/events` | debug: current state, audit trail |

## Storage

Three tables (`traveler_profiles`, `trips`, `trip_events`) with `TripState` stored whole
as JSON. The JSON column carries a Postgres variant, so moving from the SQLite dev
default to Postgres is a `DATABASE_URL` change and an Alembic run:

```bash
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/travel_agent .venv/Scripts/python.exe -m alembic upgrade head
```

`trip_events` is append-only and names what changed (`constraint_added`, `lock_added`,
`itinerary_updated`, ...) alongside the raw operations.
