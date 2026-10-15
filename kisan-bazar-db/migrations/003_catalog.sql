-- 003_catalog.sql
-- Crops, land types, and the sourced crop-calendar rules the advisory engine reads.

CREATE TABLE crops (
    id                  serial PRIMARY KEY,
    name_ne             text NOT NULL,
    name_en             text NOT NULL,
    category            text NOT NULL CHECK (category IN
                            ('cereal','vegetable','cash_crop','fruit','pulse')),
    unit                text NOT NULL DEFAULT 'kg',
    shelf_life_hours    int NOT NULL,          -- drives listing expiry + collection urgency
    grades              text[] NOT NULL DEFAULT ARRAY['A','B','C']
);

CREATE TABLE land_types (
    code    text PRIMARY KEY,                  -- khet / bari / pakho / balaute
    name_ne text NOT NULL,
    name_en text NOT NULL,
    notes_ne text,
    notes_en text
);

-- Crop calendar: sourced, versioned, reviewable. Never write to this table
-- without a `source` that a human can go verify.
CREATE TABLE crop_calendar_rules (
    id              serial PRIMARY KEY,
    crop_id         int NOT NULL REFERENCES crops(id),
    elevation_band  text NOT NULL CHECK (elevation_band IN
                        ('terai','inner_terai','midhill','highhill','mountain')),
    land_type_code  text NOT NULL REFERENCES land_types(code),
    sow_months_bs   int[] NOT NULL,             -- 0=Baisakh .. 11=Chaitra
    duration_days   int,                        -- null for perennials
    why_ne          text NOT NULL,
    why_en          text NOT NULL,
    caution_ne      text,
    caution_en      text,
    source          text NOT NULL,              -- e.g. 'NARC 2081 crop calendar v3'
    source_url      text,
    reviewed_at     date NOT NULL,
    active          boolean NOT NULL DEFAULT true,
    CHECK (array_length(sow_months_bs,1) > 0)
);
CREATE INDEX ccr_lookup_idx ON crop_calendar_rules (elevation_band, land_type_code)
    WHERE active;

COMMENT ON TABLE crop_calendar_rules IS
    'Advisory content only. Every row must trace to a NARC/DoA source and carry a review date.
     App-layer recommendations must be phrased as options with tradeoffs, never instructions.';
