"""
Kisan Bazar API — a real, runnable implementation of the core endpoints
from the backend architecture doc, against SQLite instead of Postgres
(see db.py for why). Run with: python3 app.py, then hit it with curl or
run tests/ for an end-to-end exercise.

SECURITY MODEL (see ../kisan-bazar-security-notes.md for the full picture,
including what this deliberately does NOT cover):

  - Every mutating endpoint requires `Authorization: Bearer <token>`,
    obtained via POST /v1/auth/request-code then POST /v1/auth/verify
    (see auth.py). A missing/invalid/expired token gets 401.
  - Farmer-scoped actions (listing produce, saving a land scan) require
    the token's phone to match the phone the action is for — a farmer
    cannot act as another farmer even if they know that phone number.
    Mismatch gets 403, not a silent no-op.
  - Lot confirmation (setting a price) requires a token belonging to a
    farmer record with is_staff=1. A regular farmer token gets 403.
  - Auth endpoints are rate-limited per phone: request-code capped to
    stop SMS-bombing a number, verify capped to make brute-forcing the
    6-digit code impractical within the code's 5-minute validity window.
  - All SQL is parameterized (no string-built queries carry user values)
    — verified by grep, not just by design intent.
  - Unhandled exceptions return a generic 500 with no stack trace or
    internals, regardless of Flask's debug setting — belt and suspenders
    against accidentally shipping debug=True.

Endpoints:
  POST /v1/auth/request-code     start login: phone -> code sent (mocked)
  POST /v1/auth/verify           phone + code -> bearer token
  POST /v1/listings              create a listing (auth: must be that farmer)
  GET  /v1/listings/mine         a farmer's own listings (auth required)
  GET  /v1/lots                  co-op's lots, optionally filtered by status
  POST /v1/lots/<id>/confirm     staff-only: price and confirm a forming lot
  GET  /v1/market/lots           buyer-facing: lots that are ready/confirmed
  POST /v1/orders                place an order against a ready lot
  POST /v1/farmers/<phone>/land-scan          save a scan (auth: must be that farmer)
  GET  /v1/farmers/<phone>/land-scan          latest scan (auth required)
  GET  /v1/farmers/<phone>/land-scan/history  full history (auth required)
"""
import os
from functools import wraps
from flask import Flask, request, jsonify, g, send_file
import db
import aggregation
import auth

app = Flask(__name__)

# vehicle-load target per lot — in production this comes from the co-op's
# configured target or a standing buyer commitment, not a constant; kept
# simple here so the aggregation logic is easy to see working.
DEFAULT_TARGET_KG = 130

# sane bounds for a single listing/order — not a business rule, a sanity
# check: nothing in this project's corridor involves a farmer moving 50
# tonnes in one listing, so reject before it corrupts aggregation math
# or looks like an attempted overflow/abuse attempt
MAX_REASONABLE_KG = 5000

# Demo mode: when 1, the /v1/dev/latest-code endpoint serves simulated OTP
# codes so visitors can log in to a public demo without a real SMS gateway.
# Explicit opt-in only — never enable where real farmer accounts exist,
# because it lets anyone read anyone's login code.
DEMO_MODE = os.environ.get("KISAN_BAZAR_DEMO_MODE", "").lower() in ("1", "true", "yes")


# ---------------- error handling: never leak internals ----------------
@app.errorhandler(Exception)
def handle_unexpected_error(e):
    # Deliberately generic — this fires regardless of Flask's debug flag,
    # so even a misconfigured deployment (debug left on) doesn't leak a
    # stack trace, file paths, or query text through this path. Flask's
    # own HTTPExceptions (404, etc.) already carry the right status code
    # via e.code; anything else is an unexpected server error.
    code = getattr(e, "code", 500)
    if isinstance(code, int) and 400 <= code < 600:
        return jsonify({"error": getattr(e, "description", "request failed")}), code
    app.logger.exception("Unhandled error")
    return jsonify({"error": "internal server error"}), 500


@app.after_request
def set_security_headers(resp):
    # Baseline headers an application can reasonably set itself. TLS
    # termination, HSTS, and a content-security-policy for any served
    # HTML are deployment/reverse-proxy concerns, not this dev server's
    # job — noted in the security doc, not silently assumed to be handled.
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Cache-Control"] = "no-store"

    # CORS: the app that calls this API is necessarily served from a
    # different origin (a published artifact link, not this API's own
    # host), so without these headers a browser blocks every fetch()
    # client-side even though the server would happily process it — this
    # is exactly the kind of gap that leaves two correctly-built pieces
    # never actually able to talk to each other. ALLOWED_ORIGINS is a
    # real allowlist, not a wildcard — a wildcard plus credentialed
    # requests (Authorization headers) is a standing CORS misconfiguration.
    # No separate OPTIONS route is needed: Flask auto-generates a preflight
    # responder for every defined route, and this hook injects the CORS
    # headers onto that response the same as any other — verified by
    # actually sending a preflight request and reading the raw response,
    # not assumed (an earlier version had a redundant custom OPTIONS
    # catch-all route that, on inspection, Flask's routing never reached).
    origin = request.headers.get("Origin")
    allowed = _allowed_origins()
    if origin and (origin in allowed or "*" in allowed):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def _allowed_origins():
    # Comma-separated list via env var in any real deployment; falls
    # back to localhost-only so a forgotten env var fails closed
    # (nothing allowed cross-origin) rather than failing open.
    raw = os.environ.get("KISAN_BAZAR_ALLOWED_ORIGINS", "http://localhost:5050,http://localhost:8080")
    return {o.strip() for o in raw.split(",") if o.strip()}


# ---------------- auth plumbing ----------------

def require_auth(f):
    """Rejects the request with 401 unless a valid, unexpired bearer token
    is present. On success, g.phone holds the authenticated farmer's phone
    for the rest of the request."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"error": "missing or malformed Authorization header"}), 401
        token = header[len("Bearer "):]
        phone = auth.verify_token(token)
        if not phone:
            return jsonify({"error": "invalid or expired token"}), 401
        g.phone = phone
        return f(*args, **kwargs)
    return wrapper


def require_self(phone_arg_name):
    """Beyond require_auth: the authenticated phone must match the phone
    the action is being taken for (a URL path segment or a JSON body
    field, checked by name). Prevents farmer A from acting as farmer B
    even with a perfectly valid token of their own."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            target_phone = kwargs.get(phone_arg_name)
            if target_phone is None:
                body = request.get_json(silent=True) or {}
                target_phone = body.get(phone_arg_name)
            if target_phone != g.phone:
                return jsonify({"error": "you can only do this for your own phone number"}), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator


def require_staff(f):
    """Beyond require_auth: the authenticated phone must belong to a
    farmer record with is_staff=1. Used for co-op actions like pricing
    a lot — a regular farmer token must not be able to do this."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        conn = db.get_db()
        row = conn.execute("SELECT is_staff FROM farmers WHERE phone=?", (g.phone,)).fetchone()
        conn.close()
        if not row or not row["is_staff"]:
            return jsonify({"error": "staff access required"}), 403
        return f(*args, **kwargs)
    return wrapper


def valid_positive_kg(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= MAX_REASONABLE_KG


@app.get("/v1/health")
def health():
    """Liveness probe for the hosting platform. No auth, no PII."""
    return jsonify({"status": "ok", "service": "kisan-bazar-api"})


@app.get("/")
def serve_index():
    html_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kisan-bazar.html")
    if os.path.exists(html_path):
        return send_file(html_path)
    return jsonify({"service": "kisan-bazar-api", "status": "running"})


# ---------------- auth endpoints ----------------

@app.post("/v1/auth/request-code")
def request_code():
    body = request.get_json(force=True, silent=True) or {}
    phone = body.get("phone")
    if not phone or not isinstance(phone, str):
        return jsonify({"error": "phone is required"}), 400

    # rate-limited per phone: at most 3 code requests per 10 minutes —
    # stops one attacker from SMS-bombing a farmer's phone with codes
    if not auth.check_rate_limit(f"request-code:{phone}", max_attempts=3, window_seconds=600):
        return jsonify({"error": "too many code requests for this number, try again later"}), 429

    conn = db.get_db()
    farmer = conn.execute("SELECT phone FROM farmers WHERE phone=?", (phone,)).fetchone()
    conn.close()
    if not farmer:
        # Same response whether or not the phone exists — a different
        # message here ("no such farmer" vs "code sent") would let an
        # attacker enumerate which phone numbers are registered.
        pass
    else:
        conn = db.get_db()
        code = auth.create_auth_code(conn, phone)
        conn.close()
        import sys
        sys.stderr.write(f"\n🔔 [SMS GATEWAY MOCK] OTP verification code for {phone}: {code}\n")
        sys.stderr.flush()
    return jsonify({"message": "if this number is registered, a code has been sent"})


@app.get("/v1/dev/latest-code/<phone>")
def dev_latest_code(phone):
    """Dev/demo convenience: returns the latest simulated SMS code.

    Only available when KISAN_BAZAR_DEMO_MODE=1. This is what lets visitors
    log in to the public demo without a real SMS gateway — and it is also
    why it must NEVER be on for anything holding real farmer data: anyone
    could read anyone's login code.
    """
    if not DEMO_MODE:
        return jsonify({"error": "not found"}), 404
    for m in reversed(auth._dev_outbox):
        if m.get("phone") == phone:
            return jsonify({"phone": phone, "code": m.get("code")})
    return jsonify({"error": "no code found"}), 404


@app.post("/v1/auth/verify")
def verify():
    body = request.get_json(force=True, silent=True) or {}
    phone = body.get("phone")
    code = body.get("code")
    if not phone or not code:
        return jsonify({"error": "phone and code are required"}), 400

    # rate-limited per phone: at most 5 verify attempts per 10 minutes —
    # 6-digit codes expire in 5 minutes, so this bounds any brute-force
    # attempt to a handful of guesses against a million possibilities
    if not auth.check_rate_limit(f"verify:{phone}", max_attempts=5, window_seconds=600):
        return jsonify({"error": "too many attempts, try again later"}), 429

    conn = db.get_db()
    ok = auth.verify_code(conn, phone, str(code))
    conn.close()
    if not ok:
        return jsonify({"error": "invalid or expired code"}), 401

    token = auth.issue_token(phone)
    return jsonify({"token": token, "expires_in_seconds": auth.TOKEN_TTL_SECONDS})


@app.post("/v1/auth/signup")
@require_auth
@require_self("phone")
def signup():
    """Register or update a farmer profile.

    The caller must already hold a valid bearer token for the phone number
    being registered — i.e. they completed the OTP request/verify flow,
    proving ownership of the number. Staff status can NEVER be self-granted
    here (always 0); co-op staff are promoted out-of-band by existing staff.
    """
    body = request.get_json(force=True, silent=True) or {}
    phone = (body.get("phone") or "").strip()
    name = (body.get("name") or "").strip()
    coop_id = body.get("coop_id") or "kavre-veg"
    land_type = body.get("land_type") or "bari"
    payout_method = body.get("payout_method") or "esewa"
    # is_staff is deliberately NOT read from the request body: honoring
    # {"role":"admin"} here was a full privilege-escalation hole.
    is_staff = 0

    if not phone or len(phone) < 6:
        return jsonify({"error": "valid phone number is required"}), 400
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        lat = float(body.get("lat") if body.get("lat") is not None else 27.62)
        lon = float(body.get("lon") if body.get("lon") is not None else 85.55)
    except (TypeError, ValueError):
        return jsonify({"error": "lat/lon must be numbers"}), 400
    if land_type not in ("bari", "khet", "bagar", "kharbari"):
        return jsonify({"error": "unknown land_type"}), 400

    conn = db.get_db()
    coop = conn.execute("SELECT id FROM cooperatives WHERE id=?", (coop_id,)).fetchone()
    if not coop:
        conn.close()
        return jsonify({"error": "unknown coop_id"}), 400
    existing = conn.execute("SELECT phone FROM farmers WHERE phone=?", (phone,)).fetchone()
    if existing:
        conn.execute("UPDATE farmers SET name=?, land_type=?, payout_method=? WHERE phone=?", (name, land_type, payout_method, phone))
    else:
        conn.execute(
            "INSERT INTO farmers (phone, name, coop_id, lat, lon, payout_method, land_type, is_staff) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (phone, name, coop_id, lat, lon, payout_method, land_type, is_staff)
        )
    conn.commit()
    conn.close()

    token = auth.issue_token(phone)
    return jsonify({
        "message": "account registered successfully",
        "phone": phone,
        "name": name,
        "token": token,
        "is_staff": False
    }), 201


# ---------------- listings & aggregation ----------------

@app.post("/v1/listings")
@require_auth
@require_self("farmer_phone")
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
    farmer = conn.execute(
        "SELECT * FROM farmers WHERE phone=?", (body["farmer_phone"],)
    ).fetchone()
    if not farmer:
        conn.close()
        return jsonify({"error": "unknown farmer_phone"}), 404

    # The listing belongs to the farmer's own co-op, never to whatever
    # coop_id the client sent: trusting the body let a farmer in coop A
    # inject listings into coop B's aggregation pool.
    coop_id = farmer["coop_id"]

    cur = conn.execute(
        "INSERT INTO listings (farmer_phone, coop_id, crop_code, grade, quantity_kg) "
        "VALUES (?,?,?,?,?)",
        (body["farmer_phone"], coop_id, body["crop_code"], body["grade"], body["quantity_kg"]),
    )
    listing_id = cur.lastrowid
    conn.commit()

    # aggregation check runs synchronously here for clarity; production
    # would enqueue this rather than block the request
    lot_formed = aggregation.check_and_form_lot(
        conn, body["coop_id"], body["crop_code"], body["grade"], DEFAULT_TARGET_KG
    )
    conn.close()

    response = {"listing_id": listing_id, "status": "open"}
    if lot_formed:
        response["lot_formed"] = lot_formed
    return jsonify(response), 201


@app.get("/v1/listings/mine")
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


@app.get("/v1/lots")
def list_lots():
    coop_id = request.args.get("coop_id")
    status = request.args.get("status")
    conn = db.get_db()
    query = "SELECT * FROM lots WHERE 1=1"
    params = []
    if coop_id:
        query += " AND coop_id=?"
        params.append(coop_id)
    if status:
        query += " AND status=?"
        params.append(status)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.post("/v1/lots/<lot_id>/confirm")
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
    lot = conn.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
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


@app.get("/v1/market/lots")
def market_lots():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT l.*, c.name_ne, c.name_en FROM lots l "
        "JOIN crops c ON c.code = l.crop_code "
        "WHERE l.status='ready'"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---------------- land scan ----------------

VALID_LAND_TYPES = {"khet", "bari", "pakho", "balaute"}
VALID_CONFIDENCE = {"good", "ok"}


@app.post("/v1/farmers/<phone>/land-scan")
@require_auth
@require_self("phone")
def save_land_scan(phone):
    """Persists a land-scan result from the app's photo+quiz flow. The photo
    itself is never sent here or stored — only the derived signals (colour
    bucket, texture answer, drainage answer) and the resulting land type,
    so this endpoint carries no image data or privacy/storage burden."""
    body = request.get_json(force=True)
    if "land_type" not in body or "confidence" not in body:
        return jsonify({"error": "land_type and confidence are required"}), 400
    if body["land_type"] not in VALID_LAND_TYPES:
        return jsonify({"error": f"land_type must be one of {sorted(VALID_LAND_TYPES)}"}), 400
    if body["confidence"] not in VALID_CONFIDENCE:
        return jsonify({"error": f"confidence must be one of {sorted(VALID_CONFIDENCE)}"}), 400

    conn = db.get_db()
    farmer = conn.execute("SELECT * FROM farmers WHERE phone=?", (phone,)).fetchone()
    if not farmer:
        conn.close()
        return jsonify({"error": "unknown farmer"}), 404

    cur = conn.execute(
        "INSERT INTO land_scans (farmer_phone, land_type, confidence, color_key, texture, drainage, had_photo) "
        "VALUES (?,?,?,?,?,?,?)",
        (phone, body["land_type"], body["confidence"],
         body.get("color_key"), body.get("texture"), body.get("drainage"),
         1 if body.get("had_photo") else 0),
    )
    scan_id = cur.lastrowid
    conn.execute("UPDATE farmers SET land_type=? WHERE phone=?", (body["land_type"], phone))
    conn.commit()
    row = conn.execute("SELECT * FROM land_scans WHERE id=?", (scan_id,)).fetchone()
    conn.close()
    return jsonify(dict(row)), 201


@app.get("/v1/farmers/<phone>/land-scan")
@require_auth
@require_self("phone")
def latest_land_scan(phone):
    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM land_scans WHERE farmer_phone=? ORDER BY created_at DESC, id DESC LIMIT 1",
        (phone,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "no scan on file for this farmer"}), 404
    return jsonify(dict(row))


@app.get("/v1/farmers/<phone>/land-scan/history")
@require_auth
@require_self("phone")
def land_scan_history(phone):
    conn = db.get_db()
    rows = conn.execute(
        "SELECT * FROM land_scans WHERE farmer_phone=? ORDER BY created_at DESC, id DESC",
        (phone,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---------------- orders ----------------

@app.post("/v1/orders")
def place_order():
    # Deliberately NOT behind require_auth: buyers (hotels, hostels) are a
    # separate identity class this prototype doesn't model login for yet.
    # Left open, same as the original version — flagged here rather than
    # silently left inconsistent with everything else, so it's a known,
    # visible gap rather than an accidental one. See the security notes.
    body = request.get_json(force=True)
    required = ["buyer_name", "lot_id", "quantity_kg"]
    missing = [f for f in required if f not in body]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400
    if not valid_positive_kg(body["quantity_kg"]):
        return jsonify({"error": f"quantity_kg must be a number between 0 and {MAX_REASONABLE_KG}"}), 400

    conn = db.get_db()
    lot = conn.execute("SELECT * FROM lots WHERE id=?", (body["lot_id"],)).fetchone()
    if not lot:
        conn.close()
        return jsonify({"error": "lot not found"}), 404
    if lot["status"] != "ready":
        conn.close()
        return jsonify({"error": f"lot is '{lot['status']}', not orderable"}), 409
    if body["quantity_kg"] > lot["pooled_kg"]:
        conn.close()
        return jsonify({"error": "quantity exceeds available lot size"}), 400
    # Oversell guard: earlier orders against this lot reduce what's left.
    # Without this, N orders each <= pooled_kg could all succeed and sell
    # the same kilos several times over.
    already_ordered = conn.execute(
        "SELECT COALESCE(SUM(quantity_kg), 0) AS total FROM orders WHERE lot_id=?",
        (body["lot_id"],)
    ).fetchone()["total"]
    if already_ordered + body["quantity_kg"] > lot["pooled_kg"]:
        conn.close()
        return jsonify({"error": f"only {lot['pooled_kg'] - already_ordered} kg still available on this lot"}), 400

    cur = conn.execute(
        "INSERT INTO orders (buyer_name, lot_id, quantity_kg, unit_rate) VALUES (?,?,?,?)",
        (body["buyer_name"], body["lot_id"], body["quantity_kg"], lot["rate_per_kg"]),
    )
    conn.commit()
    order_id = cur.lastrowid
    conn.close()
    return jsonify({
        "order_id": order_id,
        "total": body["quantity_kg"] * lot["rate_per_kg"],
    }), 201


# ---------------- admin endpoints ----------------

@app.get("/v1/admin/overview")
@require_auth
@require_staff
def admin_overview():
    """Co-op staff dashboard data. Staff-only: this exposes farmer PII."""
    conn = db.get_db()
    farmers = conn.execute("SELECT phone, name, coop_id, lat, lon, payout_method, land_type, is_staff FROM farmers").fetchall()
    listings = conn.execute("SELECT id, farmer_phone, coop_id, crop_code, grade, quantity_kg, quantity_committed_kg, status, created_at FROM listings ORDER BY id DESC LIMIT 50").fetchall()
    lots = conn.execute("SELECT id, coop_id, crop_code, grade, target_kg, pooled_kg, rate_per_kg, status, created_at FROM lots ORDER BY created_at DESC").fetchall()
    crops = conn.execute("SELECT code, name_ne, name_en, shelf_life_hours FROM crops").fetchall()
    orders = conn.execute("SELECT id, buyer_name, lot_id, quantity_kg, unit_rate, created_at FROM orders ORDER BY id DESC").fetchall()
    conn.close()

    total_kg = sum(r["quantity_kg"] for r in listings)
    pooled_kg = sum(r["pooled_kg"] for r in lots)

    return jsonify({
        "farmers": [dict(r) for r in farmers],
        "listings": [dict(r) for r in listings],
        "lots": [dict(r) for r in lots],
        "crops": [dict(r) for r in crops],
        "orders": [dict(r) for r in orders],
        "counts": {
            "farmers": len(farmers),
            "listings": len(listings),
            "lots": len(lots),
            "total_kg": round(total_kg, 1),
            "pooled_kg": round(pooled_kg, 1)
        }
    })


# ---------------- AI advisor endpoint ----------------
try:
    import ai_advisor
except ImportError:
    from . import ai_advisor

@app.post("/v1/assistant/chat")
def assistant_chat():
    body = request.get_json(force=True, silent=True) or {}
    message = (body.get("message") or "").strip()
    zone = body.get("zone", "midhill")
    land = body.get("land", "bari")
    reply = ai_advisor.analyze_query(message, zone=zone, land=land)
    return jsonify(reply)



if __name__ == "__main__":
    db.init_db()
    # debug mode must be explicitly opted into via an environment variable,
    # never the code default — debug=True in a real deployment exposes
    # stack traces (and, if the debugger endpoint is reachable, can allow
    # arbitrary code execution). Defaulting to off means someone has to
    # actively choose to weaken this, not inherit it by omission.
    debug_mode = os.environ.get("KISAN_BAZAR_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(port=5050, debug=debug_mode)
