# Kishan-Bazzar Backend Code Review — 2026-10-19

Scope: `kisan-bazar-api/` — app.py (1269 lines), db.py (239), auth.py (159),
assistant.py (623), seed.py (92), ai_advisor.py (212), aggregation.py (113).

Method: full read of all 7 files, route-table introspection via Flask's
`url_map`, and a live run of `tests/test_auth.py` against the current code.

**Headline: 2 critical, 6 high, 15 medium, 7 low issues, plus architecture notes.**
The most urgent: **OTP login is completely broken** (critical #1) — the existing
test suite fails on it. Fix that first, then the oversell races.

---

## CRITICAL

### C-1. `POST /v1/auth/verify` is routed to `admin_login` — OTP login dead
- **File:** `app.py`, lines 387–391 and 421
- **Severity:** critical
- **Description:** The route decorators are stacked:
  ```python
  @app.post("/v1/auth/verify")      # line 387


  @app.post("/v1/auth/admin")        # line 390
  def admin_login():                 # line 391
  ```
  Both decorators apply to `admin_login`. The real `def verify():` at line 421
  is a plain module-level function — never registered as a route. Verified via
  `url_map`: `/v1/auth/verify -> admin_login`. A valid phone+code POST now runs
  the admin password check and returns 401 `invalid credentials`.
  Introduced in commit `4936523` ("Admin password login"). **No farmer or buyer
  can log in via OTP right now — the entire login flow is broken on live.**
- **Proof:** `python3 tests/test_auth.py` fails at step 1:
  `AssertionError: {'error': 'invalid credentials'}` on the verify call.
- **Fix:** Move `@app.post("/v1/auth/verify")` to directly above `def verify():`
  (line 421) and delete the orphan decorator at line 387. Add a regression test
  asserting `url_map` binds `/v1/auth/verify` to the `verify` view function.

### C-2. Startup migration can crash the app on any non-demo database
- **File:** `app.py`, `_ensure_db_ready()`, ~lines 100–115
- **Severity:** critical
- **Description:** The lots migration hardcodes `coop_id='kavre-veg'` in the
  INSERT. It runs whenever co-ops exist but `lots` is empty. If a database has
  any other co-op (but not `kavre-veg`) and zero lots, the INSERT violates the
  foreign key (`PRAGMA foreign_keys = ON` in `db.get_db()`) and raises inside
  `_ensure_db_ready()`, which runs at import time — **the app fails to start**.
- **Fix:** Guard with `INSERT OR IGNORE`, or check the coop exists first:
  `SELECT id FROM cooperatives WHERE id='kavre-veg'` before inserting; skip the
  migration (with a log line) when it doesn't.

---

## HIGH

### H-1. `place_order` oversell race (TOCTOU)
- **File:** `app.py`, `place_order()`, ~lines 755–780
- **Severity:** high
- **Description:** The oversell guard is check-then-insert:
  `SELECT COALESCE(SUM(quantity_kg)...)` → compare → `INSERT`. Under SQLite's
  default DEFERRED isolation, two concurrent requests with different
  idempotency keys can both read the same SUM before either INSERTs, then both
  commit — selling the same kilos twice. The idempotency decorator does not help
  (different keys). The 30s busy timeout only serializes the writes, not the
  read-modify-write sequence.
- **Fix:** Wrap the check+insert in `BEGIN IMMEDIATE` (acquire the write lock
  before the SELECT), or do the guard atomically, e.g. a conditional INSERT
  using a single statement, or `UPDATE lots SET pooled_kg = pooled_kg - ? WHERE
  id=? AND pooled_kg >= ?` style reservation.

### H-2. `decide_offer` accept path has NO oversell/availability check
- **File:** `app.py`, `decide_offer()`, ~lines 880–900
- **Severity:** high
- **Description:** Accepting an offer converts it straight to an order
  (`INSERT INTO orders ...`) without checking lot status or remaining quantity.
  `place_order` has the guard; this path bypasses it entirely. A staff member
  accepting several offers (or an offer larger than the lot) oversells silently.
- **Fix:** Reuse the same availability check as `place_order` before inserting;
  return 409/400 when the lot can't cover the offer quantity. Also verify the
  lot is still `ready`.

### H-3. `check_and_form_lot` race: duplicate lot IDs and double-commit
- **File:** `aggregation.py`, `check_and_form_lot()`, lines 47–113
- **Severity:** high
- **Description:** Two concurrent `POST /v1/listings` calls that both cross the
  threshold will both run the aggregation. `_next_lot_code()` derives the ID
  from `COUNT(*)`, so both compute the same `KVR-1NN` → the second INSERT fails
  on the PK (500 to the farmer, after their listing was already committed), or
  worse, both read listings as `open` before either commits and both commit
  overlapping `lot_listings` rows. Called synchronously inside `create_listing`
  with no transaction boundary.
- **Fix:** `BEGIN IMMEDIATE` at the top of `check_and_form_lot`; generate the
  lot ID from a sequence/UUID instead of `COUNT(*)` (also fixes collisions if a
  lot row is ever deleted).

### H-4. `GET /v1/offers` is public — buyer phone numbers enumerable
- **File:** `app.py`, `list_offers()`, lines 827–842
- **Severity:** high
- **Description:** No `@require_auth` on the route. Anyone can list every offer
  ever made, including `buyer_phone` and `buyer_name`, filterable by `lot_id`.
  No pagination either, so it's a full table dump. Buyer PII leak.
- **Fix:** Require auth; scope the query — buyers see only their own offers
  (`buyer_phone = g.phone`-equivalent), staff see all. Add pagination like
  `/v1/lots`.

### H-5. Assistant ledger query references a non-existent column — always Rs 0
- **File:** `assistant.py`, `_ledger()`
- **Severity:** high
- **Description:** The query joins `orders o JOIN listings l ON o.listing_id=l.id`,
  but the `orders` table has **no `listing_id` column** (only the `lot_listings`
  junction table does). Every call raises `OperationalError`, which the bare
  `except Exception: pass` swallows → `due = 0` → the farmer is always told
  "no pending payments". A silently wrong money answer is worse than an error.
- **Fix:** Rewrite against the real schema (orders are per-lot, not per-listing;
  the farmer's share needs `lot_listings` → listings attribution), and remove
  the blanket `except: pass` — log the error and say "couldn't load right now".

### H-6. `book_rental` allows double-booking; `book_date` unvalidated
- **File:** `app.py`, `book_rental()`, lines 1063–1095
- **Severity:** high
- **Description:** No check for overlapping bookings on the same machine — two
  farmers can book the same tiller for the same dates. `book_date` is only
  checked non-empty; `"not-a-date"` is accepted and stored. `days` is an int
  but never checked against the machine's availability window.
- **Fix:** Validate `book_date` as a real date (not in the past); add an overlap
  query against `rental_bookings` for the same `rental_id` and reject conflicts
  with 409.

---

## MEDIUM

### M-1. Idempotency keys not scoped per user — cross-user replay
- **File:** `app.py`, `idempotent()`, ~lines 215–300
- **Severity:** medium
- **Description:** Replay lookup is `(key, endpoint)` only. If two different
  farmers use the same `Idempotency-Key` (UUID collision is unlikely, but a
  buggy client reusing a constant, or two buyers on `/v1/orders` which has no
  auth), the second gets the first user's response replayed — including another
  farmer's `listing_id`.
- **Fix:** Include the authenticated identity in the lookup: `(key, endpoint,
  g.phone or 'anon')`. For the unauthenticated `/v1/orders`, key on
  `(key, endpoint, buyer_phone)`.

### M-2. `GET /v1/crop-doctor/history` has no auth — anyone's history readable
- **File:** `app.py`, lines 997–1010
- **Severity:** medium
- **Description:** `farmer_phone` comes from an unauthenticated query param.
  Enumerate phone numbers → read everyone's diagnosis history. Lower sensitivity
  than offers, but still PII-adjacent health-of-crop data tied to identity.
- **Fix:** Add `@require_auth` + `@require_self`-style check so a farmer reads
  only their own history (or staff reads any).

### M-3. `list_lots` pagination bypass via negative limit
- **File:** `app.py`, ~lines 585–595
- **Severity:** medium
- **Description:** `limit = min(int(...), 200)` with no lower clamp. SQLite treats
  `LIMIT -1` as "no limit", so `?limit=-1` dumps the whole table, defeating the
  pagination added for scalability.
- **Fix:** `limit = max(1, min(int(...), 200))`.

### M-4. Intent matching uses raw substring — systematic misclassification
- **File:** `assistant.py`, `INTENTS` + `classify_intent()`
- **Severity:** medium
- **Description:** `if pat.lower() in lower` with no word boundaries. `"hi"` (w=1)
  fires inside "this/while/high"; `"mal"` (fertilizer, w=2) fires inside
  "normal/small"; `"dam"` inside "damage"; `"list"` inside "listen". Short
  patterns routinely outscore the real intent on ordinary sentences.
- **Fix:** Match on word boundaries (`re.search(r"\b"+re.escape(pat)+r"\b", ...)`)
  for short patterns (len ≤ 3), or tokenize first and score per token.

### M-5. `_get_price` fallback queries a column that doesn't exist
- **File:** `assistant.py`, `_get_price()`
- **Severity:** medium
- **Description:** Falls back to
  `SELECT rate_per_kg FROM listings ...`, but `listings` has no `rate_per_kg`
  column → always raises → swallowed by `except: pass`. Dead fallback that
  masks the real gap (no listing-level pricing exists by design).
- **Fix:** Delete the listings fallback; return `(None, None)` explicitly with a
  comment, or query `lot_listings`-attributed data if listing prices are added.

### M-6. Listing committed before lot formation — partial failure leaves orphan
- **File:** `app.py`, `create_listing()`, ~lines 540–563
- **Severity:** medium
- **Description:** `INSERT INTO listings` + `commit()` happens before
  `aggregation.check_and_form_lot(conn, ...)`, which does its own commit. If
  aggregation raises (e.g. the H-3 lot-ID collision), the listing persists but
  the lot doesn't — and the idempotency record is still stored, so a retry
  replays the "success" response while the lot was never formed.
- **Fix:** Single transaction: remove the intermediate `conn.commit()`, commit
  once after aggregation; on exception, roll back so the retry path is clean.

### M-7. Orders never leave `placed` — no fulfill/cancel/settle flow
- **File:** `app.py`, `db.py` (orders.status)
- **Severity:** medium
- **Description:** Nothing ever updates `orders.status`. The ledger's
  `o.status != 'paid'` filter is meaningless — every order is perpetually
  "payable". No cancel endpoint either, so a mistaken order can't be undone.
- **Fix:** Add staff endpoints for order state transitions
  (`placed → confirmed → delivered → settled`, plus `cancelled`), with the
  oversell guard re-checked on confirm.

### M-8. No buyer response to counter-offers — negotiation is one-sided
- **File:** `app.py`, `decide_offer()`, ~lines 845–925
- **Severity:** medium
- **Description:** Staff can `countered` an offer, but there is no endpoint for
  the buyer to accept/reject the counter. A `countered` offer just sits until
  staff unilaterally accept/reject it themselves. The state machine in the
  schema comment (`pending | accepted | rejected | countered | withdrawn |
  converted`) isn't fully wired.
- **Fix:** Add `POST /v1/offers/<id>/respond` (buyer-authenticated or
  phone-verified) accepting the counter or withdrawing.

### M-9. Rate limits keyed on `request.remote_addr` — useless behind a proxy
- **File:** `app.py` (`place_order`, `make_offer`, `assistant_chat`)
- **Severity:** medium
- **Description:** On Render (or any reverse proxy), `remote_addr` is the
  proxy's IP — every user shares one rate-limit bucket, so one abuser blocks
  everyone, or (with multiple proxies) limits never trigger.
- **Fix:** Read the client IP from `X-Forwarded-For` (first entry), trusting it
  only when the proxy is known; better, move rate limiting to authenticated
  identity where available.

### M-10. Unbounded in-memory structures — slow leaks
- **File:** `auth.py` (`_attempts` dict, `_dev_outbox` list)
- **Severity:** medium
- **Description:** `check_rate_limit` prunes timestamps per key but never evicts
  keys — the dict grows with every distinct phone/IP seen. `_dev_outbox`
  appends one entry per OTP request forever; `dev_latest_code` scans it in
  reverse each call (O(n) per login page load).
- **Fix:** Evict `_attempts` keys whose history is empty after pruning; cap
  `_dev_outbox` to the last ~200 entries (deque with maxlen).

### M-11. `decide_offer` reimplements staff check + opens two connections
- **File:** `app.py`, ~lines 848–860
- **Severity:** medium
- **Description:** Does its own `SELECT is_staff` query instead of using the
  existing `@require_staff` decorator, then closes the connection and opens a
  second one for the real work. Duplicated authorization logic drifts.
- **Fix:** Replace with `@require_staff` (applied after `@require_auth`).

### M-12. `request_code` fetches a farmer row it never uses
- **File:** `app.py`, ~lines 350–365
- **Severity:** medium (code smell with a perf cost)
- **Description:** Opens a connection, `SELECT phone FROM farmers`, closes it —
  and never reads `farmer`. Then opens a second connection for
  `create_auth_code`. Dead query + double connection per OTP request.
- **Fix:** Delete the first block entirely (the comment already explains codes
  are issued for unknown numbers).

### M-13. Seeded admin gets `role='farmer'`, not `'staff'`
- **File:** `db.py` `init_db()` + `seed.py`
- **Severity:** medium
- **Description:** The backfill `UPDATE farmers SET role='staff' WHERE is_staff=1`
  runs inside `init_db()`, but `seed()` calls `init_db(fresh=True)` *before*
  inserting the admin row — so the admin keeps `role='farmer'`. `admin_login`
  returns `"role": "farmer"` for the admin; `require_staff` still works (it
  checks `is_staff`), so this is a data inconsistency, not an outage. It
  self-heals on the next restart (backfill runs against existing rows).
- **Fix:** In `seed.py`, set `role='staff'` explicitly on the admin INSERT, or
  re-run the backfill after seeding.

### M-14. `public_stats` counts buyers as farmers
- **File:** `app.py`, ~line 1150
- **Severity:** medium (data correctness)
- **Description:** `SELECT COUNT(*) FROM farmers WHERE is_staff=0` includes
  `role='buyer'` rows in the "farmers" stat shown on the public website.
- **Fix:** `WHERE is_staff=0 AND role='farmer'` (or rename the stat).

### M-15. `SYMPTOM_INDEX` covers only 4 of 16 crops; others 400
- **File:** `app.py`, ~lines 900–925; `ai_advisor.CROPS_INFO` has 6 crops
- **Severity:** medium (product gap, not a crash)
- **Description:** Crop Doctor 400s on any crop outside the 6 in `CROPS_INFO`
  (cardamom, ginger has entries; apple/junar/lentil/etc. don't). The seed data
  and frontend advertise 16 crops.
- **Fix:** Either extend the KB or return a graceful "not yet supported for
  this crop" message instead of `unknown crop_code`.

---

## LOW

### L-1. Dead code: `ai_advisor.analyze_query()` never called
- `ai_advisor.py:75` — the old keyword matcher. `assistant.py` imports only
  `CROPS_INFO`. Delete or remove the module's stale docstring claims.

### L-2. Dead code: `assistant.extract_quantity()` never called
- `assistant.py:179` — defined, tested(?), but no handler uses it. Either wire
  it into price/sell flows or delete. (Note: its regex would also match phone
  numbers as "kg" — fix before wiring.)

### L-3. Price change arrow is dead logic
- `assistant.py`, `_price()` / `_get_price()` — always returns `change=0`, so
  the 📈/📉 branch never fires; every price shows ➡️ "steady". Either compute a
  real delta (vs previous lot rate) or drop the arrow.

### L-4. `serve_site` traversal check is subtly wrong (defense in depth holds)
- `app.py`, ~lines 330–336 — `realpath(full).startswith(realpath(site_dir))`
  without a trailing separator: `/site/../site-evil/x` would pass the prefix
  test. Currently mitigated because Flask's `send_from_directory` → `safe_join`
  rejects `..` anyway. Fix: compare with `os.path.commonpath` or append `os.sep`.

### L-5. `_next_lot_code` COUNT-based IDs collide if lots are deleted
- `aggregation.py`, ~line 108 — use a sequence or UUID suffix.

### L-6. `create_rental` doesn't validate `contact_phone`
- `app.py`, ~line 1045 — orders/offers enforce the Nepali mobile regex; rentals
  accept anything. Validate consistently.

### L-7. `signup` re-registration doesn't update `coop_id`/`lat`/`lon`
- `app.py`, ~lines 490–500 — the UPDATE covers name/land_type/payout_method/role
  only; a farmer who moves co-ops keeps stale location, silently affecting
  aggregation radius. Update all mutable fields or document why not.

---

## ARCHITECTURE ISSUES

**A-1. `app.py` is a 1269-line god module.** Routes, auth decorators,
idempotency machinery, the symptom keyword index, startup migrations, static
file serving, and business logic (offer decisions, rental booking) all live in
one file. Split into blueprints: `auth`, `market` (listings/lots/orders/offers),
`advisory` (crop doctor/assistant), `rentals`, `admin`, plus a `services/`
layer for the business rules currently embedded in handlers (offer state
machine, oversell guard).

**A-2. No transaction boundaries.** Every multi-statement write is a chain of
autocommits. This is the root cause of H-1, H-3, and M-6. Introduce a
`@transactional` helper (or a unit-of-work) that wraps request handlers in
`BEGIN IMMEDIATE … COMMIT/ROLLBACK`, so read-modify-write sequences are atomic.

**A-3. `assistant.py` mixes four concerns.** NLU (intent/entity), session store
(`_sessions` global), data access (raw SQL in handlers), and response rendering
(`T()` templates) are interleaved. Extract a `session_store`, a `data.py` for
the price/ledger queries (which would have made the H-5/M-5 invalid-SQL bugs
visible in one place), and keep handlers pure.

**A-4. Fail-silent `except: pass` in data paths.** `assistant._ledger` and
`_get_price` catch-all and return plausible defaults — this is how two
invalid-SQL bugs shipped undetected. Policy: data-access code must log and
surface errors; only the HTTP boundary decides what the user sees.

**A-5. Authorization logic duplicated, not composed.** `decide_offer`
reimplements the staff check instead of stacking `@require_staff`; `require_self`
is correct but its kwargs-vs-body fallback is subtle and untested for the
`land-scan` path. Centralize: every staff/mutating route gets its policy from
one decorator stack, covered by tests.

**A-6. Test suite structure hides failures.** Tests are scripts with `run()`,
not discoverable unittest/pytest cases (`python -m unittest discover` finds 0
tests). The C-1 regression shipped despite a test that covers it — because
nothing runs the suite automatically. Convert to pytest-style tests and run them
in CI (or at least a pre-push hook); the suite is good, it's just not wired up.

---

## Suggested fix order
1. **C-1** (OTP login down) + regression test — deploy immediately.
2. **C-2** (startup FK crash) — one-line guard.
3. **H-1, H-2, H-3** (oversell + lot races) — `@transactional` with
   `BEGIN IMMEDIATE`, reuse the guard in `decide_offer`.
4. **H-4, M-2** (PII exposure) — auth on `list_offers`, `crop_doctor_history`.
5. **H-5, M-5** (assistant invalid SQL) — fix queries, kill `except: pass`.
6. **H-6** (rental double-booking) — overlap check.
7. Mediums batch, then the A-series refactors as the codebase grows.
