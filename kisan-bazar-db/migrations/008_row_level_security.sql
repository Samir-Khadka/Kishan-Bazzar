-- 008_row_level_security.sql
-- Defense-in-depth for Postgres: even if application-layer authorization
-- in the API has a bug, the database itself refuses to return or modify
-- another farmer's rows. This mirrors the auth model built and tested
-- against the SQLite API (../../kisan-bazar-api/auth.py + app.py's
-- require_self/require_staff) — port that model here, don't re-derive it.
--
-- NOT independently tested against a live Postgres instance in this
-- environment (same standing caveat as the rest of this migration set —
-- see ../README.md). This is a reasonable, standard RLS pattern, but
-- treat it as reviewed-by-design, not verified-by-execution, until
-- someone runs it against a real database.
--
-- Assumes the API sets a session variable per request identifying the
-- authenticated caller, e.g.:
--   SET LOCAL app.current_phone = '9841000001';
--   SET LOCAL app.current_is_staff = 'false';
-- immediately after opening the connection for that request, using the
-- same phone/is_staff the bearer token already resolved to — RLS is a
-- second lock on the same door, not a separate identity system.

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE listings ENABLE ROW LEVEL SECURITY;
ALTER TABLE land_scans ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments ENABLE ROW LEVEL SECURITY;
ALTER TABLE lots ENABLE ROW LEVEL SECURITY;

-- Staff bypass: co-op staff need to see across farmers to do their job
-- (confirming lots, running routes). Everyone else is scoped to themself.
CREATE POLICY users_self_or_staff ON users
    USING (phone = current_setting('app.current_phone', true)
           OR current_setting('app.current_is_staff', true) = 'true');

CREATE POLICY listings_self_or_staff ON listings
    USING (farmer_id IN (SELECT id FROM users WHERE phone = current_setting('app.current_phone', true))
           OR current_setting('app.current_is_staff', true) = 'true')
    WITH CHECK (farmer_id IN (SELECT id FROM users WHERE phone = current_setting('app.current_phone', true)));
    -- USING governs what's visible; WITH CHECK governs what can be
    -- written — deliberately narrower: staff can SEE all listings (they
    -- need to, to build lots) but a listing can only ever be INSERTed or
    -- UPDATEd as the authenticated farmer's own, staff included. A staff
    -- account being compromised should not mean it can forge listings
    -- under an arbitrary farmer's name.

CREATE POLICY land_scans_self_or_staff ON land_scans
    USING (farmer_id IN (SELECT id FROM users WHERE phone = current_setting('app.current_phone', true))
           OR current_setting('app.current_is_staff', true) = 'true')
    WITH CHECK (farmer_id IN (SELECT id FROM users WHERE phone = current_setting('app.current_phone', true)));

CREATE POLICY payments_self_or_staff ON payments
    USING (farmer_id IN (SELECT id FROM users WHERE phone = current_setting('app.current_phone', true))
           OR current_setting('app.current_is_staff', true) = 'true');
    -- no WITH CHECK: payments are only ever written by a trusted backend
    -- payout job, never directly by a farmer or staff request — RLS here
    -- is purely about read scoping (a farmer must never see another
    -- farmer's payout amounts), not write authorization.

CREATE POLICY lots_staff_write_all_read ON lots
    USING (true)   -- lots are co-op-level, not farmer-private — anyone
                    -- authenticated can see lot status (this matches the
                    -- API's GET /v1/lots and /v1/market/lots having no
                    -- per-farmer scoping either)
    WITH CHECK (current_setting('app.current_is_staff', true) = 'true');
    -- but only staff can ever INSERT/UPDATE a lot (form it, price it,
    -- move its status) — matches @require_staff on POST /v1/lots/<id>/confirm

-- A role that owns nothing and only carries whatever the RLS policies
-- above grant — the API's database connection should authenticate as
-- this role, never as a superuser or the table owner, both of which
-- bypass RLS entirely regardless of any policy defined above.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kisan_bazar_api') THEN
        CREATE ROLE kisan_bazar_api LOGIN;
    END IF;
END
$$;
GRANT SELECT, INSERT, UPDATE ON users, listings, land_scans, payments, lots TO kisan_bazar_api;
