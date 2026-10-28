# Kisan Bazar — API (runnable prototype)

A real, working implementation of the aggregation engine and market
endpoints from the backend architecture doc, against Flask + SQLite
(both already available in a plain Python env — no install step needed).
This is a faithful simplification of the PostgreSQL/PostGIS schema in
`../kisan-bazar-db/`, not a different design: same entities, same
aggregation logic, plain lat/lon + haversine distance instead of PostGIS
geography types.

**Verified running in this sandbox**, both via Flask's test client and
as an actual HTTP server on `localhost:5050` (`curl` example below).

## Run it

```bash
python3 seed.py                  # creates kisan_bazar.db, seeds the Kavre corridor
python3 tests/test_auth.py       # login flow, rate limiting, forged tokens, impersonation
python3 tests/test_api.py        # aggregation-engine scenario, now auth-gated
python3 tests/test_land_scan.py  # land-scan persistence + cross-farmer access checks
```

Or as a live server:

```bash
python3 app.py             # listens on :5050, debug mode OFF by default

# 1. request a login code (in production this is sent via SMS — see
#    ../kisan-bazar-sms/ for the gateway this would call)
curl -X POST localhost:5050/v1/auth/request-code \
  -H "Content-Type: application/json" -d '{"phone":"9841000001"}'
# {"message":"if this number is registered, a code has been sent"}
# — note the code is NEVER in this response; check server logs or, for
#   this prototype, auth._dev_outbox if you're driving it from Python

# 2. verify the code to get a bearer token
curl -X POST localhost:5050/v1/auth/verify \
  -H "Content-Type: application/json" -d '{"phone":"9841000001","code":"123456"}'
# {"token":"eyJwaG9uZSI6Ijk4NDEwMDAwMDEi...","expires_in_seconds":86400}

# 3. use the token for anything that touches that farmer's data
curl -X POST localhost:5050/v1/listings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token from step 2>" \
  -d '{"farmer_phone":"9841000001","coop_id":"kavre-veg","crop_code":"KAULI","grade":"A","quantity_kg":38}'

curl -X POST localhost:5050/v1/farmers/9841000001/land-scan \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token from step 2>" \
  -d '{"land_type":"khet","confidence":"good","color_key":"dark","texture":"clay","drainage":"khet","had_photo":true}'
```

## Security model (see ../kisan-bazar-security-notes.md for the full picture)

Every mutating endpoint requires a bearer token, obtained via a two-step
phone + one-time-code login (`auth.py`). Three authorization rules are
enforced and **tested against attempts to break them**, not just assumed:

- A farmer's token only works for that farmer's own phone number —
  farmer A cannot list produce, read, or write a land scan as farmer B,
  even with a perfectly valid token of their own (`tests/test_auth.py`
  #11, `tests/test_land_scan.py` #11-12).
- Lot confirmation (setting a price) requires a **staff** account —
  a regular farmer's token gets 403, checked *before* the lot lookup
  even happens, so a non-staff caller can't use the error response to
  probe which lot IDs exist (`tests/test_auth.py` #12).
- Codes and tokens are rate-limited per phone number — `tests/test_auth.py`
  actually fires repeated requests and asserts a 429 shows up, rather
  than trusting the limiter exists just because the code is there.

**Design note on the land-scan endpoint:** it never receives or stores the
photo itself — only the derived signals (colour bucket, texture answer,
drainage answer) and the resulting land type. The classification (the
HSL-based colour analysis and the land-type combination logic) happens
client-side in the app prototype, tested in isolation there before this
endpoint existed; this API's job is only to persist the *result*, not to
redo the analysis. That keeps this endpoint free of image data, storage
cost, and the privacy questions that come with holding photos of
someone's land.

## What the test actually proves

The end-to-end test isn't just "the server responds" — it exercises the
specific logic that makes this product work rather than fail the way
earlier Nepal marketplace apps did:

1. **A single farmer's listing never triggers a lot** — 38kg alone
   against a 130kg target correctly stays `open`.
2. **A farmer 50km outside the co-op's 18km collection radius is
   excluded from the pool**, even though their 100kg would easily push
   the total over threshold. This is the one that matters most: without
   it, the aggregation engine would happily group farmers who can't
   actually be reached by the same collection route, which is exactly
   the kind of bug that looks fine in a demo and fails on the first
   real collection day.
3. **Partial contribution is handled correctly** — the farmer whose
   listing tips the total past threshold only has part of their
   quantity pulled into the lot (40 of their 44kg), and their listing
   status becomes `partially_pooled`, not `pooled`, leaving the
   remainder available for the next lot.
4. **State transitions are guarded** — ordering more than a lot's
   pooled quantity fails with 400; confirming an already-`ready` lot a
   second time fails with 409, so a co-op staffer can't accidentally
   double-price the same lot.

`tests/test_land_scan.py` covers the newer land-scan endpoints
(`POST/GET /v1/farmers/<phone>/land-scan`, plus `/history`) — persisting
the app's photo+quiz result, a farmer's `land_type` updating on rescan
while the prior scan stays in history rather than being overwritten, the
no-photo path (color_key correctly null, had_photo correctly false), and
that bad input (unknown land_type, unknown farmer, missing fields) fails
with a clean 400/404 instead of a 500.

## Files

```
db.py               SQLite schema (simplified PostGIS -> lat/lon)
aggregation.py       The core threshold/pooling logic — read this first
app.py                Flask routes — aggregation endpoints + land-scan endpoints
seed.py                Seed data: same Kavre corridor as the other artifacts
tests/test_api.py         End-to-end aggregation scenario with assertions
tests/test_land_scan.py   End-to-end land-scan persistence scenario
```

## Honest gaps before this is more than a prototype

- **Synchronous aggregation check.** `check_and_form_lot` runs inline
  inside the `POST /v1/listings` request. Fine for a demo; production
  should enqueue it (the architecture doc's event-trigger design) so a
  slow aggregation pass never blocks a farmer's listing from saving.
- **No auth.** Any caller can list produce as any farmer_phone, confirm
  any lot, place any order. The real API needs phone-verified auth
  (OTP, matching how `users.phone` works as the login identity in the
  Postgres schema) before this touches real data.
- **Fixed 130kg target.** Production should read the co-op's configured
  target or an active standing buyer commitment, not a hardcoded
  constant — this was simplified specifically to keep the aggregation
  logic legible for this prototype.
- **SQLite, not Postgres.** This proves the *logic* is correct; it does
  not prove the real Postgres/PostGIS migrations in `../kisan-bazar-db/`
  work, since I still haven't been able to run those against a live
  Postgres instance in this network-disabled sandbox. Porting this
  logic onto that schema (swapping haversine_km's Python math for
  PostGIS's ST_DWithin) is the next real step, not a formality.


## Calling this from a browser (CORS)

A page served from a different origin than this API can only call it if
the API sends CORS headers. `app.py` does this from an allowlist, not a
wildcard:

```bash
export KISAN_BAZAR_SECRET="<long random string>"          # token signing key
export KISAN_BAZAR_ALLOWED_ORIGINS="https://your-app.example"  # comma-separated
```

Unset, the allowlist defaults to localhost only, so a forgotten variable
fails closed. `tests/run_test_server.py` runs the same app with the mock
SMS outbox mirrored to a file, for the integration test in
`../kisan-bazar-jstests/test_integration.js`.

Seed data now includes all 16 crops the app offers, using the same short
codes as the app and the SMS/USSD system.

## Real SMS via the Messaging API (optional)

By default OTP codes go to the in-memory dev outbox (mock SMS) and the
public demo reads them via `/v1/dev/latest-code` in demo mode. To send
real SMS instead, deploy the separate Messaging API service
(https://github.com/Samir-Khadka/messaging-api) with a real provider
(Twilio or Meta), then set on this service:

```bash
export KISAN_MESSAGING_API_URL="https://your-messaging-api.example"
export KISAN_MESSAGING_API_KEY="your-messaging-api-key"
export KISAN_MESSAGING_CHANNEL="sms"   # optional, default sms
```

With those set, `/v1/auth/request-code` sends the locally minted code
through the Messaging API; verification, tokens, and rate limits stay
exactly as before. If the gateway call fails, request-code answers 502
instead of claiming a code was sent. With them unset, behaviour is
unchanged — the integration is purely opt-in.
