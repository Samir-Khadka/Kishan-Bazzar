# Kisan Bazar — prototype test harness

Runs the actual JavaScript from `kisan-bazar.html` — not a reimplementation
of its logic — inside a minimal stubbed DOM, using Node's `vm` module. This
is how the two real bugs in the land-scan feature were caught before
shipping, and running it more broadly against the rest of the app caught
a bug in the *test itself* (see below) while confirming the app's core
view-rendering and pricing logic is correct.

## Run it

```bash
node test_app.js         # 38 assertions — core view rendering, pricing, land scan
node test_assistant.js   # 58 assertions — the AI Sahayak rule engine and chat UI
node test_integration.js # 37 assertions — app code <-> the REAL Flask API over HTTP
```

Expects `../kisan-bazar.html` to exist (one directory up) — copy the
current prototype there before running if you're working from a fresh
checkout.

## How it works

`harness.js` extracts the `<script>` block from the HTML file and runs it
in a `vm` context with stubbed `document`/`localStorage`/etc. — just
enough that `render()` and friends don't throw, without needing a real
browser or `jsdom` (not installable in a network-disabled sandbox).

The one non-obvious trick: top-level `const`/`let` bindings in a script
run via `vm.runInContext` do **not** become properties of the context
object — only `var` and function declarations do. Since almost everything
worth testing here (`RULES`, `S`, `sell`, `scan`, `money`, `classifySoilColor`,
every `view*` function) is declared with `const`/`let`, they'd normally be
invisible from outside. The fix: append one line to the *same* script
(so it shares the same lexical scope) that copies everything onto
`globalThis.__test` before execution finishes. See `EXPORT_LIST` in
`harness.js`.

## What it checks (38 assertions, currently all passing)

- **Formatting**: Devanagari numeral conversion, `money()` rounding, `esc()`
  actually escapes `<`, `"`, `&` while leaving Devanagari text untouched.
- **Land scan**: the HSL-based colour classifier and confidence-scoring
  logic, re-verified in the app's actual runtime (these were unit-tested
  standalone during development, where two real bugs were caught and
  fixed — see the main project history).
- **RULES data integrity**: every rule's crop code exists in `CROPS`, every
  zone/land value is a real id, every sow month is a valid 0–11 integer.
- **`viewAdvice()` across all 20 zone×land combinations**: must never throw.
  7 of 20 correctly show no recommendation this month — checked against
  which combos that *should* be true for (pakho isn't a primary
  configuration in the lowland rule set; khet/balaute barely exist at high
  elevation) rather than just asserting "no crashes and move on."
- **`viewToday()` across all 5 zones**: must never throw, and must show
  *that zone's* weather bulletin, not a stale or wrong one.
- **`viewSell()` earnings math**: grade-A and grade-B multipliers apply
  correctly to the displayed co-op total, and the middleman comparison
  figure is always strictly lower (that comparison is the entire pitch to
  a farmer on that screen — if the math were ever wrong or inverted, it'd
  be actively misleading, not just cosmetically off).
- **`offerRow()`**: correct status tag per state (`lot`/`paid`/`new`), and
  the displayed total matches `kg × stored rate`.
- **Buyer/co-op views**: all six don't throw.
- **Scan-pending suppression**: mid-scan (photo/quiz not yet finished),
  the advice screen must not show recommendations based on stale `S.land`
  — this was a deliberate design decision when the land-scan feature was
  built, and this test guards it from regressing silently.

## A bug this run caught — in the test, not the app

The first version of the "does every zone/land combo show a
recommendation" check looked for the substring `"card"` anywhere in the
rendered HTML. That's a false signal: the word `"card"` appears in CSS
class names used by *every* screen state — the empty-state div is
literally `class="card empty"` — so the check could never actually detect
"no recommendation" and silently reported `0/20 empty` regardless of the
real data. Fixed by checking for the recommendation card's actual marker
(`class="rec`) instead, which then correctly found the 7 genuinely-empty
combos and let them be checked against what should agronomically be
sparse, rather than trusted blindly.

Worth keeping in mind for anyone extending this suite: a "the app didn't
crash" test is weaker than it looks if the pass/fail signal itself is
checking for the wrong thing. The fix here was to assert against a
CSS class specific enough to only exist on the element actually being
tested for.

## test_assistant.js — the AI Sahayak rule engine

Separate file, 58 assertions, covering the client-side assistant added
later (see `../kisan-bazar-assistant-notes.md` for why it's rule-based
rather than backed by a live LLM call, and how it upgrades to one).

What matters most in this suite isn't "does it respond" — a rule-based
matcher can easily return *something* plausible-looking without it being
*correct*. So most assertions check the reply against the app's actual
live state rather than a fixed expected string:

- Price answers are checked against `CROPS[x].rate` at test time, not a
  hardcoded number — so the test still passes correctly if rates change.
- Weather answers are checked across all 5 zones, confirming the reply
  matches *that* zone's bulletin, not a stale or default one.
- Plant advice is checked across all 20 zone×land combinations against
  the same `RULES` filtering `viewAdvice()` itself uses — including that
  the 7 combos with no real recommendation get the honest "improve your
  soil" fallback, not a fabricated crop.
- The ledger answer is checked with a **mixed** pending+paid offer list,
  specifically to catch the paid offer's amount leaking into the "still
  owed" figure — an easy mistake that would show a farmer a wrong number
  about their own money.
- Romanized Nepali (`golbheda`, `kauli`, `aalu`, ...) is tested alongside
  Nepali script and English, since that's how many farmers will actually
  type on a phone keyboard.
- The chat UI itself: chips render only on the latest message (not
  accumulating down the whole conversation), whitespace-only input adds
  nothing, and opening/closing the panel doesn't scroll the underlying
  page (a real bug caught and fixed during this build — see below).

### A real bug caught before shipping

The original `render()` function unconditionally called
`window.scrollTo(0,0)` on every call, which was fine when every render
came from a tab switch — but once the assistant's `sendChatMessage()`
started calling the same `render()` to redraw the chat, sending a message
would have yanked the whole page back to the top instead of just updating
the chat panel. Fixed by giving `render()` an optional `{skipScroll:true}`
the chat-only code paths pass, while tab/role switches still get the
scroll-to-top they need. Caught by re-reading the render call graph while
wiring the feature, not by the test suite itself — worth noting as a
reminder that these tests check *output correctness*, not *side-effect
correctness* like scroll position, which needs a real browser to verify
directly.


## test_integration.js — the app talking to the real API

Unlike the other two suites, this one starts the actual Flask server
(`../kisan-bazar-api/tests/run_test_server.py`) and runs the app's own
login and listing code against it with Node's real `fetch`. It then asks
the server directly what it stored and compares. Needs `python3` + Flask
(both already required by the API); no other setup, ~10 seconds.

The launcher mirrors the API's mock SMS outbox to a file so the test can
"receive" the login code the way a real test would read an SMS-provider
double. The API itself never returns a code in any HTTP response and the
launcher does not change that.

Sections: default-disabled behaviour; login errors; real login→token→
listing with server-side verification; all 16 crops accepted by the API;
the aggregation engine driven from app code (including the out-of-radius
farmer who must not trigger a lot); authorization boundaries (farmer
cannot price a lot, staff can); failure handling (server down, bad token,
not logged in); CORS headers.

**Proven to be able to fail:** the suite was run against two deliberately
broken copies of the app (Authorization header removed; one crop code
mis-mapped) and failed on the specific checks it should each time.

**Limits — read these:** Node's `fetch` doesn't enforce CORS and isn't a
browser, and the server is on localhost in the same sandbox. So this proves
the code paths and data contracts are compatible; it does not prove a
browser on the public internet can reach a deployed API. That needs a real
deployment and a real-browser test.

`harness.js` also exports `evalInApp(code)`: the app reassigns some
top-level `let` bindings (e.g. `sell = {...}` after a post), which leaves
exported snapshots like `A.sell` stale; evaluating inside the app's own
context always sees the live binding. (A stale-snapshot trap that is easy
to fall into when writing more tests.)
