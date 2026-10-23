"""
The aggregation engine: turns many small farmer listings into one
co-op lot once a real target is reached. This is the piece that makes
the difference between "a listings app" and something that actually
solves Nepal's fragmented-landholding problem (see backend architecture
doc §3 for the full design rationale).

Kept as plain functions over a sqlite3 connection, not a class, so the
logic is easy to read start to finish and easy to port onto the real
Postgres schema later — every function here has an obvious PostGIS
equivalent (haversine_km -> ST_DWithin / ST_Distance).
"""
import math
import uuid


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km. Good enough at Nepal's scale — no need
    for anything fancier than the standard haversine formula here."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def open_listings_in_radius(conn, coop_id, crop_code, grade):
    """All open/partially_pooled listings for a crop+grade, filtered to
    farmers within that co-op's collection radius. Mirrors the WHERE
    ST_DWithin(...) clause the real Postgres version would use."""
    coop = conn.execute("SELECT * FROM cooperatives WHERE id=?", (coop_id,)).fetchone()
    if not coop:
        return []
    rows = conn.execute(
        """SELECT l.*, f.lat AS farmer_lat, f.lon AS farmer_lon, f.name AS farmer_name
           FROM listings l JOIN farmers f ON f.phone = l.farmer_phone
           WHERE l.coop_id=? AND l.crop_code=? AND l.grade=?
             AND l.status IN ('open','partially_pooled')""",
        (coop_id, crop_code, grade),
    ).fetchall()
    in_range = []
    for r in rows:
        dist = haversine_km(coop["lat"], coop["lon"], r["farmer_lat"], r["farmer_lon"])
        if dist <= coop["collection_radius_km"]:
            in_range.append(r)
    return in_range


def check_and_form_lot(conn, coop_id, crop_code, grade, target_kg):
    """
    The core threshold check, run after every new listing (and safe to
    re-run any time — it's idempotent: listings already fully committed
    to a lot are excluded by the status filter above).

    Returns the lot dict if a new lot was formed this call, else None.
    Partial pooling (threshold not yet reached) just leaves listings as
    'open' — nothing to return, nothing to notify yet.

    The whole read-modify-write runs under BEGIN IMMEDIATE when the
    caller isn't already holding a transaction: two listings landing
    in the same instant used to both see the same uncommitted kilos,
    both decide the threshold was crossed, and double-commit them
    until the listings CHECK constraint turned the second one into a
    500. Under the write lock the second caller re-reads the first
    caller's committed state and correctly sees the pool as drained.
    """
    started_here = not conn.in_transaction
    if started_here:
        conn.execute("BEGIN IMMEDIATE")
    try:
        result = _form_lot_locked(conn, coop_id, crop_code, grade, target_kg)
    except Exception:
        if started_here:
            conn.rollback()
        raise
    if started_here:
        conn.commit()
    return result


def _form_lot_locked(conn, coop_id, crop_code, grade, target_kg):
    available = open_listings_in_radius(conn, coop_id, crop_code, grade)
    pooled_so_far = sum(r["quantity_kg"] - r["quantity_committed_kg"] for r in available)
    if pooled_so_far < target_kg:
        return None  # not enough yet — hold, matches "forming" state implicitly

    lot_id = _next_lot_code(conn, coop_id)
    conn.execute(
        "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, pooled_kg, status) "
        "VALUES (?,?,?,?,?,0,'forming')",
        (lot_id, coop_id, crop_code, grade, target_kg),
    )

    remaining_needed = target_kg
    contributions = []
    for r in available:
        if remaining_needed <= 0:
            break
        free_kg = r["quantity_kg"] - r["quantity_committed_kg"]
        take = min(free_kg, remaining_needed)
        conn.execute(
            "INSERT INTO lot_listings (lot_id, listing_id, kg_contributed) VALUES (?,?,?)",
            (lot_id, r["id"], take),
        )
        new_committed = r["quantity_committed_kg"] + take
        new_status = "pooled" if new_committed >= r["quantity_kg"] else "partially_pooled"
        conn.execute(
            "UPDATE listings SET quantity_committed_kg=?, status=? WHERE id=?",
            (new_committed, new_status, r["id"]),
        )
        remaining_needed -= take
        contributions.append({"farmer": r["farmer_name"], "kg": take})

    pooled_total = target_kg - remaining_needed
    conn.execute("UPDATE lots SET pooled_kg=? WHERE id=?", (pooled_total, lot_id))
    conn.commit()

    return {
        "lot_id": lot_id,
        "coop_id": coop_id,
        "crop_code": crop_code,
        "grade": grade,
        "pooled_kg": pooled_total,
        "contributions": contributions,
    }


def _next_lot_code(conn, coop_id):
    """Human-readable lot codes like the prototype's 'KVR-118' — a short
    prefix derived from the co-op id plus a running count for that co-op.
    COUNT(*) alone can name a code that already exists (a deleted lot
    leaves a gap the count reuses), so walk upward from the count until
    the code is actually free — callers hold the write lock, so the
    check can't race."""
    prefix = "".join(ch for ch in coop_id.upper() if ch.isalpha())[:3] or "LOT"
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM lots WHERE coop_id=?", (coop_id,)
    ).fetchone()["n"]
    n = 100 + count + 1
    while conn.execute("SELECT 1 FROM lots WHERE id=?", (f"{prefix}-{n}",)).fetchone():
        n += 1
    return f"{prefix}-{n}"
