-- 002_identity.sql
-- Users, wards, cooperatives, membership.

CREATE TABLE wards (
    id              serial PRIMARY KEY,
    district        text NOT NULL,
    province        text NOT NULL,
    municipality    text NOT NULL,
    ward_no         int NOT NULL,
    boundary        geography(Polygon,4326),
    centroid        geography(Point,4326),
    elevation_band  text NOT NULL CHECK (elevation_band IN
                        ('terai','inner_terai','midhill','highhill','mountain')),
    river_basin     text CHECK (river_basin IN
                        ('koshi','bagmati','gandaki','karnali','mahakali',NULL)),
    UNIQUE (district, municipality, ward_no)
);
CREATE INDEX wards_boundary_gix ON wards USING gist (boundary);
CREATE INDEX wards_centroid_gix ON wards USING gist (centroid);

CREATE TABLE users (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phone           text UNIQUE NOT NULL,
    full_name       text NOT NULL,
    role            text NOT NULL CHECK (role IN
                        ('farmer','buyer','coop_staff','driver','admin')),
    preferred_lang  text NOT NULL DEFAULT 'ne'
                        CHECK (preferred_lang IN ('ne','en','mai','bho','awa','thr')),
    preferred_channel text NOT NULL DEFAULT 'app'
                        CHECK (preferred_channel IN ('app','sms','ivr')),
    ward_id         int REFERENCES wards(id),
    location        geography(Point,4326),
    payout_method   text NOT NULL DEFAULT 'cash'
                        CHECK (payout_method IN ('esewa','khalti','fonepay','cash')),
    payout_handle   text,                    -- wallet id / phone for the payout method
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    is_active       boolean NOT NULL DEFAULT true
);
CREATE INDEX users_location_gix ON users USING gist (location);
CREATE INDEX users_ward_idx ON users (ward_id);
CREATE TRIGGER users_touch BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE cooperatives (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL,
    registration_no text,
    ward_id         int REFERENCES wards(id),
    location        geography(Point,4326) NOT NULL,
    phone           text,
    collection_radius_m int NOT NULL DEFAULT 15000,
    commission_pct  numeric(4,2) NOT NULL DEFAULT 8.00 CHECK (commission_pct BETWEEN 0 AND 25),
    default_payout_method text NOT NULL DEFAULT 'esewa'
                        CHECK (default_payout_method IN ('esewa','khalti','fonepay','cash')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    is_active       boolean NOT NULL DEFAULT true
);
CREATE INDEX coops_location_gix ON cooperatives USING gist (location);

CREATE TABLE coop_members (
    coop_id     uuid REFERENCES cooperatives(id) ON DELETE CASCADE,
    farmer_id   uuid REFERENCES users(id) ON DELETE CASCADE,
    joined_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (coop_id, farmer_id)
);
CREATE INDEX coop_members_farmer_idx ON coop_members (farmer_id);

COMMENT ON COLUMN cooperatives.collection_radius_m IS
    'How far the co-op will pool listings from, in meters. Wider in hills where roads are sparse.';
