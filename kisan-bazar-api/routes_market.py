"""Market routes: listings, lot aggregation views, buyer orders,
bid/offer negotiation, price trends and public stats. The stock
invariant for orders and offer acceptance lives in availability.py;
row reads for offers/lots go through repositories.py."""
import os
import re

from flask import Blueprint, request, jsonify, g, current_app

import db
import aggregation
import auth
import assistant
import repositories
import availability
from web import (
    DEMO_MODE, DEFAULT_TARGET_KG, MAX_REASONABLE_KG,
    require_auth, require_self, require_staff, idempotent,
    valid_positive_kg, reject,
)

bp = Blueprint("market", __name__)


@bp.post("/v1/listings")
@require_auth
@require_self("farmer_phone")
@idempotent
def create_listing():
    body = request.get_json(force=True)
    required = ["farmer_phone", "coop_id", "crop_code", "grade", "quantity_kg"]
    missing = [f for f in required if f not in body]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400
    if not valid_positive_kg(body["quantity_kg"]):
        return jsonify({"error": f"quantity_kg must be a number between 0 and {MAX_REASONABLE_KG}"}), 400
    if body["grade"] not in ("A", "B", "C"):
        return jsonify({"error": "grade must be A, B, or C"}), 400

    conn = db.get_db()
    farmer = repositories.get_farmer(conn, body["farmer_phone"])
    if not farmer:
        conn.close()
        return jsonify({"error": "unknown farmer_phone"}), 404

    # The listing belongs to the farmer's own co-op, never to whatever
    # coop_id the client sent: trusting the body let a farmer in coop A
    # inject listings into coop B's aggregation pool.
    coop_id = farmer["coop_id"]

    # Validate crop_code against the crops table — an invalid code would
    # otherwise die as an unhandled 500 on the FK constraint.
    crop = conn.execute("SELECT code FROM crops WHERE code=?", (body["crop_code"],)).fetchone()
    if not crop:
        conn.close()
        return jsonify({"error": "unknown crop_code"}), 400

    cur = conn.execute(
        "INSERT INTO listings (farmer_phone, coop_id, crop_code, grade, quantity_kg) "
        "VALUES (?,?,?,?,?)",
        (body["farmer_phone"], coop_id, body["crop_code"], body["grade"], body["quantity_kg"]),
    )
    listing_id = cur.lastrowid
    conn.commit()

    # aggregation check runs synchronously here for clarity; production
    # would enqueue this rather than block the request.
    # NOTE: uses the farmer's trusted coop_id, not body["coop_id"].
    lot_formed = aggregation.check_and_form_lot(
        conn, coop_id, body["crop_code"], body["grade"], DEFAULT_TARGET_KG
    )
    conn.close()

    response = {"listing_id": listing_id, "status": "open"}
    if lot_formed:
        response["lot_formed"] = lot_formed
    return jsonify(response), 201


@bp.get("/v1/listings/mine")
@require_auth
def my_listings():
    # phone comes from the authenticated token, never from a query
    # param — otherwise anyone could read anyone's listing history by
    # changing ?phone= in the URL despite having a valid token of their own
    conn = db.get_db()
    rows = conn.execute(
        "SELECT * FROM listings WHERE farmer_phone=? ORDER BY created_at DESC", (g.phone,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@bp.get("/v1/lots")
def list_lots():
    coop_id = request.args.get("coop_id")
    status = request.args.get("status")
    # Pagination: default 50, max 200. Prevents one request from dumping
    # the entire table as data grows.
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return jsonify({"error": "invalid pagination params"}), 400
    conn = db.get_db()
    # Join cooperatives for display name + count distinct contributing farmers
    # via lot_listings -> listings, so the frontend doesn't render placeholders.
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
    # farmer_count via one GROUP BY join, not a correlated subquery per row:
    # the old version ran one extra query for every lot on the page (up to
    # 200). Grouping by the lots PK keeps l.* deterministic in SQLite.
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
    conn.close()
    return jsonify({
        "data": [dict(r) for r in rows],
        "total": total, "limit": limit, "offset": offset,
    })


@bp.get("/v1/lots/<lot_id>")
def get_lot(lot_id):
    """Single lot by id — the website's shareable lot pages link here.
    The list endpoint is paginated, so a lookup against the first page
    would wrongly 404 lots beyond it."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT l.*, c.name AS coop_name, "
        "(SELECT COUNT(DISTINCT li.farmer_phone) FROM lot_listings ll "
        " JOIN listings li ON ll.listing_id = li.id "
        " WHERE ll.lot_id = l.id) AS farmer_count "
        "FROM lots l LEFT JOIN cooperatives c ON l.coop_id = c.id "
        "WHERE l.id=?",
        (lot_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "lot not found"}), 404
    return jsonify(dict(row))


@bp.post("/v1/lots/<lot_id>/confirm")
@require_auth
@require_staff
def confirm_lot(lot_id):
    body = request.get_json(force=True)
    if "rate_per_kg" not in body:
        return jsonify({"error": "rate_per_kg required"}), 400
    rate = body["rate_per_kg"]
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate <= 0:
        return jsonify({"error": "rate_per_kg must be a positive number"}), 400

    conn = db.get_db()
    lot = repositories.get_lot(conn, lot_id)
    if not lot:
        conn.close()
        return jsonify({"error": "lot not found"}), 404
    if lot["status"] != "forming":
        conn.close()
        return jsonify({"error": f"lot is '{lot['status']}', not 'forming'"}), 409

    conn.execute(
        "UPDATE lots SET rate_per_kg=?, status='ready' WHERE id=?",
        (rate, lot_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"lot_id": lot_id, "status": "ready", "rate_per_kg": rate})


@bp.get("/v1/market/lots")
def market_lots():
    conn = db.get_db()
    # Newest first, capped at 200: this endpoint had no ORDER BY (row order
    # was whatever SQLite felt like) and no LIMIT, so every website visit
    # pulled the entire ready-lots table. 200 matches /v1/lots' max page.
    rows = conn.execute(
        "SELECT l.*, c.name_ne, c.name_en FROM lots l "
        "JOIN crops c ON c.code = l.crop_code "
        "WHERE l.status='ready' ORDER BY l.created_at DESC LIMIT 200"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@bp.post("/v1/orders")
@require_auth
@idempotent
def place_order():
    # Buyers authenticate exactly like everyone else: an OTP-verified
    # bearer token whose phone must match the order's buyer_phone.
    # This endpoint used to take orders from ANYONE who could format
    # a Nepali phone number — and since availability is
    # pooled_kg - SUM(orders), unauthenticated fake orders directly
    # blocked real buyers from real stock. The app already holds the
    # buyer's token after login and sends it here.
    if not auth.check_rate_limit(f"order:{request.remote_addr}", max_attempts=10, window_seconds=600):
        return jsonify({"error": "too many orders, try again later"}), 429
    body = request.get_json(force=True)
    required = ["buyer_name", "buyer_phone", "lot_id", "quantity_kg"]
    missing = [f for f in required if f not in body]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400
    if not valid_positive_kg(body["quantity_kg"]):
        return jsonify({"error": f"quantity_kg must be a number between 0 and {MAX_REASONABLE_KG}"}), 400
    phone = str(body["buyer_phone"]).strip()
    # Nepali mobiles: 98XXXXXXXX / 97XXXXXXXX (10 digits, NTC/Ncell)
    if not re.fullmatch(r"9[678]\d{8}", phone):
        return jsonify({"error": "buyer_phone must be a valid Nepali mobile number"}), 400
    if phone != g.phone:
        return jsonify({"error": "you can only do this for your own phone number"}), 403

    # The stock invariant lives in availability.py — one locked
    # implementation shared with offer acceptance.
    conn = db.get_db()
    try:
        order_id, lot = availability.place_order_locked(
            conn,
            lot_id=body["lot_id"],
            quantity_kg=body["quantity_kg"],
            buyer_name=body["buyer_name"],
            buyer_phone=phone,
        )
    except availability.StockError as e:
        return jsonify({"error": e.message}), e.status
    finally:
        conn.close()
    return jsonify({
        "order_id": order_id,
        "total": body["quantity_kg"] * lot["rate_per_kg"],
    }), 201


@bp.post("/v1/offers")
@require_auth
@require_self("buyer_phone")
def make_offer():
    """Buyer places a bid/offer on a lot. Unlike fixed-price orders, offers
    propose a rate — the co-op can accept, reject, or counter.
    Requires the buyer's own verified login: the bid is bound to the token's
    phone, so nobody can place bids as someone else's number. Rate-limited
    per IP."""
    if not auth.check_rate_limit(f"offer:{request.remote_addr}", max_attempts=10, window_seconds=600):
        return jsonify({"error": "too many offers, try again later"}), 429
    body = request.get_json(force=True, silent=True) or {}
    required = ["buyer_name", "buyer_phone", "lot_id", "bid_rate_per_kg", "quantity_kg"]
    missing = [f for f in required if f not in body]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400
    phone = str(body["buyer_phone"]).strip()
    if not re.fullmatch(r"9[678]\d{8}", phone):
        return jsonify({"error": "buyer_phone must be a valid Nepali mobile number"}), 400
    try:
        rate = float(body["bid_rate_per_kg"]); qty = float(body["quantity_kg"])
    except (TypeError, ValueError):
        return jsonify({"error": "bid_rate_per_kg and quantity_kg must be numbers"}), 400
    if rate <= 0 or qty <= 0 or qty > MAX_REASONABLE_KG:
        return jsonify({"error": "invalid rate or quantity"}), 400

    conn = db.get_db()
    lot = conn.execute("SELECT * FROM lots WHERE id=?", (body["lot_id"],)).fetchone()
    if not lot:
        conn.close()
        return jsonify({"error": "lot not found"}), 404
    if lot["status"] not in ("ready", "forming"):
        conn.close()
        return jsonify({"error": f"lot is '{lot['status']}', not open for offers"}), 409

    cur = conn.execute(
        "INSERT INTO offers (lot_id, buyer_phone, buyer_name, bid_rate_per_kg, quantity_kg) "
        "VALUES (?,?,?,?,?)",
        (body["lot_id"], phone, str(body["buyer_name"]).strip(), rate, qty))
    conn.commit()
    offer_id = cur.lastrowid
    conn.close()
    return jsonify({"offer_id": offer_id, "status": "pending",
                    "message": "Offer placed. The co-op will respond soon."}), 201


@bp.get("/v1/offers")
@require_auth
def list_offers():
    """List offers, optionally filtered by lot_id or buyer_phone.
    Staff see everything; everyone else sees only their own offers —
    buyer names and phone numbers are not public data."""
    conn = db.get_db()
    is_staff = repositories.farmer_is_staff(conn, g.phone)
    lot_id = request.args.get("lot_id")
    buyer_phone = request.args.get("buyer_phone")
    if not is_staff:
        if buyer_phone and buyer_phone != g.phone:
            conn.close()
            return jsonify({"error": "you can only view your own offers"}), 403
        buyer_phone = g.phone
    q = "SELECT * FROM offers WHERE 1=1"
    params = []
    if lot_id:
        q += " AND lot_id=?"; params.append(lot_id)
    if buyer_phone:
        q += " AND buyer_phone=?"; params.append(buyer_phone)
    # Capped like every other list endpoint (Day-5 left this one
    # unbounded): newest 200 is far past any real negotiation queue.
    q += " ORDER BY created_at DESC LIMIT 200"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@bp.post("/v1/offers/<int:offer_id>/decide")
@require_auth
@require_staff
def decide_offer(offer_id):
    """Co-op staff accepts, rejects, or counters an offer.
    Body: {"decision": "accepted"|"rejected"|"countered", "counter_rate_per_kg": N}
    Accepted offers convert to real orders."""
    body = request.get_json(force=True, silent=True) or {}
    decision = (body.get("decision") or "").strip().lower()
    if decision not in ("accepted", "rejected", "countered"):
        return jsonify({"error": "decision must be accepted, rejected, or countered"}), 400
    counter = None
    if decision == "countered":
        try:
            counter = float(body.get("counter_rate_per_kg"))
        except (TypeError, ValueError):
            return jsonify({"error": "counter_rate_per_kg required for counter"}), 400
        if counter <= 0:
            return jsonify({"error": "counter rate must be positive"}), 400

    conn = db.get_db()
    try:
        # The write lock is taken BEFORE the offer is read, and the
        # status check happens on the row re-read under the lock.
        # The previous version validated status on a pre-lock read:
        # two staff decisions racing the same pending offer could
        # both pass, and an accept could convert one offer into two
        # orders. Under BEGIN IMMEDIATE the second decide now sees
        # status='converted' and gets a 409.
        conn.execute("BEGIN IMMEDIATE")
        offer = repositories.get_offer(conn, offer_id)
        if not offer:
            return reject(conn, "offer not found", 404)
        if offer["status"] != "pending" and not (offer["status"] == "countered" and decision in ("accepted", "rejected")):
            return reject(conn, f"offer is '{offer['status']}', cannot decide", 409)

        if decision == "countered":
            conn.execute(
                "UPDATE offers SET status='countered', counter_rate_per_kg=?, decided_at=datetime('now') WHERE id=?",
                (counter, offer_id))
        elif decision == "accepted":
            # Convert to a real order at the agreed rate, through the
            # SAME locked availability check as direct orders
            # (availability.py) — accepting must never oversell.
            lot = repositories.get_lot(conn, offer["lot_id"])
            if not lot:
                return reject(conn, "lot not found", 404)
            rate = offer["counter_rate_per_kg"] if offer["status"] == "countered" and offer["counter_rate_per_kg"] else offer["bid_rate_per_kg"]
            try:
                availability.insert_order(
                    conn, lot, offer["quantity_kg"],
                    offer["buyer_name"], offer["buyer_phone"],
                    unit_rate=rate, insufficient_status=409)
            except availability.StockError as e:
                return reject(conn, e.message, e.status)
            conn.execute(
                "UPDATE offers SET status='converted', decided_at=datetime('now') WHERE id=?",
                (offer_id,))
        else:
            conn.execute(
                "UPDATE offers SET status='rejected', decided_at=datetime('now') WHERE id=?",
                (offer_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"offer_id": offer_id, "decision": decision})


@bp.get("/v1/prices/trends")
def price_trends():
    """Historical lot rates per crop — real data for trend charts.
    Query: ?crop_code=GOLB&days=30"""
    crop_code = (request.args.get("crop_code") or "").strip().upper()
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        days = 30
    days = max(7, min(days, 365))
    conn = db.get_db()
    q = ("SELECT crop_code, date(created_at) AS d, "
         "SUM(rate_per_kg) AS s, COUNT(*) AS n, MIN(created_at) AS first_seen "
         "FROM lots WHERE rate_per_kg IS NOT NULL "
         "AND created_at >= date('now', ?)")
    params = [f"-{days} days"]
    if crop_code:
        q += " AND crop_code=?"; params.append(crop_code)
    # Aggregate in the DB: the old version transferred every lot row in the
    # window and grouped in Python. SUM/COUNT come from SQL; the final
    # round() stays in Python (SQLite ROUND() rounds halves away from zero,
    # Python rounds half to even — same input must give the same avg_rate).
    # first_seen ordering preserves the old crop order: crops appear in the
    # order of their first lot activity in the window.
    q += " GROUP BY crop_code, d ORDER BY first_seen, crop_code, d"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    # Group by crop, then by day (avg rate)
    by_crop = {}
    for r in rows:
        by_crop.setdefault(r["crop_code"], []).append({
            "date": r["d"],
            "avg_rate": round(r["s"] / r["n"], 1),
            "lots": r["n"],
        })
    return jsonify(by_crop)


@bp.get("/v1/prices/latest")
def latest_prices():
    """Latest rate per crop — for the market overview."""
    conn = db.get_db()
    # One row per crop straight from the DB. The old version fetched EVERY
    # rated lot in history (ORDER BY created_at DESC, no LIMIT) and walked
    # them in Python — cost grew with the whole table on each call.
    rows = conn.execute(
        "SELECT crop_code, rate_per_kg, pooled_kg, date(created_at) AS d FROM ("
        "  SELECT *, ROW_NUMBER() OVER ("
        "    PARTITION BY crop_code ORDER BY created_at DESC, id DESC"
        "  ) AS rn FROM lots "
        "  WHERE rate_per_kg IS NOT NULL AND status='ready'"
        ") WHERE rn = 1 ORDER BY crop_code").fetchall()
    conn.close()
    return jsonify({r["crop_code"]: dict(r) for r in rows})


@bp.get("/v1/stats")
def public_stats():
    """Public aggregate stats for the website. No PII, no auth."""
    conn = db.get_db()
    farmers = conn.execute("SELECT COUNT(*) as c FROM farmers WHERE is_staff=0").fetchone()["c"]
    coops = conn.execute("SELECT COUNT(*) as c FROM cooperatives").fetchone()["c"]
    lots = conn.execute("SELECT COUNT(*) as c FROM lots WHERE status='ready'").fetchone()["c"]
    conn.close()
    resp = jsonify({"farmers": farmers, "coops": coops, "active_lots": lots})
    # Cacheable for 60s — stats don't change every second, and this endpoint
    # is hit by every website visitor. Reduces DB load under traffic.
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp
