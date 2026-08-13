# Contributing

Thanks for looking. This project has an unusual amount of written-down
opinion, so before the mechanics, the part that actually matters.

## The house rules

This codebase exists to demonstrate one thing: **an agent that is honest about
what it does not know.** Every rule below is downstream of that, and every one
of them was written after something went wrong. They are worth keeping.

**1. A defect gets a ledger row and a test that would have caught it.**
[INVARIANTS.md](INVARIANTS.md) ends with a numbered ledger — 63 rows so far —
of every defect that only appeared by running the thing. A fix without a row
is a fix that will be made again; a row without a test is a story. Both, or
neither.

**2. No second validation system.** There is exactly one authority for "is
this schedule feasible as written" (`validate_itinerary`), one for "how wrong
are our numbers" (`calibration_service`), one for "what should be learned"
(`learning_service`). New logic *folds* an existing authority's answer in — it
does not recompute it against its own thresholds. Two systems that can
disagree about a number will.

**3. Every figure carries its provenance.** A measured drive, an earned
calibration band, a stated assumption and an unknown are four different kinds
of number and must be four different things on screen. An unknown never
becomes a zero — not in a sum, not in a score, not in a window.

**4. Closed vocabularies stay closed.** `PreferenceKey`, `Dimension`,
`FindingKind`, `Verdict` are `Literal` types on purpose. Adding a value means
naming who consumes it (a preference nothing reads) or who could contradict it
(a claim nothing can check). Both mistakes have their own ledger rows.

**5. No fake precision.** Ordinal words over invented percentages. "87%
feasible" is a lie with a decimal point in it; `fragile` is a claim you can
argue with.

**6. Anything user-facing is written for a traveller.** Ledger 60 is a panel
that printed a backticked method name at somebody planning a holiday.

**7. Green tests are not evidence the feature is reachable.** Ledger 41, 50
and 59 are all the same shape: a string present in the source, a test grepping
for it, and a feature no user could get to. Assert the route a user travels.

**8. A guard goes where it cannot be forgotten.** Spending is counted in
`request_json` and in `Toolbox.__aenter__` — the two places everything paid
passes through — rather than in each of the seventeen callers that could reach
a provider. Rule 7's failure mode applies to guards too: one that has to be
remembered will be missing from exactly the case that needed it (ledger 61,
63).

## Running the checks

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest -q
```

1022 tests, no network, no API keys, a couple of minutes.

```bash
ruff check .
ruff format --check .
```

Live tests hit real providers and cost quota, so they are opt-in and need
`GOOGLE_MAPS_API_KEY`:

```bash
python -m pytest -m live --override-ini addopts=
```

### Before you open a pull request

- `python -m pytest -q` passes, and the count went **up**.
- If you fixed a defect: a ledger row in [INVARIANTS.md](INVARIANTS.md) naming
  how it was found and which test pins it.
- If you touched anything a user sees: say how you checked it in the browser,
  not just that the test greps for it.
- If you added a value to a closed vocabulary: say who consumes it.
- Commands you write in documentation work on Linux, macOS **and** Windows.
  CI runs on ubuntu and windows precisely so this stays true.

## Where things live

| Path | What is in it |
|---|---|
| `app/models/` | Pydantic models. The vocabularies and their docstrings are the specification |
| `app/services/` | Pure logic. No I/O, no model calls — this is where the arithmetic lives |
| `app/providers/` | The outside world, one class per external API, all replaceable |
| `app/agent/` | The loop, the tool registry, the context projection, the prompt |
| `app/api/` | HTTP. Thin: load, call a service, commit through the patch engine |
| `app/web/index.html` | The whole UI, one file, no CDN |
| `tests/scenarios/` | One file per milestone, mapping onto its acceptance criteria |
| `scripts/` | Runnable demonstrations. Four need no key at all |

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the longer version.

## Licence and contributions

This project is [Apache-2.0](LICENSE). Under section 5 of that licence, any
contribution you intentionally submit for inclusion is licensed under the same
terms, with no separate agreement to sign.

Please do not include code you are not entitled to license that way, and
please do not paste real personal data — trips, addresses, credentials — into
issues, tests or fixtures. The demo trip in `scripts/seed_demo_trip.py` is
fictional on purpose.
