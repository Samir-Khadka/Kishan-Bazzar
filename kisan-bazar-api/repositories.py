"""
Data-access layer for the Kisan Bazar API — the repository seam.

Until now every route handler opened its own connection and wrote SQL
inline: 86 conn.execute call sites across four modules, with every
caller holding its own copy of the schema. That is exactly how the
assistant's ledger query rotted — it named columns that don't exist
(orders.listing_id, orders.rate_per_kg), nothing owned the schema
knowledge, and the failure was invisible until someone audited it.

This module owns the queries for the money/identity-critical domains
(lots, offers, orders-adjacent reads, farmers, prices, diagnoses,
rentals). Handlers and the assistant call these functions instead of
writing SQL; the schema has exactly one place to be right, and tests
can exercise the queries directly at this seam. It is also where the
SQLite dialect lives — a future Postgres port rewrites this file (and
db.py), not every route.

Functions take an open sqlite3 connection (row_factory=Row, see
db.get_db) and never commit: transaction control belongs to the
caller, because the stock invariant (availability.py) must compose
several of these reads/writes inside one BEGIN IMMEDIATE block.
"""


def get_farmer(conn, phone):
    return conn.execute(
        "SELECT * FROM farmers WHERE phone=?", (phone,)).fetchone()


def farmer_is_staff(conn, phone):
    row = conn.execute(
        "SELECT is_staff FROM farmers WHERE phone=?", (phone,)).fetchone()
    return bool(row and row["is_staff"])


def get_lot(conn, lot_id):
    return conn.execute(
        "SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()


def get_offer(conn, offer_id):
    return conn.execute(
        "SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()


def get_rental(conn, rental_id):
    return conn.execute(
        "SELECT * FROM rentals WHERE id=?", (rental_id,)).fetchone()


def get_booking(conn, booking_id):
    """A rental booking joined with its rental (for owner checks)."""
    return conn.execute(
        "SELECT b.*, r.owner_phone AS rental_owner_phone, "
        "r.machine_name AS machine_name "
        "FROM rental_bookings b JOIN rentals r ON r.id = b.rental_id "
        "WHERE b.id=?", (booking_id,)).fetchone()


def overlapping_booking(conn, rental_id, book_date, days):
    """An active (requested/confirmed) booking for this machine whose
    [book_date, book_date+days) range intersects the requested range,
    or None. Cancelled/completed bookings free their dates."""
    return conn.execute(
        "SELECT * FROM rental_bookings "
        "WHERE rental_id=? AND status IN ('requested','confirmed') "
        "AND date(book_date) < date(?, '+' || ? || ' days') "
        "AND date(?) < date(book_date, '+' || days || ' days') "
        "ORDER BY book_date LIMIT 1",
        (rental_id, book_date, days, book_date),
    ).fetchone()


def farmer_pending_due(conn, phone):
    """Rupees owed to a farmer for produce already ordered from lots
    they contributed to.

    The money path is listings -> lot_listings -> lots -> orders: a
    farmer pools kg_contributed of a listing into a lot; buyers order
    against the lot at the order's unit_rate. The farmer's share of
    each order is prorated by their fraction of the lot's pooled
    kilos (kg_contributed / pooled_kg) — the same fraction of every
    kilo sold is theirs. Orders carry no 'paid' state in this schema
    (status is 'placed'), so every placed order counts as pending;
    if a payment state is added later, filter on it here — this is
    the only place that knows.
    """
    rows = conn.execute(
        "SELECT o.quantity_kg AS qty, o.unit_rate AS rate, "
        "ll.kg_contributed AS contrib, lot.pooled_kg AS pooled "
        "FROM orders o "
        "JOIN lots lot ON lot.id = o.lot_id "
        "JOIN lot_listings ll ON ll.lot_id = lot.id "
        "JOIN listings li ON li.id = ll.listing_id "
        "WHERE li.farmer_phone=?",
        (phone,),
    ).fetchall()
    due = 0.0
    for r in rows:
        pooled = r["pooled"] or 0
        if pooled <= 0:
            continue
        due += r["qty"] * r["rate"] * (r["contrib"] / pooled)
    return due


def recent_lot_rates(conn, crop_code, limit=2):
    """Newest-first confirmed lot rates for a crop (rate_per_kg set).
    Two rows are enough to compute an honest price change; one row
    means 'no earlier rate to compare against'."""
    rows = conn.execute(
        "SELECT rate_per_kg FROM lots WHERE crop_code=? "
        "AND rate_per_kg IS NOT NULL "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (crop_code, limit),
    ).fetchall()
    return [r["rate_per_kg"] for r in rows]


def diagnoses_for_farmer(conn, phone, limit=20):
    return conn.execute(
        "SELECT * FROM diagnoses WHERE farmer_phone=? "
        "ORDER BY created_at DESC LIMIT ?",
        (phone, limit),
    ).fetchall()
