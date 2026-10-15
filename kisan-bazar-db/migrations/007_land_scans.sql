-- 007_land_scans.sql
-- Persists the app's photo + touch/drainage-quiz land scan (see the
-- prototype's "फोटोबाट जाँच्नुहोस्" flow). Mirrors the SQLite version in
-- ../../kisan-bazar-api/db.py, which was built and tested first — port
-- that logic here, don't re-derive it.

ALTER TABLE users ADD COLUMN IF NOT EXISTS land_type text
    CHECK (land_type IN ('khet','bari','pakho','balaute') OR land_type IS NULL);

CREATE TABLE land_scans (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    farmer_id       uuid NOT NULL REFERENCES users(id),
    land_type       text NOT NULL CHECK (land_type IN ('khet','bari','pakho','balaute')),
    confidence      text NOT NULL CHECK (confidence IN ('good','ok')),
    color_key       text CHECK (color_key IN ('dark','red','pale','mixed',NULL)),
    texture         text CHECK (texture IN ('clay','sandy','mixed',NULL)),
    drainage        text CHECK (drainage IN ('khet','fast','sometimes',NULL)),
    had_photo       boolean NOT NULL DEFAULT false,
        -- the photo itself is never stored server-side, only the derived
        -- signals above — no image data, no consent/storage burden
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX land_scans_farmer_idx ON land_scans (farmer_id, created_at DESC);

COMMENT ON TABLE land_scans IS
    'Full scan history per farmer — users.land_type holds only the current
     value. A farmer rescanning overwrites users.land_type but the prior
     scan stays here, so a co-op field agent can see how a read changed.';

-- Trigger keeping users.land_type in sync with the most recent scan,
-- mirroring what the Flask API does explicitly in save_land_scan() —
-- kept here too since Postgres writes may not always go through that API
-- (a co-op staff bulk-import, for instance).
CREATE OR REPLACE FUNCTION sync_farmer_land_type() RETURNS trigger AS $$
BEGIN
    UPDATE users SET land_type = NEW.land_type WHERE id = NEW.farmer_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER land_scans_sync AFTER INSERT ON land_scans
    FOR EACH ROW EXECUTE FUNCTION sync_farmer_land_type();
