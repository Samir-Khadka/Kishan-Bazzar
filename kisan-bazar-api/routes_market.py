"""Market routes: listings, lot aggregation views, buyer orders,
bid/offer negotiation, price trends and public stats. The stock
invariant for orders and offer acceptance lives in availability.py;
row reads for offers/lots go through repositories.py."""
import re

from flask import Blueprint, g, jsonify, request

import aggregation
import auth
import availability
import db
import repositories
from web import (DEFAULT_TARGET_KG, MAX_REASONABLE_KG, idempotent, reject,
                 require_auth, require_self, require_staff,
                 valid_positive_kg)

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
    crop = repositories.get_crop(conn, body["crop_code"])
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
    rows = repositories.listings_for_farmer(conn, g.phone)
    conn.close()
    return jsonify([dict(r) for r in rows])


@bp.get("/v1/lots")
def list_lots():
    coop_id = request.args.get("coop_id")
    status = request.args.get("status")
    # Pagination: default 50, max 200. Prevents one request from dumping
    # the entire table as data grows. The lower bound matters too:
    # SQLite treats a negative LIMIT as "no limit", so limit=-1 used
    # to silently bypass the cap and dump the whole table.
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return jsonify({"error": "invalid pagination params"}), 400
    conn = db.get_db()
    # Join cooperatives for display name + count distinct contributing
    # farmers via lot_listings -> listings, so the frontend doesn't
    # render placeholders. The query itself is a read model now:
    # repositories.lots_page.
    rows, total = repositories.lots_page(conn, coop_id, status, limit, offset)
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
    row = repositories.lot_detail(conn, lot_id)
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
    try:
        # Take the write lock BEFORE reading the lot: the old code
        # checked status on a pre-lock read, so two staff confirming
        # the same forming lot at once both passed and the second
        # silently overwrote the first's rate. Under BEGIN IMMEDIATE
        # the loser re-reads status='ready' and gets an honest 409.
        conn.execute("BEGIN IMMEDIATE")
        lot = repositories.get_lot(conn, lot_id)
        if not lot:
            return reject(conn, "lot not found", 404)
        if lot["status"] != "forming":
            return reject(conn, f"lot is '{lot['status']}', not 'forming'", 409)
        conn.execute(
            "UPDATE lots SET rate_per_kg=?, status='ready' WHERE id=?",
            (rate, lot_id),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"lot_id": lot_id, "status": "ready", "rate_per_kg": rate})


@bp.get("/v1/market/lots")
def market_lots():
    conn = db.get_db()
    # Newest first, capped at 200 (repositories.market_ready_lots):
    # this endpoint used to have no ORDER BY and no LIMIT, so every
    # website visit pulled the entire ready-lots table.
    rows = repositories.market_ready_lots(conn)
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


@bp.get("/v1/orders/mine")
@require_auth
def my_orders():
    """The authenticated buyer's own orders, with payment status —
    read-only. (Settlement itself is staff-only, below.)"""
    conn = db.get_db()
    rows = repositories.orders_for_buyer(conn, g.phone)
    conn.close()
    return jsonify([dict(r) for r in rows])


@bp.post("/v1/orders/<int:order_id>/settle")
@require_auth
@require_staff
def settle_order(order_id):
    """Record that an order has been PAID (settled). Staff-only.

    Body: {"amount": N, "method": "cash"|"bank"|"esewa"|"khalti",
           "reference": "optional staff note / txn reference"}

    This records a payment the co-op made/received through its own
    channels — there is NO payment-gateway integration, and the
    method string is what staff attest happened, not a processor
    confirmation. The amount must equal the order total (quantity ×
    unit rate): settlement is a full-order event in this state
    machine (unpaid -> settled), so a partial or wrong amount is a
    data error, rejected with the expected total. Settling twice is
    a 409. The check-and-insert runs under BEGIN IMMEDIATE, so two
    staff settling the same order concurrently can't double-record.
    """
    body = request.get_json(force=True, silent=True) or {}
    method = (body.get("method") or "").strip().lower()
    if method not in repositories.SETTLEMENT_METHODS:
        return jsonify({
            "error": f"method must be one of {list(repositories.SETTLEMENT_METHODS)}"
        }), 400
    try:
        amount = float(body.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number"}), 400
    if amount <= 0:
        return jsonify({"error": "amount must be positive"}), 400
    reference = (body.get("reference") or "").strip() or None

    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = repositories.get_order(conn, order_id)
        if not order:
            return reject(conn, "order not found", 404)
        if repositories.get_settlement(conn, order_id):
            return reject(conn, "order is already settled", 409)
        total = round(order["quantity_kg"] * order["unit_rate"], 2)
        if abs(amount - total) > 0.01:
            return reject(
                conn,
                f"amount must equal the order total Rs {total:,.2f}", 400)
        row = repositories.insert_settlement(
            conn, order_id, amount, method, reference, g.phone)
        conn.commit()
    finally:
        conn.close()
    return jsonify({
        "order_id": order_id,
        "payment_status": "settled",
        "amount": row["amount"],
        "method": row["method"],
        "reference": row["reference"],
        "settled_by": row["settled_by"],
        "settled_at": row["settled_at"],
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
    lot = repositories.get_lot(conn, body["lot_id"])
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
    if not is_staff and buyer_phone and buyer_phone != g.phone:
        conn.close()
        return jsonify({"error": "you can only view your own offers"}), 403
    rows = repositories.list_offers(
        conn, is_staff=is_staff, phone=g.phone,
        lot_id=lot_id, buyer_phone=buyer_phone)
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
    by_crop = repositories.price_trends(conn, crop_code, days)
    conn.close()
    return jsonify(by_crop)


@bp.get("/v1/prices/latest")
def latest_prices():
    """Latest rate per crop — for the market overview."""
    conn = db.get_db()
    latest = repositories.latest_prices(conn)
    conn.close()
    return jsonify(latest)


@bp.get("/v1/stats")
def public_stats():
    """Public aggregate stats for the website. No PII, no auth."""
    conn = db.get_db()
    stats = repositories.public_stats(conn)
    conn.close()
    resp = jsonify(stats)
    # Cacheable for 60s — stats don't change every second, and this endpoint
    # is hit by every website visitor. Reduces DB load under traffic.
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp
