# Kisan Bazar — database

Runnable PostgreSQL + PostGIS schema for the aggregation-first marketplace,
plus seed data for a pilot corridor (Kavre/Dhading → Kathmandu) that mirrors
the screens in the app prototype and the backend design doc.

**Not yet run against a live database in this environment** (no network
access to pull the Postgres/PostGIS image here) — run it yourself with the
steps below before trusting it in anything real.

## Run it

```bash
docker compose up -d
# wait a few seconds for postgis to initialize, then:

for f in migrations/*.sql; do
  docker compose exec -T db psql -U kisan -d kisan_bazar -f "/${f}"
done

docker compose exec -T db psql -U kisan -d kisan_bazar -f /seed/001_seed.sql

# sanity check — should print aggregation totals, advisory rows, etc.
docker compose exec -T db psql -U kisan -d kisan_bazar -f /seed/002_demo_queries.sql
```

Or without Docker, against any Postgres 15+ with PostGIS installed:

```bash
createdb kisan_bazar
psql kisan_bazar -c "CREATE EXTENSION IF NOT EXISTS postgis;"
for f in migrations/*.sql; do psql kisan_bazar -f "$f"; done
psql kisan_bazar -f seed/001_seed.sql
psql kisan_bazar -f seed/002_demo_queries.sql
```

## Layout

```
migrations/
  001_extensions.sql   postgis, pgcrypto, updated_at trigger helper
  002_identity.sql     users, wards, cooperatives, membership
  003_catalog.sql      crops, land types, sourced crop-calendar rules
  004_listings.sql     the aggregation core — listings, lots, orders, routes
  005_payments.sql     farmer payouts, decoupled from buyer collection
  006_weather.sql      DHM-style bulletins + ward-based alert fan-out
  007_land_scans.sql   Photo+quiz land-scan history, syncs users.land_type
                        (not yet run against live Postgres — see caveat below;
                        the SQLite version in ../kisan-bazar-api/db.py *is*
                        tested and is the source of truth this was ported from)
seed/
  001_seed.sql         Kavre/Dhading corridor: wards, 2 co-ops, 4 farmers,
                        listings that cross the aggregation threshold,
                        one active weather bulletin
  002_demo_queries.sql read-only queries mirroring the app's screens
```

## What to check once it's running

- Query 2 in `002_demo_queries.sql` should show cauliflower at 90kg pooled
  (38 + 52 from two farmers) against grade A, and 44kg for the fourth
  farmer's separate grade-A row if inserted after — adjust the threshold
  constant to match whatever vehicle size you're piloting with.
- Query 4 should return the four Kavre-ward farmers as pending SMS/push
  deliveries for the seeded rain warning.
- If you add `pg_cron`, schedule `SELECT expire_stale_listings();` hourly
  and `REFRESH` isn't needed for the two views — they're plain views, not
  materialized, so they're always live.

## Known gaps before this is production-ready

- No row-level security yet — a farmer can currently query any other
  farmer's listings via the API layer if the API doesn't enforce scoping.
  Add RLS policies keyed on `auth.uid() = farmer_id` equivalent once the
  auth layer is chosen.
- Ward boundaries here are centroids only, not real polygons — pull actual
  boundary geometry from the Survey Department / OpenStreetMap before
  launch; ward-based alert fan-out is only as good as this data.
- No migration tool (Alembic/Flyway/sqlx) wired up yet — files are plain
  numbered SQL, meant to be run in order by hand or wrapped by whichever
  tool the backend framework prefers.
