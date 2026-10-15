# Kisan Bazar — AI Sahayak: what it is, and the honest upgrade path

## What's actually running

The assistant in the app right now is **rule-based, not an LLM** — keyword
matching against a fixed set of intents (weather, price, sell, plant,
ledger, land-scan, greeting, help), answered using the app's own live data
(current zone's weather, today's rates, the farmer's actual pending
payments). It understands Nepali script, English, and common romanized
Nepali spellings (`golbheda`, `kauli`, `aalu`, etc.) for the ~16 crops the
app already knows about.

This is a deliberate choice, not a placeholder for something better I
didn't get to. Three real constraints point the same direction:

1. **A published page can't safely hold an API key.** Anything in the
   page's source is visible to whoever opens dev tools — there is no
   server-side place to keep a secret in a static HTML artifact.
2. **A published page's content-security policy blocks it anyway.**
   Published pages can only load scripts from a short allow-list
   (cdnjs, jsdelivr, the Tailwind play-CDN, jQuery's CDN) and can't make
   arbitrary network requests to an LLM API endpoint — this is enforced
   at the platform level, not a choice I could code around.
3. **Rule-based means it works with no signal at all**, which matters
   more here than almost anywhere — a farmer checking prices from a
   field with no data connection needs the app's own cached numbers, not
   a round-trip to a model that requires connectivity to answer "what's
   today's cauliflower rate."

So rather than build something that only half-works once published, or
quietly fake it with hardcoded responses that look dynamic but aren't,
I built the rule engine to actually pull from live app state — tested
across all 5 weather zones and all 20 zone×land combinations so the
answers are genuinely correct, not just plausible-looking.

## Where a real LLM belongs, and how it connects

The backend already built for this project (`../kisan-bazar-api/`) is
exactly the right place for this. The pattern:

```
Farmer's phone  --https-->  Kisan Bazar backend  --API key held server-side-->  Claude API
```

The backend holds the API key (in an environment variable or secrets
manager, never in client code), receives the farmer's question over a
normal authenticated API call, calls Claude server-side, and returns the
answer. This is the same shape as any production app that uses an LLM —
the key never leaves the server, and the client never talks to the model
provider directly.

**What a real LLM adds over the current rule engine:** genuinely
open-ended questions the fixed intent list can't cover — "my tomato
leaves are curling, what's wrong," "should I sell now or wait a week,"
a question phrased in a way the keyword matcher doesn't catch. The
rule engine is good at the ~8 things farmers ask constantly; an LLM is
good at everything else, at the cost of needing connectivity and a
server round-trip.

**A reasonable hybrid, once there's a backend to build it on:**
1. Try the rule engine first — it's instant and works offline, and
   covers the majority of real traffic (rates, weather, "how do I sell").
2. If nothing matches with confidence, and the phone has a connection,
   fall through to `POST /v1/assistant/ask` on the backend, which calls
   Claude with the farmer's question plus real context (their zone,
   land type, recent listings — the same data the rule engine already
   uses) so the model isn't answering blind.
3. If there's no connection and the rule engine didn't match, fall back
   to what the current fallback already does: offer the co-op's phone
   number. A human is always the final fallback, not a spinner.

**Guardrails that matter once a real model is in the loop**, worth
deciding before building this, not after:
- Nothing agronomic an LLM says should be presented with more
  confidence than the sourced crop-calendar data already in this
  project (see `kisan-bazar-crop-calendar.md`) — an LLM's crop advice
  should carry the same "confirm with your local agriculture office"
  framing the land-scan feature already uses, not less.
- The model should never be the channel for anything transactional
  (placing an order, confirming a lot, moving money) — those stay on
  the deterministic API endpoints already built, where a bug means a
  clear error, not a plausible-sounding wrong action.
- Log what the rule engine *couldn't* answer (the queries that fell
  through) — that log is the roadmap for which new intents are worth
  adding to the fast, offline-capable rule engine over time, so the
  set of "instant, no-signal-needed" answers keeps growing instead of
  every open-ended question permanently needing a network round trip.
