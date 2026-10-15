-- 004_listings.sql
-- The aggregation core: individual farmer listings pool into co-op lots.

CREATE TABLE listings (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    farmer_id               uuid NOT NULL REFERENCES users(id),
    coop_id                 uuid REFERENCES cooperatives(id),
    crop_id                 int NOT NULL REFERENCES crops(id),
    grade                   text NOT NULL,
    quantity_kg             numeric(8,2) NOT NULL CHECK (quantity_kg > 0),
    quantity_committed_kg   numeric(8,2) NOT NULL DEFAULT 0
                                CHECK (quantity_committed_kg >= 0),
    asking_rate             numeric(10,2),
    photo_url               text,
    location                geography(Point,4326) NOT NULL,
    status                  text NOT NULL DEFAULT 'open' CHECK (status IN
                                ('open','partially_pooled','pooled','withdrawn','expired')),
    created_via             text NOT NULL DEFAULT 'app' CHECK (created_via IN
                                ('app','sms','ivr','coop_staff')),
    created_at              timestamptz NOT NULL DEFAULT now(),
    expires_at              timestamptz NOT NULL,
    CHECK (quantity_committed_kg <= quantity_kg)
);
CREATE INDEX listings_location_gix ON listings USING gist (location);
CREATE INDEX listings_open_idx ON listings (coop_id, crop_id, grade)
    WHERE status IN ('open','partially_pooled');
CREATE INDEX listings_farmer_idx ON listings (farmer_id, created_at DESC);

-- Auto-expire listings based on the crop's shelf life. Called by a scheduled job
-- (pg_cron or an external worker) — kept as a function so both can call it.
CREATE OR REPLACE FUNCTION expire_stale_listings() RETURNS int AS $$
    WITH expired AS (
        UPDATE listings
        SET status = 'expired'
        WHERE status IN ('open','partially_pooled')
          AND expires_at < now()
        RETURNING id
    )
    SELECT count(*)::int FROM expired;
$$ LANGUAGE sql;

CREATE TABLE lots (
    id                text PRIMARY KEY,          -- human code, e.g. 'KVR-118'
    coop_id           uuid NOT NULL REFERENCES cooperatives(id),
    crop_id           int NOT NULL REFERENCES crops(id),
    grade             text NOT NULL,
    target_kg         numeric(8,2),
    pooled_kg         numeric(8,2) NOT NULL DEFAULT 0,
    rate_per_kg       numeric(10,2),
    status            text NOT NULL DEFAULT 'forming' CHECK (status IN
                        ('forming','ready','routed','collected','delivered','settled','cancelled')),
    collection_date   date,
    confirmed_by      uuid REFERENCES users(id),  -- co-op staff who priced/confirmed it
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER lots_touch BEFORE UPDATE ON lots
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE lot_listings (
    lot_id          text REFERENCES lots(id) ON DELETE CASCADE,
    listing_id      uuid REFERENCES listings(id),
    kg_contributed  numeric(8,2) NOT NULL CHECK (kg_contributed > 0),
    weighed_kg      numeric(8,2),                 -- filled in at physical pickup
    PRIMARY KEY (lot_id, listing_id)
);

CREATE TABLE orders (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    buyer_id            uuid NOT NULL REFERENCES users(id),
    lot_id              text NOT NULL REFERENCES lots(id),
    quantity_kg         numeric(8,2) NOT NULL CHECK (quantity_kg > 0),
    unit_rate           numeric(10,2) NOT NULL,
    delivery_address    text,
    delivery_location   geography(Point,4326),
    status              text NOT NULL DEFAULT 'placed' CHECK (status IN
                            ('placed','confirmed','out_for_delivery','delivered','cancelled')),
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX orders_lot_idx ON orders (lot_id);
CREATE INDEX orders_buyer_idx ON orders (buyer_id, created_at DESC);

CREATE TABLE routes (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    coop_id       uuid REFERENCES cooperatives(id),
    driver_id     uuid REFERENCES users(id),
    vehicle_desc  text,
    route_date    date NOT NULL,
    status        text NOT NULL DEFAULT 'planned' CHECK (status IN
                        ('planned','in_progress','completed'))
);

CREATE TABLE route_stops (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    route_id      uuid REFERENCES routes(id) ON DELETE CASCADE,
    lot_id        text REFERENCES lots(id),
    farmer_id     uuid REFERENCES users(id),
    stop_order    int NOT NULL,
    planned_time  time,
    actual_time   timestamptz,
    location      geography(Point,4326),
    UNIQUE (route_id, stop_order)
);
