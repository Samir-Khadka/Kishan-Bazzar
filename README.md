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
python3 tests/test_security_fixes.py   # regression tests for the Oct 2026 hardening pass

# SMS/USSD demo (from kisan-bazar-sms/)
python3 tests/test_flows.py

# App tests (from kisan-bazar-jstests/)
node test_app.js            # 38 checks
node test_assistant.js      # 58 checks
node test_integration.js    # 37 checks, starts the real API itself
```

## Status

- The app works as a demo. Weather, prices and co-op lots shown in it are sample data.
- The API is deploy-ready: `wsgi.py` + `render.yaml` serve the app and API from one
  service (gunicorn). When served from the Flask app on a public host, the frontend
  auto-enables its backend connection on the same origin — no config needed.
  Set `KISAN_BAZAR_SECRET` and keep `KISAN_BAZAR_DEMO_MODE=1` only for the public demo
  (it exposes simulated OTP codes so visitors can log in without a real SMS gateway).
- Login and listing produce are wired from the app to the API and tested end to end.
- The PostgreSQL migrations have never been run against a live database.
- Nothing has been checked with real farmers, co-ops or DHM. See `kisan-bazar-weaknesses.md`.

## Deploying the demo (Render, free tier)

1. Push this repo to GitHub (it is the deploy source).
2. In Render: New → Web Service → connect the repo. `render.yaml` fills in the
   build (`pip install -r kisan-bazar-api/requirements.txt`) and start
   (`gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2`) commands automatically.
3. Render generates `KISAN_BAZAR_SECRET` for you; `KISAN_BAZAR_DEMO_MODE=1` is already
   set in `render.yaml` so visitors can log in with the demo accounts below.
4. Open the service URL — the market loads with the backend already connected.

Demo accounts (OTP code is shown in the app after you request it — demo mode only):

| Who | Phone |
|---|---|
| Sarita Tamang (farmer) | 9841000001 |
| Ram Bahadur K.C. (farmer) | 9841000002 |
| Mina Shrestha (farmer) | 9841000003 |
| Krishna Lama (farmer) | 9841000004 |
| Kavre Co-op Desk (staff) | 9801100000 |

Note: Render's free disk is ephemeral — the demo database re-seeds itself on every
deploy/restart, so every visitor gets a clean market. Listings made by visitors do
not persist across restarts. For a persistent database, attach Render Postgres and
port `db.py` to the `kisan-bazar-db/` migrations.
