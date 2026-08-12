# 问津 · Whither

**A travel planning agent that is honest about what it does not know.**

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-1022%20passing-brightgreen.svg)](#test)

*English · [中文](README.zh-CN.md)*

It decides where to go, compares real options with real figures, builds an
itinerary that is geographically and temporally feasible, explains why it
recommended what it did, and replans *part* of a trip when you ask.

It does not book anything, does not touch payments, and does not treat
LLM-generated facts as authoritative.

> *问津* is to ask the way at a river crossing — 陶渊明's *后遂无问津者*, the line
> that closes *Peach Blossom Spring* once nobody asks after the ford any more.
> *Whither* is the same question in English: **whither goest thou?** Both are a
> question, not a promise, which is the right register for something whose
> whole discipline is saying what it does not know.

---

## Why this one is different

Most travel agents fail by being confidently wrong. This one is built so that
it structurally cannot be, and the rules are written down rather than hoped
for — all eight in **[INVARIANTS.md](INVARIANTS.md)**:

- **The state is the truth, not the conversation.** Every change the model
  makes goes through a validated JSON-Pointer patch with revision control,
  lock enforcement and rejection memory. The LLM proposes; it never
  overwrites. ([architecture](docs/ARCHITECTURE.md#how-a-change-reaches-the-database))
- **Absence is not negation.** "Nobody said whether this place is vegetarian"
  and "this place is not vegetarian" are different facts and are stored
  differently. A missing figure never becomes a zero.
  ([invariant 1](INVARIANTS.md#1-absence-is-not-negation))
- **A score is not a confidence.** Every ranking carries `coverage` beside
  `total`, so an option cannot win by being the one nobody has data about.
  ([invariant 2](INVARIANTS.md#2-a-score-is-not-a-confidence))
- **It measures its own error.** After a trip, it checks the numbers it gave
  you against what actually happened, and reports "never checked" out loud
  rather than as silence.
  ([invariant 7](INVARIANTS.md#7-a-figure-is-only-worth-something-if-something-could-contradict-it))
- **The engine computes; the model explains.** Every arrival window, verdict
  and travel time is deterministic code the model is forbidden from redoing in
  prose. ([invariant 8](INVARIANTS.md#8-the-engine-computes-the-model-explains))

The most-read document in this repo is probably
**[docs/FIELD-NOTES.md](docs/FIELD-NOTES.md)** — the defects that only appeared
by running the thing against real fares, real routes and a real traveller,
each with the test that now pins it.

---

## See it work in 60 seconds — no API key needed

```bash
git clone https://github.com/wanghaoyu0408/travel-agent
cd travel-agent
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Three demos are entirely self-contained — no key, no network, no server,
deterministic:

```bash
python scripts/learn_from_trips.py       # it learns, and only you get to apply it
python scripts/check_our_own_numbers.py  # it measures how wrong it usually is
python scripts/preview_the_trip.py       # the stress test, day by day
```

A fourth walks the HTTP surface — locks, rejections, revisions, the patch
engine refusing things — and needs the server up in another terminal, but
still no key:

```bash
python -m uvicorn app.main:app --reload   # terminal 1
python scripts/demo_milestone1.py         # terminal 2
```

`preview_the_trip.py` prints, among other things:

```
1. Safe as expected, fragile if the day runs slow
   2026-10-03 · FRAGILE   (1 of 1 journeys measured)
      Lunch reservation: planned 11:00 · expected 10:58 · conservative 10:58–11:02
         walking · 28 min · provider figure
      ⚠ late_arrival_risk: conservative arrival is 10:58–11:02

6. One road closure is not a finding
   a 14-minute estimate that took 95 minutes is a +579% error
   median before: +32.5%    after: +32.5%
```

Full catalogue, including the ones that hit live providers:
**[docs/DEMOS.md](docs/DEMOS.md)**.

### Test

```bash
python -m pytest -q
```

1022 tests, no network, no API keys. `tests/scenarios/` maps one-to-one onto
each milestone's acceptance criteria.

---

## Running it for real

Copy `.env.example` to `.env`. `DATABASE_URL` defaults to SQLite, so the only
thing you must fill in is `GOOGLE_MAPS_API_KEY`.

That key needs these enabled in Google Cloud Console, on a project with
billing on:

| API | Needed for | Without it |
|---|---|---|
| **Places API (New)** | finding places. The *legacy* Places API will not work | nothing works |
| **Routes API** | real travel times | every leg reports "not measured" |
| Weather API | forecasts | the seasonal half still runs; Open-Meteo needs no key |
| Maps JavaScript API | the map panel | the panel explains its own absence |

Optional, each unlocking one feature and degrading honestly without it:
`OPENAI_API_KEY` (the conversation itself), `DUFFEL_ACCESS_TOKEN` (flight
search — this codebase has no ticketing path at all), `SERPAPI_API_KEY` (hotel
prices).

```bash
python -m uvicorn app.main:app --reload
```

Then open **http://127.0.0.1:8000** for the web UI, or `/docs` for the API.

The UI is one file (`app/web/index.html`) with no CDN and no key baked into
it. A turn takes minutes because it is really calling Google, Duffel, SerpApi
and an LLM, so it shows a clock and the actual tool calls as they happen.

> **`MAPS_BROWSER_API_KEY` is optional and matters the moment anyone but you
> can reach the app.** A page publishes whatever key it loads, so a shared key
> hands out your Places and Routes budget along with the map. See
> [SECURITY.md](SECURITY.md).

---

## How a change reaches the database

```
user message
    -> load TripState
    -> LLM proposes a TripPatch
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

Any failure aborts the whole patch. There is no partial application, and no
path that lets a caller hand back a replacement state.

---

## Read more

| Document | What it is for |
|---|---|
| **[INVARIANTS.md](INVARIANTS.md)** | The eight rules that must not drift, each with why, where it is enforced, and the tests that pin it — plus a 60-row ledger of every defect found by running it |
| **[docs/FIELD-NOTES.md](docs/FIELD-NOTES.md)** | The long version of that ledger: what broke, how it was found, what changed |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | Design decisions worth knowing, the layout, the API surface, storage |
| **[docs/DEMOS.md](docs/DEMOS.md)** | Every acceptance script, what it proves, and what it costs to run |
| **[CONTRIBUTING.md](CONTRIBUTING.md)** | How to run the checks, and the house rules a change is expected to keep |
| **[SECURITY.md](SECURITY.md)** | What this does and does not protect, and how to handle keys |

---

## Scope, and what this deliberately is not

Being clear about this is the same discipline as the rest of the project.

- **Single user, no authentication.** There is no login and no per-user
  isolation: anyone who can reach the port can see and edit every trip. It is
  a personal tool. Do not expose it to the internet — see
  [SECURITY.md](SECURITY.md).
- **One process.** The per-trip agent mutex, the run registry and the HTTP
  cache all live in memory, so running multiple workers would silently break
  the guarantee that one trip has one turn at a time.
- **It does not book.** No ticketing, no payment, no reservation. Flight and
  hotel providers are read-only by construction, and a test asserts the Duffel
  module contains nothing named for orders, payment, seats or cancellation.
- **It does not trust the model with facts.** Every figure comes from a
  provider or from deterministic code. When there is no figure, it says so.

Milestones 1–11 are complete; there is no 8 (it was folded into 7). What each
one covers is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Licence

[Apache-2.0](LICENSE). See [NOTICE](NOTICE) for the third-party services this
depends on but does not redistribute — you supply your own credentials and are
bound by those providers' terms.
