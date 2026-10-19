# Kishan-Bazzar Frontend Code Review

**Date:** 2026-10-19
**Scope:** `kisan-bazar.html` (2942 lines, single-file app), `website/` (10 pages + `site.js` + `chat.js` + `style.css`)
**Method:** Line-by-line read of all JS, cross-checked frontend/backend API contracts against `kisan-bazar-api/app.py`, `node --check` syntax validation (all pass).

> Note: JS syntax is valid everywhere. Every issue below is a runtime/logic problem, not a parse error.

---

## CRITICAL — broken user journeys

### C1. `loadLots()` stores raw backend crop codes; market view crashes (TypeError)
- **File:** `kisan-bazar.html`, ~line 918 (`loadLots`), ~line 2148 (`viewMarket`), ~line 2309 (`viewCart`)
- **What happens:** Backend `/v1/lots` returns `crop_code` values like `"KAULI"`, `"GOLB"`. `loadLots()` maps them verbatim: `crop: l.crop_code`. But `viewMarket()` does `const c = CROPS[l.crop]` — `CROPS` is keyed by `"cauliflower"`, `"tomato"`, etc. `CROPS["KAULI"]` is `undefined`, and the next line reads `c.ic` → **TypeError, entire market view goes blank**. Same unguarded pattern in `viewCart()` (`c.ic`, `c.ne`).
- **Trigger:** Any session with `BACKEND.enabled` (the default on Render) once lots load. I.e., the live demo's main buyer journey is broken.
- **Fix:** Map through the existing lookup: `crop: CROP_CODE_MAP[l.crop_code] || l.crop_code`. Add a guard at the render sites too: `const c = CROPS[l.crop] || {}` (line 2192 already does this — copy that pattern).

### C2. `apiRegister` is called but never defined — modal registration always fails
- **File:** `kisan-bazar.html`, line 1589 (`doAuthRegister`)
- **What happens:** The auth modal's "Register" button calls `await apiRegister(...)`. No such function exists (only `apiRequestCode`, `apiVerifyCode`, `apiCreateListing`, `apiSaveLandScan`, `apiGetLandScan`, `apiGetMe` are defined). Throws `ReferenceError`, caught by the surrounding try/catch → user always sees "दर्ता हुन सकेन / Registration failed." New-user signup through the modal is 100% broken. (The older inline `doRegister()` at line 694 works — it inlines the fetch — so there are two registration paths and the visible one is dead.)
- **Fix:** Define `apiRegister(phone, name, role)` mirroring `doRegister()`'s fetch to `/v1/auth/signup`, or delete `doAuthRegister` and route the modal through `doRegister()`.

### C3. `doAdminLogin` writes session to wrong localStorage keys — admin logged out on reload
- **File:** `kisan-bazar.html`, ~lines 1548–1556
- **What happens:** On success it does `store.set("token", ...)`, `store.set("phone", ...)`, `store.set("name", ...)`, `store.set("role", "staff")`. But the `account` object hydrates from `"accountToken"`, `"accountPhone"`, `"accountName"`, `"accountRole"`. After a page reload the admin session is gone. It also never sets `account.isStaff = true` and never calls `saveAccount()`.
- **Fix:** Replace the four `store.set` calls with `account.isStaff = true; saveAccount();`.

### C4. `doPlaceOrder` double-tap guard leaks — order button dies permanently after one failed attempt
- **File:** `kisan-bazar.html`, ~lines 2247–2275
- **What happens:** `_ordering = true` is set at the top. The two early exits (`!BACKEND.enabled`, `!buyerPhone`) `return` **without resetting `_ordering = false`**. After one tap while offline or logged out, every subsequent tap hits `if(_ordering) return;` forever (until reload). The user is told to log in, does so, taps again — nothing happens, no feedback.
- **Fix:** Reset `_ordering = false` before each early `return`, or use try/finally around the whole body.

### C5. Website `login.html` calls a non-existent endpoint — website login always 404s
- **File:** `website/login.html`, line 107
- **What happens:** `verifyOTP()` posts to `/v1/auth/verify-code`. The backend only defines `@app.post("/v1/auth/verify")` (`app.py` line 387). Every website login attempt fails with "Wrong code."
- **Fix:** Change to `/v1/auth/verify`.

### C6. Website `lot.html` doesn't unwrap the paginated `/v1/lots` response — lot detail page never loads
- **File:** `website/lot.html`, inline script
- **What happens:** `const lots = await (await fetch("/v1/lots")).json();` then `(lots||[]).find(...)`. But `/v1/lots` returns `{data, total, limit, offset}`, not an array. `.find` is not a function on the object → TypeError → caught → page always shows "लोड हुन सकेन।" Every lot detail page is broken. (`site.js`'s `api()` helper already unwraps pagination; `lot.html` doesn't use it.)
- **Fix:** `const payload = await (await fetch("/v1/lots")).json(); const lots = Array.isArray(payload) ? payload : (payload.data || []);`

### C7. `loadLots()` expects API fields that don't exist — silent data loss
- **File:** `kisan-bazar.html`, ~lines 918–925
- **What happens:** Maps `coop: l.coop_name || "सहकारी"`, `coopEn: l.coop_name_en || "Co-op"`, `farmers: l.farmer_count || 0`. `/v1/lots` returns `SELECT * FROM lots` — no `coop_name`, `coop_name_en`, or `farmer_count` columns. Every live lot renders as "सहकारी / Co-op" with 0 farmers. (There is a separate `/v1/market/lots` endpoint that joins crop names, but the frontend doesn't call it.)
- **Fix:** Either call `/v1/market/lots` (and adapt), or extend `/v1/lots` with a co-op join + farmer count, then map real fields.

---

## HIGH — likely broken or badly degraded

### H1. `fetchAdminOffers` / `fetchMyOffers` omit the Authorization header
- **File:** `kisan-bazar.html`, ~line 2608 (`fetchAdminOffers`), ~line 2330 (`fetchMyOffers`)
- **What happens:** Both call `fetch(.../v1/offers...)` with no headers, while `decideOffer` (same file) sends `Authorization: Bearer ...`. If the list endpoints require auth (they should — offers contain buyer phone numbers), both lists 401 → the `.catch` returns `[]` → admin sees "No new offers" and buyer sees an empty offers tab even when offers exist. Silent failure, no error shown.
- **Fix:** Add `headers: {"Authorization": "Bearer " + account.token}` to both, matching `decideOffer`.

### H2. Auth modal role toggle wipes the typed phone number
- **File:** `kisan-bazar.html`, `renderAuthModal()` phone step (~line 1455)
- **What happens:** The phone `<input>` is rendered with `value="${account.phone||""}"`. Clicking the Farmer/Buyer role toggle fires `render({skipScroll:true})`, which re-creates the modal DOM and resets the input to `account.phone` (still `""` until OTP is requested). Typing a number then choosing a role erases the number. Same pattern wipes the OTP code input on any re-render.
- **Fix:** Sync input → state on `input` event, or read the live DOM value before re-render and stash it in `account.phoneDraft`.

### H3. Cart quantity stepper crashes on stale lot id
- **File:** `kisan-bazar.html`, ~line 2852 (click delegation, `t.dataset.lot`)
- **What happens:** `const l = LOTS.find(x=>x.id===id); S.cart[id] = Math.max(0, Math.min(l.kg, ...))` — if `l` is undefined (stale `data-lot` after lots reload), `l.kg` throws TypeError inside the delegated click handler, breaking all subsequent clicks on the page.
- **Fix:** `if(!l) return;` before touching `l.kg`.

### H4. No error boundary in `render()` — one throwing view blanks the whole app
- **File:** `kisan-bazar.html`, ~line 2703
- **What happens:** `$("main").innerHTML = (VIEWS[S.tab]||viewToday)();` — if any view throws (C1, H3, corrupt localStorage data), the exception propagates out of `render()`, leaving a half-rendered or blank page with no recovery except reload.
- **Fix:** Wrap the view call in try/catch and render a fallback error card with a "retry" button.

### H5. Website login token doesn't carry over to the app
- **File:** `website/login.html` (~line 113)
- **What happens:** On success it stores `localStorage["kb-token"]` / `["kb-phone"]` and redirects to `/`. The app hydrates from `"kb:accountToken"` / `"kb:accountPhone"`. After redirect the app still shows logged-out. The login round-trip is pointless.
- **Fix:** Use the same keys as the app (`kb:accountToken`, `kb:accountPhone`, `kb:accountName`, `kb:accountRole`), or better, drop the website login page and deep-link into the app's auth modal.

### H6. `WEATHER[S.zone]` has no fallback for corrupt localStorage
- **File:** `kisan-bazar.html`, ~line 1028 (`viewToday`)
- **What happens:** `S.zone` comes from localStorage. If it holds an unknown value, `WEATHER[S.zone]` is `undefined` and `w.level` throws → blank app (via H4).
- **Fix:** `const w = WEATHER[S.zone] || WEATHER.midhill;`

---

## MEDIUM — logic mistakes & UX breakage

### M1. Two parallel auth flows (modal + inline), one broken
- **File:** `kisan-bazar.html`, `renderAuthModal()` (~1451) vs `renderAccountPanel()` (~2067)
- The header "Login / Register" button opens the modal (new flow); the Mine tab shows an inline phone/OTP/register form (old flow). They share the `account` object but use different element IDs and different functions. The modal's register path is dead (C2). Two flows to maintain, two sets of bugs.
- **Fix:** Pick one. The modal is the visible entry point — fix C2/C3/H2 there and remove the inline form, or remove the modal.

### M2. `doVerifyCode` (old flow) toasts "Connected" even when profile fetch fails
- **File:** `kisan-bazar.html`, ~lines 660–683
- The `catch` around `apiGetMe` only handles 404 (→ register step). Any other error (network, 500) is swallowed and execution falls through to `toast("जोडियो/Connected")` with no name/role set. Misleading success state.
- **Fix:** Re-throw or handle non-404 errors: show the error, don't toast success.

### M3. `renderPriceTrends` doesn't guard empty point arrays
- **File:** `kisan-bazar.html`, ~line 1136: `const first = pts[0].avg_rate`
- If the API returns a crop key with an empty array, `pts[0]` is undefined → TypeError → blank Today tab (via H4).
- **Fix:** `if(!pts || pts.length < 2) return "";` inside the map (sparkline already guards `< 2`).

### M4. `viewMine` shows a hardcoded fake payout number as the user's
- **File:** `kisan-bazar.html`, ~line 2044: `eSewa · ९८४२XXXXXX`
- Rendered under "Where payment arrives" as if it were the user's configured payout. It's a placeholder presented as account data — the kind of demo-data-as-real the project was explicitly told to stop doing.
- **Fix:** Show the actual `account.phone`-derived payout or label it clearly as an example.

### M5. `doBookRental` uses blocking native `prompt()` dialogs
- **File:** `kisan-bazar.html`, ~lines 2010–2016
- Two chained `prompt()` calls for days and date. Blocking, unstyled, no validation UI, terrible on mobile, and `parseInt(days)` has no radix. Cancel on the second prompt silently aborts (acceptable) but there's no date format validation — a malformed date goes straight to the API.
- **Fix:** Replace with inline form fields in a small dialog; validate `YYYY-MM-DD` with a regex before POST.

### M6. Inline `onclick` handlers in the auth modal
- **File:** `kisan-bazar.html`, `renderAuthModal()` — `onclick="doAuthRequestCode()"`, `onclick="closeAuth()"`, `onclick="useDemoAccount(i)"`, etc.
- The rest of the app uses delegated `addEventListener` handlers. Inline handlers depend on globals, bypass the delegation conventions, and the `onclick="if(event.target===this)closeAuth()"` overlay pattern is fragile.
- **Fix:** Give the buttons `id`/`data-action` attributes and handle them in the delegated click listener.

### M7. No Enter-key support in modal inputs
- **File:** `kisan-bazar.html`, keydown delegation (~2870)
- Enter is handled for `chatInput`, `phoneInput`, `codeInput` (old flow) but not for the modal's `auth-phone`, `auth-code`, `admin-user`, `admin-pass`, `auth-name`. Keyboard users must click buttons.
- **Fix:** Add the modal input IDs to the keydown handler.

### M8. Forced role jump on auth gates is jarring
- **File:** `kisan-bazar.html`, `showOfferDialog` (~2180), `doPlaceOrder` (~2262), `doBookRental` (~2005)
- When a logged-out user taps "Make an offer" / "Confirm order", the code sets `S.role="farmer"; S.tab="mine"` — yanking a buyer-context user into the farmer panel. The toast says "log in as buyer" while the UI shows farmer tabs.
- **Fix:** Open the auth modal in place (`openAuth()`) instead of switching roles.

### M9. `chat.js` depends on `site.js` load order via global `API`
- **File:** `website/chat.js` — `fetch(API + "/v1/assistant/chat")`
- `API` is a `const` in `site.js`. Works only because every page loads `site.js` before `chat.js`. A page that includes `chat.js` alone (or a future reorder) throws `ReferenceError` on first message send.
- **Fix:** Define `const API = ""` (or `window.KB_API`) defensively at the top of `chat.js`.

### M10. style.css references undefined `--muted`; `.muted` class undefined
- **File:** `website/style.css` — `.empty{...color:var(--muted)}`; `--muted` is never defined in `:root`. `.muted` is used across pages but only `.section .muted` is styled — bare `<p class="muted">` (404, lot, login pages) gets no styling.
- **Fix:** Add `--muted:#6b7a6b;` (or map to `--ink-2`) and a top-level `.muted{color:var(--ink-2)}` rule.

---

## LOW — code smells (fix opportunistically)

- **L1. Monolith:** 2942-line single file, ~70 top-level functions, mutable globals (`S`, `account`, `sell`, `scan`, `chat`, `doctor`, `trendsCache`, `rentalsCache`, `adminCache`, `myOffersCache`, `adminOffersCache`). Full `innerHTML` re-render on every state change loses input focus and is the root cause of H2. Long-term: split into modules (state, api, views, i18n) or adopt a tiny render-diff.
- **L2. Cache objects never invalidate on logout:** `myOffersCache`, `adminOffersCache`, `trendsCache`, `rentalsCache`, `adminCache` survive logout — a second user on the same device briefly sees the previous user's offers. Clear caches in the logout handler.
- **L3. `esc()` doesn't escape single quotes or backticks.** Fine for element content; risky if ever interpolated into an attribute delimited by `'` — audit before reuse.
- **L4. `N()`/`money()` assume numeric input.** `money(undefined)` → `"Rs NaN"`. Call sites mostly guard, but `offerRow` (`o.kg*o.rate`) and trends don't fully.
- **L5. Duplicated i18n tables.** `CROP_NE`/`CROP_EN`/`CROP_ICON` in `site.js` duplicate `CROPS` + `CROP_CODE_MAP` in the app. One shared source would prevent drift (they've already drifted: the app has no `CROP_ICON` keyed by code).
- **L6. `speak()`/`speakReply()` TTS fires on every bot reply** if voice-out was ever toggled — `chat.voiceOut` default isn't shown in the excerpt; verify it defaults off.
- **L7. `loadLots()` early-returns when `lotsLoaded`** — after `doConfirmLot` (admin confirms a lot → status flips to ready) the buyer's lot list never refreshes until reload. Invalidate `lotsLoaded` after order/offer mutations.

---

## ARCHITECTURE NOTES

1. **No separation of concerns.** State, API layer, i18n, and ~25 view functions live in one script scope. The standing directive asks for "clean code plus good architecture" — the highest-leverage step is extracting `api.js` (all fetch wrappers), `state.js` (S/account/caches with a subscribe/notify), and `i18n.js` (T/N/money/CROPS). Everything else can follow.
2. **Render-everything on every change.** `render()` rebuilds header, nav, main, chat, and modal DOM on each interaction. This is what causes H2 (input wipe), focus loss in chat, and the need for `skipScroll` plumbing. Even a minimal keyed diff or "render only the dirty region" would fix a class of bugs.
3. **Two auth systems, two token key namespaces, two crop vocabularies** (CROPS keys vs backend codes vs doctor's codes). Each duplication is a bug farm — C2, C3, H5, C1 are all instances.
4. **Frontend/backend contract is implicit.** C7 (missing `coop_name`/`farmer_count`) and C5/C6 (wrong endpoints, unwrapped pagination) show there's no shared contract or integration test hitting the real endpoints from the frontend. A small smoke test (fetch each endpoint, assert shape) would catch these.
5. **Website and app are separate i18n implementations** (`data-ne`/`data-en` attributes vs `T()` function) with separate crop tables — the same string fixes have to be made twice.

---

## FIX PRIORITY (suggested order)

1. C1 (crop code mapping) — unbreaks the live buyer journey
2. C5 + C6 (website login endpoint, lot page pagination) — unbreaks the website
3. C2 (define `apiRegister`) or remove the dead modal register path
4. C4 (guard leak) + C3 (admin session keys)
5. H1 (auth headers on offer fetches), H3 (lot null guard), H4 (render try/catch)
6. H2 (input wipe), H5 (token key alignment), H6 (WEATHER fallback)
7. M-items, then architecture extraction
