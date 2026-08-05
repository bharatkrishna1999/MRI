# Merchant Risk Intelligence

Domain-first underwriting for a merchant of record. Paste a domain, get a
reserve percentage and a payout delay.

```
pip install -r requirements.txt
python app.py            # http://localhost:8000
```

---

## What it does

One input: a domain. `example.com` and `https://www.example.com/pricing` both
work; input is normalised to the registrable domain with `tldextract` against a
bundled public-suffix snapshot, so the form never waits on a network call to
validate itself. Everything else — declared legal name, category, country,
contact email — lives in a collapsed **Advanced** panel, is entirely optional,
and comes back prefilled with whatever enrichment actually found.

The engine then, inside an 8 second budget:

1. reads the RDAP registration record,
2. resolves the A record and places it in a country with a **local** MaxMind
   GeoLite2 lookup,
3. fetches the root, extracts every anchor, and follows up to **15 internal
   links at depth 1** in a thread pool with a 3 second timeout each,
4. scores 24 signals across 7 categories worth 100 weight points,
5. returns a decision, a reserve percentage, a payout delay and machine reason
   codes.

None of that happens behind a progress bar. Every outbound call, every page the
crawler fetches and every signal it scores is printed to a live console as it
happens — see **Watching a run** below. The pages are built for a phone first:
one column, tap targets that clear 44px, and wide tables that scroll inside
their own container rather than widening the page.

## The decision, not the score

A number out of 100 is not a decision. These are:

| Score | Decision | Reserve | Payout |
|---|---|---|---|
| 80–100 | Auto approve | 0% | T+7 |
| 60–79 | Approve with reserve | 5% held 90 days | T+14 |
| 40–59 | Manual review — incorporation doc, ID, bank proof | onboarding held | none |
| 0–39 | Decline with reason codes | n/a | n/a |

Some findings override the band the score would have bought. A Safe Browsing
hit, a confirmed restricted category, a parked domain or an unreachable site
declines regardless of how clean everything else is. A missing refund policy
alone caps the outcome at *approve with reserve* — it can never auto-approve.

A restricted category is the only finding here that declines on the engine's own
reading of a page, so it has to earn it. The inference runs on the pages that
describe the offering, never on terms and privacy boilerplate — a merchant's
acceptable-use page names every vertical it *refuses*, and reading that as a
description of the merchant declines the applicants with the best compliance
hygiene. Sentences that prohibit a vertical are dropped before matching, and a
restricted reading that rests on one keyword, or that only ties with an ordinary
reading of the same page, caps at *manual review* instead of declining: a person
reads the storefront and makes the call.

## The signal set

| Category | Weight | Signals |
|---|---:|---|
| Domain identity | 20 | age from RDAP creation (8), registration term (4), privacy proxy (4), TLD abuse tier (4) |
| Site liveness | 20 | content depth (6), HTTP root (4), TLS validity + issuer + days to expiry (4), parked-page check (4), TTFB (2) |
| Commercial legitimacy | 20 | **refund or cancellation policy (8)**, terms (3), privacy (3), contact (3), pricing (3) |
| Payment surface | 15 | incumbent processor in page source (7), highest listed price (4), recurring billing (4) |
| Category risk | 15 | inferred category tier (9), declared vs inferred mismatch (3), restricted keyword scan (3) |
| Reputation | 5 | Google Safe Browsing v4, Spamhaus DBL fallback (5) |
| Consistency | 5 | country vs GeoIP vs ccTLD (2), legal name vs footer (2), email domain vs site (1) |

Every signal emits its raw value, a 0–100 normalisation, its weight, its
contribution and one sentence of plain English.

Two rules the engine will not bend:

- **Refund policy is weighted hardest in its category** — 8 of the 20 points,
  twice any other page. Its absence is the strongest single predictor of
  chargeback volume: a customer who cannot find how to get their money back from
  the merchant asks their bank instead, and that arrives as a dispute.
- **Nothing is ever scored off the merchant's own dropdown.** The declared
  category is used for exactly one thing — comparing it against the category
  inferred from the site's own text to raise `CATEGORY_MISMATCH`. Declaring a
  safer tier than the site operates in is the clearest fraud signal in the
  system. If nothing is declared, the mismatch signal abstains; it never becomes
  free points.

## Failure behaviour

Global budget 8 seconds, 3 seconds per call. A signal that cannot be computed
returns **unavailable** and is removed from the weighted denominator. It never
falls back to zero, because a zero is indistinguishable from a real failing
signal and would manufacture a false decline out of somebody else's outage.

Confidence is the share of the 100 policy weight points that could be computed.
Below 60% the run is capped at manual review and carries `LOW_CONFIDENCE`.
Every decision is cached for 24 hours, keyed by domain *and* policy version.

## Watching a run

Underwriting is a judgement about somebody's business, so the engine says what
it did rather than asking to be trusted. Every run records an ordered trace —
each HTTP request with its status, size and time to first byte, each redirect
hop, the DNS answers, the TLS handshake and issuer, the RDAP parse, each page
the crawler fetched and what class it was, the taxonomy inference with its
lexicon hits, then all 24 signals with their arithmetic, the weighted score,
any policy override and the audit write.

The UI streams it over server-sent events and renders it as a terminal:

```
GET /api/v1/evaluate/stream?domain=example.com     text/event-stream
```

```
curl -N 'http://localhost:8000/api/v1/evaluate/stream?domain=stripe.com'
```

Event names are `start`, `trace`, `result`, `failed`, `done` — deliberately not
`open` or `error`, which `EventSource` already dispatches for transport state.
Work that takes measurable time emits a `running` event first and a terminal
event carrying the same `id` after, so a consumer completes the line in place
instead of printing it twice.

The same list ships as `trace` on the ordinary JSON response and is stored with
the audit record, so a decision replayed months later still carries the calls
that produced it. A cache hit is honest about being one: its own trace is two
lines, and the run that actually made the calls is kept alongside as
`trace_of_cached_run`. Tick **bypass the 24h cache** on the form to watch the
whole thing happen live.

## Audit trail and API

Every run is persisted to SQLite — inputs, every raw signal value, the weights
version, the score, the timestamp — and is replayable at
`/api/v1/audit/{id}`. The policy version is stamped on the result page and in
every response.

```
GET  /api/v1/evaluate?domain=example.com      full structured decision
POST /api/v1/evaluate                         {"domain": "...", "advanced": {...}}
GET  /api/v1/evaluate/stream?domain=...       the same run, narrated over SSE
GET  /api/v1/policy                           weights, bands, reason codes, taxonomy
GET  /api/v1/audit/recent                     last N decisions
GET  /api/v1/audit/{id}                       replay one decision
GET  /api/v1/benchmark/results                metrics from the last benchmark run
GET  /api/v1/health                           GeoLite2 build date, store stats
```

The working curl is printed on every result page.

## Benchmark

`/benchmark` renders a confusion matrix, precision, recall, F1 and false
positive rate over 60 hand-labelled domains, with a threshold slider that
recomputes everything live and overlapping score histograms for the good and bad
classes. **Positive class is "bad"**: precision is how often a decline was
correct, recall is how much of the bad set was caught, false positive rate is
how many good merchants were turned away.

Labels are committed at `data/benchmark_labels.csv` — 30 legitimate digital
merchants from Product Hunt and Indie Hackers, 30 that should not board.

```
python -m mri.benchmark              # runs the engine over all 60, writes results
python tools/refresh_labels.py verify        # re-check every label against the engine
python tools/refresh_labels.py urlhaus --count 10 --write   # append fresh URLhaus hosts
```

Labels rot. A domain labelled "parked" today can be a real store next quarter,
so `verify` exists and should be run before quoting the numbers.

## Cost

Nothing here costs money.

- **RDAP** — free, no key.
- **MaxMind GeoLite2 Country** — the `.mmdb` is committed to `data/` and read
  locally through `geoip2`. No key, no rate limit, no call to maxmind.com. See
  `NOTICE` for attribution.
- **Google Safe Browsing v4** — free, but it does need an API key. Set
  `SAFE_BROWSING_API_KEY` if you have one. Without it the engine falls back to a
  keyless **Spamhaus DBL** DNS lookup, and if that is also unreachable the
  reputation signal drops out of the denominator like any other failure.

Nominatim was removed. Their usage policy caps you at one request per second and
they block; it would have failed live.

The only credential the app reads is `SAFE_BROWSING_API_KEY`, and it is optional.
Every other outbound call — RDAP, the DBL lookup, the merchant's own site — needs
no account. Confirm what a running instance is actually using at
`/api/v1/policy` (`safe_browsing_key_configured`) and `/api/v1/health`.

## Running on a free hosting tier

`render.yaml` is set to the free plan. Two things follow from that, and the app
is built to survive both:

**The instance sleeps when idle.** A cold start takes about a minute, during
which the first request hangs. The demo pre-warm runs automatically on boot and
takes roughly twenty seconds after that, so the three buttons are cached before
anyone clicks. Practically: open the page a couple of minutes before you need to
show it, and it is warm.

**There is no persistent disk.** SQLite lives on the container filesystem, so
the audit trail and the 24 hour cache reset on every restart and redeploy. The
decisions themselves are unaffected — the store is rebuilt empty and the demos
re-warm — but do not expect last week's runs to still be there.

That last point is why benchmark results are read from SQLite *first and then
from `data/benchmark_results.json`*: a run triggered on the deployed instance is
gone after the next restart, but a results file committed to the repository
survives. Run the benchmark, commit the JSON, and `/benchmark` renders instantly
on every future boot with no network calls at all.

```
python -m mri.benchmark
git add data/benchmark_results.json && git commit -m "Benchmark run under policy_v1.1"
```

## Tests

```
python -m unittest discover -s tests -t .
```

The suite runs the real parser, crawler, signal code, scorer and decision logic
against fixture sites — only the four network entry points are swapped — so a
failure means behaviour changed, not that a mock drifted.

## Layout

```
app.py                 entry point (uvicorn app:app)
mri/policy.py          versioned weights, bands, reason codes — the whole policy
mri/engine.py          orchestration, deadlines, confidence
mri/decision.py        score -> reserve percentage and payout delay
mri/crawl.py           anchor extraction and depth-1 link discovery
mri/trace.py           the run trace behind the live console and the audit record
mri/signals/           the seven categories
mri/benchmark.py       labelled-set harness and metrics
mri/store.py           SQLite audit trail and 24h cache
mri/ui/                the two pages
data/                  GeoLite2 database, labels CSV, benchmark results
```
