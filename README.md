# Kisan Bazar (किसान बजार)

A direct farmer-to-market app for Nepal. Farmers list produce, co-ops pool it into truck-sized lots, buyers order lots, and farmers get weather warnings and crop advice. This folder holds every part of the project.

## What is in here

| Path | What it is |
|---|---|
| `kisan-bazar.html` | The app. One self-contained file: open it in any browser. Nepali by default, English toggle. Includes the land-scan feature and the AI Sahayak assistant. |
| `kisan-bazar-api/` | Flask + SQLite API: phone/OTP login, the aggregation engine (pools listings into lots), land-scan storage. |
| `kisan-bazar-db/` | PostgreSQL/PostGIS migrations (001–008) and seed data for the pilot corridor. |
| `kisan-bazar-sms/` | SMS, USSD and IVR channels for feature phones, with a runnable demo. |
| `kisan-bazar-jstests/` | Test harness that runs the app's real JavaScript, including an integration test against the real API. |
| `kisan-bazar-backend.md` | Architecture design: schema, aggregation logic, payment flow. |
| `kisan-bazar-crop-calendar.md` | Crop calendar reference by elevation zone. Draft: not yet confirmed by NARC/DoA. |
| `kisan-bazar-weather-integration.md` | How weather data should be sourced (DHM access, 1155 hotline). |
| `kisan-bazar-assistant-notes.md` | What the AI Sahayak is, and how a real LLM would connect on the server. |
| `kisan-bazar-weaknesses.md` | An honest audit of what is missing or unproven. Read this before relying on anything. |

## Running the tests

Needs Python 3 with Flask, and Node 18 or newer. Keep the folders side by side as delivered; the tests find each other by relative path.

```bash
# API tests (run from kisan-bazar-api/)
python3 seed.py
python3 tests/test_auth.py
python3 tests/test_api.py
python3 tests/test_land_scan.py

# SMS/USSD demo (from kisan-bazar-sms/)
python3 tests/test_flows.py

# App tests (from kisan-bazar-jstests/)
node test_app.js            # 38 checks
node test_assistant.js      # 58 checks
node test_integration.js    # 37 checks, starts the real API itself
```

## Status

- The app works as a demo. Weather, prices and co-op lots shown in it are sample data.
- Login and listing produce are wired from the app to the API and tested end to end. This is switched off by default (`BACKEND.enabled` in `kisan-bazar.html`) because no deployed API exists yet.
- The PostgreSQL migrations have never been run against a live database.
- Nothing has been checked with real farmers, co-ops or DHM. See `kisan-bazar-weaknesses.md`.
