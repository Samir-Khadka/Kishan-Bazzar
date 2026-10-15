# Kisan Bazar — SMS / USSD / IVR channel

Non-app access to the same four core actions the app prototype offers:
check rates, check weather, list produce for sale, check your ledger. This
is the reach layer for farmers above ~2000m, on feature phones, or with
weak data connections — not a lesser version of the app, a parallel front
door to the same backend (see `../kisan-bazar-backend.md` for the API
surface these handlers call in production).

**Verified runnable in this sandbox** — `mock_backend.py` stands in for the
real API; swap it for an HTTP client against `/v1/listings`, `/v1/market/lots`,
etc. and the menu/parsing logic here doesn't change.

## Run it

```bash
python3 tests/test_flows.py
```

Prints nine example conversations (USSD and SMS) plus two regression checks,
end to end, in both Nepali and English.

## Files

```
mock_backend.py    Stand-in for the real API — crops, farmers, weather,
                    listings. Swap for an HTTP client, keep the interface.
ussd.py            *1234# menu state machine (see design note below)
sms.py             Keyword commands: RATE, BECH, MAUSAM, HISAB, HELP
IVR_MENU.md         Voice call script and DTMF map for a 1618-style line
tests/test_flows.py Runnable demo conversations + regression checks
```

## One vocabulary across three channels

Crop codes (`KAULI`, `GOLB`, `ALU`, `PYAJ`, `ADUWA`, `MAKAI`) and the same
four intents (rate / weather / sell / ledger) are shared across USSD, SMS,
and IVR. A co-op field agent training a farmer teaches one mental model,
not three — which channel the farmer actually uses on a given day comes
down to signal strength and phone type, not what they learned.

## A bug worth knowing about, since it shows the real design trap here

The first version of `ussd.py` derived the caller's position in the menu by
re-parsing the *entire* accumulated dial string on every request (the way
telco USSD gateways deliver it — each request repeats the full history,
e.g. `"0*4"` means "pressed 0, then 4"). That's fine for stateless
navigation, but the language toggle has a side effect (flipping
`session.lang`), and reprocessing the same `"0"` on every replay flipped it
again each time — so `0` then `4` silently reprocessed the `0` and dropped
the `4` entirely. A farmer switching to English and then asking for their
ledger would silently get bounced back to the root menu instead.

Fixed by moving state onto the `Session` object (`session.stage`) and
reading only the *latest* keypress each call, instead of replaying history.
`tests/test_flows.py` now asserts this specific sequence stays correct —
worth keeping that test if this module gets extended, since it's exactly
the kind of bug that passes a quick manual check and fails in production
three menus deep.

## What's still a stub

- `mock_backend.py` is in-memory, single-process — replace with real API
  calls per the backend architecture doc before this touches real farmers.
- No rate limiting / abuse handling on the SMS command parser — a malformed
  or spam-flooded inbound queue needs backpressure before launch.
- The USSD `Session` here lives in a Python object for the demo; production
  needs it in Redis with a TTL matching the telco's session timeout
  (typically ~90 seconds of caller inactivity).
- IVR is a written script only (`IVR_MENU.md`) — wiring it into an actual
  telephony platform (Twilio-style or a local Nepali telco's IVR product)
  is separate work, and recorded prompts need a native speaker, not TTS,
  for anything alert-related.
