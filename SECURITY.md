# Security

## The most important thing on this page

**This application has no authentication, and that is a design choice rather
than an omission.** There is no login, no session, no per-user isolation:
anyone who can reach the port can list, read, edit and delete every trip in
the database.

It is a personal tool. **Run it on localhost. Do not expose it to the
internet** — not behind a "nobody knows the URL", not on a home router with a
port forwarded, not on a cloud box with an open security group.

If you want more than one person to use it, that is a real feature and not a
configuration flag: it needs an identity to hang trips off, a filter on the
trip list, and the per-trip run mutex moved out of process memory. Until then,
one person, one machine.

## What is protected, and what is not

| | Status |
|---|---|
| Credentials in source or git history | **None.** No key, `.env` or database file has ever been committed; `.gitignore` covers all three |
| Secrets in the served page | **None.** The browser fetches its map key from `/ui-config` at runtime; a test asserts no `AIza…` string is in the HTML |
| Ticketing / payment paths | **None exist.** Flight and hotel providers are read-only by construction, and a test asserts the Duffel module contains nothing named for orders, payment, seats or cancellation |
| Writes to trip state | Always through the validated patch engine: revision-checked, lock-enforced, all-or-nothing |
| Authentication / authorisation | **Absent by design.** See above |
| Rate limiting | Absent. There is no limit on how many turns may be started, only on what each one costs — see below |
| Runaway spend inside a turn | Bounded on four axes. See below |
| Transport | Plain HTTP on localhost. No TLS termination is provided |

## What one turn can spend

You supply the keys, so it is worth knowing exactly what stands between a
confused model and your bill. Every limit here is a setting you can change in
`.env`; the defaults are set above the most expensive turn ever measured
against this codebase, so they bound a runaway without cutting off real work.

| Limit | Default | What it bounds |
|---|---|---|
| `AGENT_MAX_ITERATIONS` | 16 | Model rounds per turn. Reaching it ends the turn *and says so* — a truncated turn never reads like a finished one |
| `AGENT_MAX_TURN_TOKENS` | 500,000 | Tokens for the whole turn, input plus output. The most expensive legitimate turn measured here used 280,311 |
| `TURN_GOOGLE_CALL_BUDGET` etc. | 120 / 20 / 20 | Paid provider calls per turn, counted per provider. Cache hits are free and do not count |
| `OPENAI_MAX_OUTPUT_TOKENS` | 32,000 | One model reply, **reasoning tokens included**. Measured worst case on this workload is 712, so this only stops a reply running away; a reply stopped by it is marked as cut off rather than handed back as an answer |
| `OPENAI_REQUEST_TIMEOUT_SECONDS` | 120 | One model call. The SDK's own default is 600s with two silent retries |

The provider ceilings are enforced in `request_json`, the one function every
paid HTTP provider goes through, so a tool cannot escape them by forgetting to
ask. A `Toolbox` opened outside an agent turn — the button endpoints do this —
starts its own budget, so a click is metered too. The count is of calls that
reach the network: anything answered from the entity registry or the cache
costs nothing and is charged nothing.

For scale, measured against the live providers on a four-day trip with ten
places and thirteen stops:

| what | Google calls |
|---|---|
| One fresh Places text search | 1 |
| Opening hours for all ten places | 10 |
| Every route matrix behind a full stress test | 2 |
| Any of the above a second time | 0 (cache) |

So a heavy turn on that trip costs around 20 against a ceiling of 120. If you
ever see a turn running near its limit, the number is wrong — not the guard
working.

**What is not bounded:** anything *across* turns. Nothing stops twenty turns
being started in a row, and there is no daily ceiling — the caps are per turn,
and a turn is something a person asks for. On localhost, that person is you.

## Handling API keys

All credentials live in `.env`, which is git-ignored. Copy `.env.example` and
fill in only what you need — every provider degrades honestly when its key is
missing rather than inventing data.

**Use two Google keys the moment anyone but you can reach the app.** A web page
publishes whatever key it loads, so a single shared key hands out your Places
and Routes budget along with the map:

- `GOOGLE_MAPS_API_KEY` — the **server** key. Unrestricted by HTTP referrer,
  because the server's own calls have no referrer. Restrict it to Places API
  (New), Routes API and Weather API.
- `MAPS_BROWSER_API_KEY` — the **browser** key. Restrict it by HTTP referrer
  *and* to Maps JavaScript API only.

With only a server key configured, the map goes dark on purpose rather than
publishing that key — a smaller loss than a scraped quota. The UI says so
under the map when the two are shared.

## Cached provider data

Caching is constrained by content class, not by endpoint, so that the Google
Maps Platform terms are respected by construction
(`app/services/cache.py`):

- **Place ids** — may persist indefinitely.
- **Coordinates** — may persist for at most 30 days, and expiry is a delete
  obligation the cache actually performs, not merely a read-time filter.
- **Everything else** (names, ratings, opening hours, route durations) —
  in-process memory only, one hour maximum, gone when the process exits. The
  durable layer *raises* rather than quietly accepting it.

Trip data you enter is yours and stays in your database. Nothing is sent
anywhere except to the providers whose keys you supplied, in order to answer
the question you asked.

## Reporting a vulnerability

Open a [GitHub issue](https://github.com/wanghaoyu0408/travel-agent/issues) for
anything that is not itself sensitive to disclose.

For something that should not be public first, email
**haoyu.uestc@gmail.com** with `SECURITY` in the subject. This is a personal
project maintained in spare time — expect a reply in days rather than hours,
and there is no bounty.

Please do not report the missing authentication as a vulnerability. It is
documented above, on purpose, at the top.
