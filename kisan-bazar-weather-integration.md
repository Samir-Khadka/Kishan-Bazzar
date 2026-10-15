# Kisan Bazar — Weather Integration: Revised Plan

This replaces §6 ("Weather ingestion — practical detail") of the backend
architecture doc. That section assumed a scraper against DHM's bulletin
pages was the practical path. Having actually checked DHM's site, that
assumption was wrong in a way worth correcting rather than quietly fixing.

---

## What I found, checking DHM's actual site

1. **The bulletin subdomain explicitly disallows automated access.**
   `meteo.dhm.gov.np/bull3/...` refuses fetches per its own `robots.txt`.
   A scraper against it wouldn't just be fragile — it would be built on a
   deliberate violation of DHM's stated access policy. Not a foundation
   to put under a product that leans on trust.

2. **The main site (`dhm.gov.np`) is a client-rendered React app**, not
   server-rendered HTML. Fetching it directly returns literally
   `"You need to enable JavaScript to run this app"` — there's nothing
   for a plain HTTP scraper to parse. Getting content out would require
   a headless browser (Playwright/Puppeteer), which is more fragile,
   slower, and more likely to break silently on every DHM redesign than
   the architecture doc's original plan implied.

3. **DHM already has an official structured data-request process** at
   `dhm.gov.np/data-service` — a form asking occupation (student /
   government office / individual researcher / NGO-INGO), purpose, and
   supporting documents, with terms to accept. A separate page
   (`dhm.gov.np/pages/data`) references **priced digital meteorological
   data** with a stated payment procedure. This is the legitimate route
   for programmatic access, not a scrape target.

4. **DHM already runs a toll-free public hotline: 1155** — press 1 for
   weather forecasting, press 2 for flood early warning — displayed
   prominently on their homepage. This directly overlaps with the IVR
   channel designed earlier in this project.

5. **Flood forecasting lives on a separate system** (`hydrology.gov.np`),
   also client-rendered (river watch, map/tabular views) — same access
   constraints as the main site.

---

## What this changes

**Drop the scraper plan entirely.** It was built on an assumption I hadn't
checked. The honest options are:

- **Apply through `dhm.gov.np/data-service`** for structured/bulk data
  access, budgeting for the fact that it's a manual review process (an
  NGO/company would likely apply under "NGO / INGO" or as a government
  partner) and that some data has a price attached. This is worth
  starting early — government data-access approvals are rarely fast, and
  the pilot corridor's launch timeline should assume months, not weeks,
  for this to clear.
- **In parallel, use Open-Meteo (or a similar public gridded forecast
  API) for the routine 5-day strip** in the app — this was already the
  plan for granular forecast in the original architecture doc and
  doesn't change. It was always meant to be supplementary to DHM, never
  a substitute for DHM's authoritative severity classification.
- **For severe weather specifically, point farmers to 1155 rather than
  duplicating it.** DHM already operates a toll-free number with exactly
  the "press 1 for weather, press 2 for flood" structure this project's
  IVR design was independently converging on. Rebuilding that from
  scratch would be redundant infrastructure competing with an existing
  public service farmers may already know. Two better paths, in order
  of preference:
  1. **Ask DHM, as part of the data-service application, whether they'll
     share bulletin content for redistribution** (SMS fan-out to
     registered farmers, ward-targeted). This is the version of the
     product that actually works — DHM's authoritative content, reaching
     farmers through Kisan Bazar's co-op/ward-targeted channel that DHM
     itself doesn't have.
  2. **If that isn't granted**, the app and IVR should say plainly "for
     official severe weather warnings, call 1155" rather than presenting
     any DHM-sourced content Kisan Bazar can't legitimately obtain.
     Kisan Bazar's own value-add in that case shrinks to the
     locally-relevant framing (which crop, which ward, which co-op
     pickup is affected) layered on Open-Meteo's supplementary forecast
     — real value, just not the same as redistributing DHM's own
     severity-classified bulletins.

## What doesn't change

Everything downstream of "a weather_bulletins row exists" is unaffected —
the schema (`weather_bulletins`, `alert_deliveries`, the `pending_alert_fanout`
view), the SMS/USSD `MAUSAM` command, and the IVR weather branch all still
work the same way regardless of whether the row was scraped, licensed via
the data-service application, or entered manually by an ops person reading
DHM's bulletin page by hand during the first weeks of the pilot — which,
honestly, given the above, is the realistic starting point before any
formal data-sharing agreement is in place.

## Immediate next step

Before writing any more integration code against DHM: submit the
`dhm.gov.np/data-service` request, framed as an NGO/service seeking to
redistribute public safety bulletins to farmers in underserved wards, and
ask directly whether programmatic (API/feed) access is available for that
use case or only the manual document-request form shown. The answer to
that question determines whether the ingestion layer is a scheduled API
poll or, for the pilot phase, a person reading a webpage each morning.
