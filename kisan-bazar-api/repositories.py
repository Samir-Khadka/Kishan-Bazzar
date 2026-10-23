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
    """Rupees still owed to a farmer for produce ordered from lots
    they contributed to and NOT yet settled.

    The money path is listings -> lot_listings -> lots -> orders: a
    farmer pools kg_contributed of a listing into a lot; buyers order
    against the lot at the order's unit_rate. The farmer's share of
    each order is prorated by their fraction of the lot's pooled
    kilos (kg_contributed / pooled_kg) — the same fraction of every
    kilo sold is theirs. Settled orders (see order_settlements) no
    longer count here; farmer_dues() exposes both sides of the split.
    """
    return farmer_dues(conn, phone)["pending"]


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


# ------------------------------------------------------------------
# Settlements (residual: orders had no paid/settled state)
# ------------------------------------------------------------------

SETTLEMENT_METHODS = ("cash", "bank", "esewa", "khalti")


def get_order(conn, order_id):
    return conn.execute(
        "SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()


def get_settlement(conn, order_id):
    return conn.execute(
        "SELECT * FROM order_settlements WHERE order_id=?",
        (order_id,)).fetchone()


def insert_settlement(conn, order_id, amount, method, reference, settled_by):
    """Record a settlement. Caller holds the write lock and has
    already checked no settlement exists (the UNIQUE constraint is
    the backstop). Returns the stored row."""
    cur = conn.execute(
        "INSERT INTO order_settlements "
        "(order_id, amount, method, reference, settled_by) "
        "VALUES (?,?,?,?,?)",
        (order_id, amount, method, reference, settled_by))
    return conn.execute(
        "SELECT * FROM order_settlements WHERE id=?",
        (cur.lastrowid,)).fetchone()


def get_cooperative(conn, coop_id):
    return conn.execute(
        "SELECT * FROM cooperatives WHERE id=?", (coop_id,)).fetchone()


def get_crop(conn, crop_code):
    return conn.execute(
        "SELECT * FROM crops WHERE code=?", (crop_code,)).fetchone()


def farmer_dues(conn, phone):
    """A farmer's money position from real orders: {"pending": Rs,
    "settled": Rs}.

    Same prorated join as farmer_pending_due (listings ->
    lot_listings -> lots -> orders, share = kg_contributed /
    pooled_kg), split by whether the order has a settlement row.
    'pending' is what buyers' orders still owe the pool; 'settled'
    is the share of orders staff have recorded as paid.
    """
    rows = conn.execute(
        "SELECT o.quantity_kg AS qty, o.unit_rate AS rate, "
        "ll.kg_contributed AS contrib, lot.pooled_kg AS pooled, "
        "(s.id IS NOT NULL) AS is_settled "
        "FROM orders o "
        "JOIN lots lot ON lot.id = o.lot_id "
        "JOIN lot_listings ll ON ll.lot_id = lot.id "
        "JOIN listings li ON li.id = ll.listing_id "
        "LEFT JOIN order_settlements s ON s.order_id = o.id "
        "WHERE li.farmer_phone=?",
        (phone,),
    ).fetchall()
    dues = {"pending": 0.0, "settled": 0.0}
    for r in rows:
        pooled = r["pooled"] or 0
        if pooled <= 0:
            continue
        share = r["qty"] * r["rate"] * (r["contrib"] / pooled)
        dues["settled" if r["is_settled"] else "pending"] += share
    return dues


# ------------------------------------------------------------------
# Read models (residual: aggregates/joins that lived in handlers)
#
# These functions own the SELECTs behind the read endpoints. Handlers
# parse input, call one of these, and jsonify the result — the SQL
# for a screen lives in exactly one place, next to the schema it
# reads. Response shapes are the ones the endpoints have always
# returned; the suites pin them.
# ------------------------------------------------------------------

_ORDER_WITH_SETTLEMENT = (
    "o.id, o.buyer_name, o.buyer_phone, o.lot_id, o.quantity_kg, "
    "o.unit_rate, o.status, o.created_at, "
    "CASE WHEN s.id IS NULL THEN 'unpaid' ELSE 'settled' END "
    "AS payment_status, "
    "s.amount AS settled_amount, s.method AS settlement_method, "
    "s.reference AS settlement_reference, s.settled_by AS settled_by, "
    "s.settled_at AS settled_at "
    "FROM orders o "
    "LEFT JOIN order_settlements s ON s.order_id = o.id ")


def orders_for_buyer(conn, phone, limit=100):
    """A buyer's own orders, newest first, with payment status."""
    return conn.execute(
        f"SELECT {_ORDER_WITH_SETTLEMENT} WHERE o.buyer_phone=? "
        "ORDER BY o.id DESC LIMIT ?", (phone, limit)).fetchall()


def admin_overview(conn):
    """Everything the staff dashboard renders, in one read model.
    Row payloads are capped (the dashboard renders recent rows);
    totals come from COUNT(*)/SUM(*) so the KPI cards stay exact
    past the caps."""
    farmers = conn.execute(
        "SELECT phone, name, coop_id, lat, lon, payout_method, "
        "land_type, is_staff FROM farmers").fetchall()
    listings = conn.execute(
        "SELECT id, farmer_phone, coop_id, crop_code, grade, "
        "quantity_kg, quantity_committed_kg, status, created_at "
        "FROM listings ORDER BY id DESC LIMIT 50").fetchall()
    lots = conn.execute(
        "SELECT id, coop_id, crop_code, grade, target_kg, pooled_kg, "
        "rate_per_kg, status, created_at FROM lots "
        "ORDER BY created_at DESC LIMIT 200").fetchall()
    crops = conn.execute(
        "SELECT code, name_ne, name_en, shelf_life_hours "
        "FROM crops").fetchall()
    orders = conn.execute(
        f"SELECT {_ORDER_WITH_SETTLEMENT} "
        "ORDER BY o.id DESC LIMIT 200").fetchall()
    total_lots = conn.execute(
        "SELECT COUNT(*) AS c FROM lots").fetchone()["c"]
    total_orders = conn.execute(
        "SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
    total_listings = conn.execute(
        "SELECT COUNT(*) AS c FROM listings").fetchone()["c"]
    # total_kg aggregates the whole table: summing only the fetched
    # (capped) listings silently undercounted past 50 listings.
    total_kg = conn.execute(
        "SELECT COALESCE(SUM(quantity_kg), 0) AS s "
        "FROM listings").fetchone()["s"]
    pooled_kg = conn.execute(
        "SELECT COALESCE(SUM(pooled_kg), 0) AS s "
        "FROM lots").fetchone()["s"]
    return {
        "farmers": [dict(r) for r in farmers],
        "listings": [dict(r) for r in listings],
        "lots": [dict(r) for r in lots],
        "crops": [dict(r) for r in crops],
        "orders": [dict(r) for r in orders],
        "counts": {
            "farmers": len(farmers),
            "listings": total_listings,
            "lots": total_lots,
            "orders": total_orders,
            "total_kg": round(total_kg, 1),
            "pooled_kg": round(pooled_kg, 1),
        },
    }


def listings_for_farmer(conn, phone):
    return conn.execute(
        "SELECT * FROM listings WHERE farmer_phone=? "
        "ORDER BY created_at DESC", (phone,)).fetchall()


def lots_page(conn, coop_id, status, limit, offset):
    """One page of lots with co-op name + distinct contributing
    farmer count, and the unpaged total. farmer_count comes from one
    GROUP BY join (not a correlated subquery per row)."""
    where = "WHERE 1=1"
    params = []
    if coop_id:
        where += " AND l.coop_id=?"
        params.append(coop_id)
    if status:
        where += " AND l.status=?"
        params.append(status)
    total = conn.execute(
        f"SELECT COUNT(*) as c FROM lots l {where}", params).fetchone()["c"]
    rows = conn.execute(
        "SELECT l.*, c.name AS coop_name, "
        "COUNT(DISTINCT li.farmer_phone) AS farmer_count "
        "FROM lots l "
        "LEFT JOIN cooperatives c ON l.coop_id = c.id "
        "LEFT JOIN lot_listings ll ON ll.lot_id = l.id "
        "LEFT JOIN listings li ON ll.listing_id = li.id "
        f"{where} GROUP BY l.id "
        "ORDER BY l.created_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return rows, total


def lot_detail(conn, lot_id):
    """A single lot with co-op name + farmer count (the paginated
    list would wrongly 404 lots beyond its first page)."""
    return conn.execute(
        "SELECT l.*, c.name AS coop_name, "
        "(SELECT COUNT(DISTINCT li.farmer_phone) FROM lot_listings ll "
        " JOIN listings li ON ll.listing_id = li.id "
        " WHERE ll.lot_id = l.id) AS farmer_count "
        "FROM lots l LEFT JOIN cooperatives c ON l.coop_id = c.id "
        "WHERE l.id=?",
        (lot_id,),
    ).fetchone()


def market_ready_lots(conn, limit=200):
    """Buyer-facing ready lots with crop names, newest first, capped."""
    return conn.execute(
        "SELECT l.*, c.name_ne, c.name_en FROM lots l "
        "JOIN crops c ON c.code = l.crop_code "
        "WHERE l.status='ready' ORDER BY l.created_at DESC LIMIT ?",
        (limit,)).fetchall()


def price_trends(conn, crop_code, days):
    """Daily average confirmed lot rates per crop, as the by-crop
    dict the endpoint returns. Aggregation happens in SQL (SUM/COUNT
    per crop-day); only the final round() is Python's, preserving the
    half-to-even rounding the endpoint has always produced. Crops
    appear in the order of their first lot activity in the window."""
    q = ("SELECT crop_code, date(created_at) AS d, "
         "SUM(rate_per_kg) AS s, COUNT(*) AS n, MIN(created_at) AS first_seen "
         "FROM lots WHERE rate_per_kg IS NOT NULL "
         "AND created_at >= date('now', ?)")
    params = [f"-{days} days"]
    if crop_code:
        q += " AND crop_code=?"; params.append(crop_code)
    q += " GROUP BY crop_code, d ORDER BY first_seen, crop_code, d"
    rows = conn.execute(q, params).fetchall()
    by_crop = {}
    for r in rows:
        by_crop.setdefault(r["crop_code"], []).append({
            "date": r["d"],
            "avg_rate": round(r["s"] / r["n"], 1),
            "lots": r["n"],
        })
    return by_crop


def latest_prices(conn):
    """Latest ready-lot rate per crop, one DB row per crop (window
    function) instead of walking every rated lot in Python."""
    rows = conn.execute(
        "SELECT crop_code, rate_per_kg, pooled_kg, date(created_at) AS d FROM ("
        "  SELECT *, ROW_NUMBER() OVER ("
        "    PARTITION BY crop_code ORDER BY created_at DESC, id DESC"
        "  ) AS rn FROM lots "
        "  WHERE rate_per_kg IS NOT NULL AND status='ready'"
        ") WHERE rn = 1 ORDER BY crop_code").fetchall()
    return {r["crop_code"]: dict(r) for r in rows}


def public_stats(conn):
    """Public aggregate stats for the website. No PII."""
    return {
        "farmers": conn.execute(
            "SELECT COUNT(*) as c FROM farmers WHERE is_staff=0"
        ).fetchone()["c"],
        "coops": conn.execute(
            "SELECT COUNT(*) as c FROM cooperatives").fetchone()["c"],
        "active_lots": conn.execute(
            "SELECT COUNT(*) as c FROM lots WHERE status='ready'"
        ).fetchone()["c"],
    }


def list_offers(conn, *, is_staff, phone, lot_id, buyer_phone):
    """Offers visible to the caller: staff see everything; everyone
    else only their own. Capped at the newest 200."""
    q = "SELECT * FROM offers WHERE 1=1"
    params = []
    if not is_staff:
        buyer_phone = phone
    if lot_id:
        q += " AND lot_id=?"; params.append(lot_id)
    if buyer_phone:
        q += " AND buyer_phone=?"; params.append(buyer_phone)
    q += " ORDER BY created_at DESC LIMIT 200"
    return conn.execute(q, params).fetchall()


def list_rentals(conn, machine_type, location):
    q = "SELECT * FROM rentals WHERE available=1"
    params = []
    if machine_type:
        q += " AND machine_type=?"; params.append(machine_type)
    if location:
        q += " AND location LIKE ?"; params.append(f"%{location}%")
    q += " ORDER BY created_at DESC"
    return conn.execute(q, params).fetchall()


def latest_land_scan(conn, phone):
    return conn.execute(
        "SELECT * FROM land_scans WHERE farmer_phone=? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (phone,),
    ).fetchone()


def land_scan_history(conn, phone):
    return conn.execute(
        "SELECT * FROM land_scans WHERE farmer_phone=? "
        "ORDER BY created_at DESC, id DESC",
        (phone,),
    ).fetchall()


# ------------------------------------------------------------------
# Collection-route reads (residual: route view had no real data)
# ------------------------------------------------------------------

def route_pickups(conn, coop_id):
    """Per-farmer collection totals for a co-op: kilos pooled into
    lots that are 'ready' (formed + priced, awaiting collection),
    aggregated across those lots, with the farmer's registered
    coordinates. One row per contributing farmer."""
    return conn.execute(
        "SELECT f.phone AS phone, f.name AS name, f.lat AS lat, "
        "f.lon AS lon, SUM(ll.kg_contributed) AS kg, "
        "GROUP_CONCAT(DISTINCT ll.lot_id) AS lot_ids, "
        "GROUP_CONCAT(DISTINCT lot.crop_code) AS crops "
        "FROM lot_listings ll "
        "JOIN lots lot ON lot.id = ll.lot_id "
        "JOIN listings li ON li.id = ll.listing_id "
        "JOIN farmers f ON f.phone = li.farmer_phone "
        "WHERE lot.coop_id=? AND lot.status='ready' "
        "GROUP BY f.phone ORDER BY f.name",
        (coop_id,),
    ).fetchall()


def route_deliveries(conn, coop_id):
    """Per-buyer delivery totals: orders placed on this co-op's
    ready lots, aggregated per buyer. Coordinates come from the
    buyer's farmer record when they have one (buyers register with
    the same OTP flow); buyers without a record have no coordinates
    and are reported separately by the caller — never placed on the
    map with invented locations."""
    return conn.execute(
        "SELECT o.buyer_phone AS phone, "
        "MAX(o.buyer_name) AS name, "
        "SUM(o.quantity_kg) AS kg, COUNT(*) AS orders, "
        "f.lat AS lat, f.lon AS lon "
        "FROM orders o "
        "JOIN lots lot ON lot.id = o.lot_id "
        "LEFT JOIN farmers f ON f.phone = o.buyer_phone "
        "WHERE lot.coop_id=? AND lot.status='ready' "
        "GROUP BY o.buyer_phone ORDER BY name",
        (coop_id,),
    ).fetchall()


def get_land_scan(conn, scan_id):
    return conn.execute(
        "SELECT * FROM land_scans WHERE id=?", (scan_id,)).fetchone()
