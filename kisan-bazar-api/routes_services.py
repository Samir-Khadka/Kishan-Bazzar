"""Service routes: machinery rentals (browse, list, book, and the
owner-confirm / cancel lifecycle) and the trilingual assistant
chat endpoint."""
from flask import Blueprint, g, jsonify, request

import assistant
import auth
import db
import repositories
from web import reject, require_auth

bp = Blueprint("services", __name__)


RENTAL_TYPES = ("power_tiller", "mini_tiller", "thresher", "sprayer",
                "tractor", "harvester", "other")

@bp.get("/v1/rentals")
def list_rentals():
    """List available machinery. Filter by ?type= and ?location=."""
    mtype = request.args.get("type", "").strip()
    loc = request.args.get("location", "").strip()
    conn = db.get_db()
    rows = repositories.list_rentals(conn, mtype, loc)
    conn.close()
    return jsonify([dict(r) for r in rows])


@bp.post("/v1/rentals")
@require_auth
def create_rental():
    """Owner lists a machine for rent. Auth required."""
    body = request.get_json(force=True, silent=True) or {}
    mtype = (body.get("machine_type") or "").strip()
    name = (body.get("machine_name") or "").strip()
    location = (body.get("location") or "").strip()
    contact = (body.get("contact_phone") or "").strip()
    try:
        rate = float(body.get("rate_per_day"))
    except (TypeError, ValueError):
        return jsonify({"error": "rate_per_day must be a number"}), 400
    if mtype not in RENTAL_TYPES:
        return jsonify({"error": f"machine_type must be one of {list(RENTAL_TYPES)}"}), 400
    if not name or not location or not contact or rate <= 0:
        return jsonify({"error": "machine_name, location, contact_phone and positive rate_per_day required"}), 400
    conn = db.get_db()
    me = repositories.get_farmer(conn, g.phone)
    cur = conn.execute(
        "INSERT INTO rentals (owner_phone, owner_name, machine_type, machine_name, rate_per_day, location, contact_phone) "
        "VALUES (?,?,?,?,?,?,?)",
        (g.phone, me["name"] if me else g.phone, mtype, name, rate, location, contact))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return jsonify({"rental_id": rid}), 201


@bp.post("/v1/rentals/<int:rental_id>/book")
@require_auth
def book_rental(rental_id):
    """Farmer requests a booking. Body: {"days": N, "book_date": "YYYY-MM-DD"}.

    A booking holds the machine for [book_date, book_date + days):
    a second booking whose range overlaps an active (requested or
    confirmed) one is refused with 409 — previously any number of
    farmers could book the same machine for the same days and all be
    quoted a total. The owner confirms (or the farmer cancels) via
    the lifecycle endpoints below; cancelling frees the dates."""
    body = request.get_json(force=True, silent=True) or {}
    raw_days = body.get("days")
    # Reject floats/bools outright: int(2.9) used to silently truncate
    # to 2 days, quoting a total the farmer never asked for.
    if isinstance(raw_days, (bool, float)):
        return jsonify({"error": "days must be an integer"}), 400
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        return jsonify({"error": "days must be an integer"}), 400
    book_date = (body.get("book_date") or "").strip()
    if days < 1 or days > 30 or not book_date:
        return jsonify({"error": "days 1-30 and book_date required"}), 400
    from datetime import datetime
    try:
        parsed_date = datetime.strptime(book_date, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "book_date must be YYYY-MM-DD"}), 400
    # A booking in the past used to be accepted: the owner was asked
    # to confirm dates that had already passed, and the dead range
    # sat in the overlap table. "Today" is measured in Nepal time
    # (the users' clock), falling back to server-local date.
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Kathmandu")).date()
    except Exception:
        today = datetime.now().date()
    if parsed_date < today:
        return jsonify({"error": "book_date cannot be in the past"}), 400
    conn = db.get_db()
    try:
        # Check-and-insert under the write lock, same discipline as
        # orders: two simultaneous bookings for the same dates must
        # not both slip past the overlap check.
        conn.execute("BEGIN IMMEDIATE")
        rental = repositories.get_rental(conn, rental_id)
        if not rental or not rental["available"]:
            return reject(conn, "rental not found or unavailable", 404)
        clash = repositories.overlapping_booking(conn, rental_id, book_date, days)
        if clash:
            return reject(
                conn,
                f"machine already booked from {clash['book_date']} "
                f"for {clash['days']} day(s)", 409)
        me = repositories.get_farmer(conn, g.phone)
        cur = conn.execute(
            "INSERT INTO rental_bookings (rental_id, farmer_phone, farmer_name, days, book_date) "
            "VALUES (?,?,?,?,?)",
            (rental_id, g.phone, me["name"] if me else g.phone, days, book_date))
        conn.commit()
        bid = cur.lastrowid
    finally:
        conn.close()
    total = days * rental["rate_per_day"]
    return jsonify({"booking_id": bid, "status": "requested",
                    "total": total,
                    "message": f"Booking requested. Owner will confirm at {rental['contact_phone']}."}), 201


@bp.post("/v1/rentals/bookings/<int:booking_id>/confirm")
@require_auth
def confirm_rental_booking(booking_id):
    """The machine's owner confirms a requested booking."""
    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        booking = repositories.get_booking(conn, booking_id)
        if not booking:
            return reject(conn, "booking not found", 404)
        if booking["rental_owner_phone"] != g.phone:
            return reject(conn, "only the machine owner can confirm this booking", 403)
        if booking["status"] != "requested":
            return reject(conn, f"booking is '{booking['status']}', cannot confirm", 409)
        conn.execute("UPDATE rental_bookings SET status='confirmed' WHERE id=?",
                     (booking_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"booking_id": booking_id, "status": "confirmed"})


@bp.post("/v1/rentals/bookings/<int:booking_id>/cancel")
@require_auth
def cancel_rental_booking(booking_id):
    """The booking farmer or the machine owner cancels — the dates
    become bookable again (cancelled bookings don't block overlap)."""
    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        booking = repositories.get_booking(conn, booking_id)
        if not booking:
            return reject(conn, "booking not found", 404)
        if g.phone not in (booking["farmer_phone"], booking["rental_owner_phone"]):
            return reject(conn, "only the booking farmer or the machine owner can cancel", 403)
        if booking["status"] not in ("requested", "confirmed"):
            return reject(conn, f"booking is '{booking['status']}', cannot cancel", 409)
        conn.execute("UPDATE rental_bookings SET status='cancelled' WHERE id=?",
                     (booking_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"booking_id": booking_id, "status": "cancelled"})


# Professional trilingual NLU engine (assistant.py) — Nepali (Devanagari),
# Romanized Nepali, and English, with conversation context and real DB data.
# (Replaces the old ai_advisor keyword matcher.)


@bp.post("/v1/assistant/chat")
def assistant_chat():
    """Conversational assistant.

    Public endpoint (no auth needed for general questions). If the caller
    passes a valid bearer <redacted>, ledger/order answers are personalized;
    otherwise the assistant asks them to log in for those topics.
    Rate-limited per IP to prevent abuse.
    """
    body = request.get_json(force=True, silent=True) or {}
    message = (body.get("message") or "").strip()
    session_id = (body.get("session_id") or request.remote_addr or "anon")[:64]
    if not message:
        return jsonify({"error": "message is required"}), 400
    if len(message) > 500:
        return jsonify({"error": "message too long"}), 400

    if not auth.check_rate_limit(f"assistant:{request.remote_addr}", max_attempts=30, window_seconds=60):
        return jsonify({"error": "too many requests, slow down"}), 429

    # Optional auth: personalize ledger/order answers when a token is given
    farmer = None
    authz = request.headers.get("Authorization", "")
    if authz.startswith("Bearer "):
        phone = auth.verify_token(authz[7:])
        if phone:
            conn = db.get_db()
            row = repositories.get_farmer(conn, phone)
            conn.close()
            if row:
                farmer = {"phone": row["phone"], "name": row["name"],
                          "is_staff": row["is_staff"]}

    conn = db.get_db()
    try:
        result = assistant.chat(session_id, message, db_conn=conn, farmer=farmer)
    finally:
        conn.close()
    return jsonify(result)
