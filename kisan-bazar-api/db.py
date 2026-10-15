"""
SQLite schema for the Kisan Bazar API prototype.

This is a deliberately simplified stand-in for the PostgreSQL + PostGIS
schema in ../kisan-bazar-db/migrations/ — same entities, same core logic,
but plain lat/lon floats and a Python haversine distance function instead
of PostGIS geography types, since SQLite has no spatial extension by
default. Swapping this for the real Postgres schema later shouldn't
change any of the business logic in aggregation.py — only db.py's SQL
and the distance calculation change.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "kisan_bazar.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cooperatives (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    collection_radius_km REAL NOT NULL DEFAULT 15,
    commission_pct  REAL NOT NULL DEFAULT 8.0
);

CREATE TABLE IF NOT EXISTS farmers (
    phone           TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    coop_id         TEXT REFERENCES cooperatives(id),
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    payout_method   TEXT NOT NULL DEFAULT 'cash',
    land_type       TEXT,          -- khet | bari | pakho | balaute | NULL if never set
    is_staff        INTEGER NOT NULL DEFAULT 0   -- 0/1 — co-op staff can confirm lots; farmers cannot
);

CREATE TABLE IF NOT EXISTS crops (
    code            TEXT PRIMARY KEY,
    name_ne         TEXT NOT NULL,
    name_en         TEXT NOT NULL,
    shelf_life_hours INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    farmer_phone    TEXT NOT NULL REFERENCES farmers(phone),
    coop_id         TEXT NOT NULL REFERENCES cooperatives(id),
    crop_code       TEXT NOT NULL REFERENCES crops(code),
    grade           TEXT NOT NULL,
    quantity_kg     REAL NOT NULL,
    quantity_committed_kg REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'open',
        -- open | partially_pooled | pooled | withdrawn | expired
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (quantity_committed_kg <= quantity_kg)
);

CREATE TABLE IF NOT EXISTS lots (
    id              TEXT PRIMARY KEY,
    coop_id         TEXT NOT NULL REFERENCES cooperatives(id),
    crop_code       TEXT NOT NULL REFERENCES crops(code),
    grade           TEXT NOT NULL,
    target_kg       REAL NOT NULL,
    pooled_kg       REAL NOT NULL DEFAULT 0,
    rate_per_kg     REAL,
    status          TEXT NOT NULL DEFAULT 'forming',
        -- forming | ready | routed | collected | delivered | settled
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lot_listings (
    lot_id          TEXT NOT NULL REFERENCES lots(id),
    listing_id      INTEGER NOT NULL REFERENCES listings(id),
    kg_contributed  REAL NOT NULL,
    PRIMARY KEY (lot_id, listing_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_name      TEXT NOT NULL,
    lot_id          TEXT NOT NULL REFERENCES lots(id),
    quantity_kg     REAL NOT NULL,
    unit_rate       REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'placed',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every scan a farmer runs is kept, not just the latest — a co-op field
-- agent revisiting a farmer's plot, or the farmer rescanning after a
-- season, should be able to see how the read changed, not just overwrite
-- history. farmers.land_type (above) holds the currently-confirmed value;
-- this table is the full audit trail behind it.
CREATE TABLE IF NOT EXISTS land_scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    farmer_phone    TEXT NOT NULL REFERENCES farmers(phone),
    land_type       TEXT NOT NULL CHECK (land_type IN ('khet','bari','pakho','balaute')),
    confidence      TEXT NOT NULL CHECK (confidence IN ('good','ok')),
    color_key       TEXT,           -- dark | red | pale | mixed | NULL if no photo given
    texture         TEXT,           -- clay | sandy | mixed
    drainage        TEXT,           -- khet | fast | sometimes
    had_photo       INTEGER NOT NULL DEFAULT 0,   -- 0/1 — the photo itself is never stored server-side
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS land_scans_farmer_idx ON land_scans (farmer_phone, created_at DESC);

-- Phone-verification codes for login. A code is single-use (consumed_at
-- set once redeemed) and short-lived (expires_at). Codes are stored
-- hashed, not in plaintext — even a full database read doesn't hand an
-- attacker working codes, only proof a hash matches after the fact.
CREATE TABLE IF NOT EXISTS auth_codes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    phone           TEXT NOT NULL,
    code_hash       TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT NOT NULL,
    consumed_at     TEXT
);
CREATE INDEX IF NOT EXISTS auth_codes_phone_idx ON auth_codes (phone, created_at DESC);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(fresh: bool = False):
    if fresh and DB_PATH.exists():
        DB_PATH.unlink()
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
