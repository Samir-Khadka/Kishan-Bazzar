# Kisan Bazar — Weaknesses (a real audit, not a highlight reel)

Every sub-project so far has its own scattered "honest gaps" section.
This pulls them into one place, adds what wasn't said out loud before,
and ranks by what actually matters. Confirmed by grep against the real
files where a claim could be checked, not asserted from memory.

## Critical — the pieces were never connected (PARTLY FIXED; read exactly what that means)

**Fixed, and proven by a real test:** one complete vertical slice — a
farmer logging in and listing produce — now runs app → API → aggregation
engine. `kisan-bazar-jstests/test_integration.js` starts the real Flask
server, runs the app's own shipped login and listing code against it over
real HTTP, then queries the server independently to confirm what the app
believes happened actually did. 37 checks, including: the farmer ~50km
out of radius does not trigger a lot while the in-radius listing that
tips 130kg does, the app displays the lot the engine formed, a farmer
token cannot price a lot while a staff token can, every one of the app's
16 crops is accepted by the API (they now share one code vocabulary), and
the app degrades honestly to "local only" when the server is unreachable
or rejects its token. To check the test could fail, I deliberately broke
the app twice (dropped the Authorization header; mis-mapped a crop code)
and confirmed specific failures each time. CORS headers were also added
to the API — without them a browser would block every call from a page
served elsewhere — and checked at the header level.

**Still NOT fixed — do not read the above as "it's all connected":**

- **The published artifact is still a demo.** `BACKEND.enabled` is
  `false` by default, because nobody's browser can reach an API running
  inside this sandbox, and there is no deployed API anywhere. Everything
  above is verified against localhost inside the sandbox using Node's
  `fetch`, which does not enforce CORS and is not a browser. Going live
  needs: the API deployed at a real HTTPS URL, `KISAN_BAZAR_SECRET` and
  `KISAN_BAZAR_ALLOWED_ORIGINS` set, the app hosted somewhere that lets
  it call that URL (the sandboxed artifact viewer's own policy blocks
  arbitrary network calls, so likely separate hosting), `BACKEND.enabled`
  flipped, and then a test in a real browser — which I have not done and
  cannot do here.
- **Only listing is wired.** The app's land scan, buyer ordering, the
  co-op screens (still hardcoded demo lots) and the ledger do not call
  the API. The API endpoints for land scans exist and are tested; the app
  does not use them yet. The co-op view does not show the real lots the
  engine forms.
- **SMS/USSD is still its own island.** It uses `mock_backend.py`, not the
  API. Wiring it is not just plumbing: the API authenticates farmers by
  bearer token after an OTP, while an SMS gateway already knows the
  caller's number from the carrier. Doing this safely needs a deliberate
  design (a trusted gateway credential allowed to act for a verified
  caller number, with its own tight limits) — that is a security decision
  to make carefully, not to bolt on.
- **A 401 isn't handled gracefully yet.** An expired token currently
  just yields "local only" listings; the app should detect it, drop the
  token and prompt a fresh login.
- **Offers don't refresh from the server.** A farmer's earlier offers
  stay "waiting" locally even after another farmer's listing forms their
  lot; there is no "pull my listings" sync.

## Critical — no real farmer, co-op, or DHM contact was ever involved

Every design decision — the 130kg aggregation threshold, the Kavre
corridor choice, the 8% commission, the crop calendar's sowing windows,
which four land types cover Nepal's soil variety, which eight intents
the assistant handles — was made by me, reasoning from search results
and general knowledge, not validated against a real farmer, a real
co-op officer, or DHM. This is stated piecemeal throughout (the crop
calendar doc is explicit about this), but it's worth saying plainly as
one item: **nothing in this project has been checked against ground
truth.** A plausible-sounding aggregation threshold and a correct one
can look identical in a demo and behave completely differently against
a real Kavre harvest week.

## High — security has a real residual attack surface, even after hardening

The auth pass closed the biggest hole (no auth at all), but it did not
close everything, and shouldn't be read as "now secure":

- **`/v1/orders` still has zero authentication**, by design-gap, not
  oversight — buyers were never modeled as an identity class. Anyone can
  place an order as any `buyer_name` string right now.
- **Phone-number-as-identity has a known weakness the app can't
  engineer around**: SIM swap and SS7-level SMS interception are
  real-world attack vectors against any phone+OTP login, used by banks
  and everyone else despite this, but not solved by anything in this
  codebase. A stolen or swapped SIM impersonates that farmer's login
  completely.
- **Rate limiting is in-memory and per-process** (`auth._attempts` is a
  plain dict). It resets on every server restart and doesn't work at
  all across multiple server instances — a real deployment needs
  Redis-backed limiting, not this.
- **Tokens can't be revoked before their 24-hour expiry.** A stolen
  token is valid for up to a full day with no way to kill it early —
  no logout endpoint, no blacklist.
- **The Postgres row-level-security migration was never run.** Same
  standing caveat as the rest of that migration set: reviewed by
  design, not verified by execution. It could contain a syntax error I
  can't catch without a live Postgres instance.
- **No monitoring or alerting exists for any of this** — repeated
  failed logins, an unusual order size, a lot priced far outside normal
  range: nothing watches for any of it.

## High — the AI Sahayak is much narrower than "AI bot" implies

Confirmed: exactly 8 intents (weather, price, sell, plant, ledger, land
scan, greeting, help), keyword-matched. Any real farmer question outside
that set — "my tomato leaves are curling," "should I sell now or wait,"
anything about pests, disease, a specific problem — gets the generic
fallback every time. This was disclosed clearly when I built it, but
worth restating here as a limitation, not just a design note: the actual
open-ended-question capability the "AI bot" framing suggests does not
exist yet. It's documented as a future server-side LLM integration,
never built.

## High — the crop calendar and weather data are not real

Two separate issues, both already flagged individually, worth pairing
here because together they mean **the app's single highest-stakes
content — what to plant, and whether a storm is coming — is entirely
placeholder data**:

- `WEATHER` in the app is fully fictional. There is no DHM connection,
  no Open-Meteo connection, nothing live. Every forecast shown is
  hardcoded demo text.
- The crop calendar's sowing-month data is my synthesis of general
  agronomic patterns, explicitly not a transcription of a verified
  NARC/DoA source (their real tables are images inside PDFs I couldn't
  extract). If this shipped as-is, a farmer could genuinely be given a
  wrong planting window.

## Medium — the backend has real scale limits, not just missing features

- **SQLite has a single-writer lock.** Fine for this demo's traffic;
  wrong for real concurrent use across many farmers and co-ops writing
  at once. The Postgres migration exists for this reason but is
  unverified (see above).
- **No pagination anywhere.** `/v1/lots`, `/v1/listings/mine`, both
  return everything, unbounded. Fine at 5 seed farmers; not fine at
  5,000 real ones.
- **No idempotency keys.** A farmer's phone losing signal mid-request
  and the app retrying could double-create a listing or an order —
  confirmed there's no idempotency handling anywhere in `app.py`.
- **`DEFAULT_TARGET_KG = 130` is still hardcoded**, exactly as flagged
  when this was built — never wired to a co-op's actual configured
  target or a standing buyer commitment.
- **Aggregation runs synchronously in-request**, also flagged at build
  time, never revisited — a slow aggregation pass blocks the farmer's
  listing call instead of being queued.

## Medium — testing has a shape problem, not just a coverage problem

Every test suite in this project (SMS, API, auth, the app's JS harness)
is strong *within its own component* — genuinely, these caught real
bugs before shipping. But because the components were never connected
(see the first item), there is **no integration test anywhere in this
project**. Every green checkmark proves a piece is internally correct,
none of them prove the pieces work together, because they never run
together. There's also no visual/rendering test of the HTML app (I
cannot screenshot in this environment, and said so each time), no
accessibility audit with a real tool (only reasoned-through contrast
and touch-target sizing), and no load testing of any kind.

## Medium — language and regional coverage is narrower than the original brief

The very first message in this project named Maithili, Bhojpuri, Awadhi,
and Tharu as necessary for real Terai reach. Every subsequent build —
the app, the SMS/USSD system, the assistant — ended up Nepali/English
only. This wasn't a deliberate later decision to cut scope; it just
quietly didn't happen, which is worth naming rather than leaving buried.

## Low, but worth listing rather than omitting

- Ward boundaries in the Postgres seed data are centroids, not real
  polygons — flagged when built, means ward-based alert fan-out would
  be inaccurate at ward edges if this went live as-is.
- No migration tool (Alembic/Flyway) for the SQL — plain numbered files,
  no rollback story, error-prone by hand at real scale.
- The land-scan photo upload has no client-side file-size or type cap
  before it's read into a canvas — a very large image would work but
  waste time/memory on a low-end phone; never tested against that case.
- No backup/disaster-recovery plan for either the SQLite file or a
  future Postgres instance — not designed at all, not just unfinished.
- Every seed phone number, co-op name, and the Kavre/Dhading corridor
  choice is illustrative, not a real, contacted cooperative — this
  connects back to the first Critical item above.

## What this list is for

Not to undercut the parts that do work — the aggregation math is
correct and tested, the auth model closes a real hole and is tested
against actual attack attempts, the land-scan classifier was debugged
twice against real test failures, not just written once and trusted.
The point of this document is that a system can be genuinely
well-built in its parts and still not be a working whole, and the
honest next step isn't more features — it's connecting what already
exists, or being explicit that this stays a components library until
someone does.
