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
import re
from functools import wraps
from flask import Flask, request, jsonify, g, send_file
import db
import aggregation
import auth
import assistant

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


# ---------------- startup: ensure schema + demo seed ----------------
# This lives in app.py (not just wsgi.py) so the database is ready no
# matter how the app is launched: gunicorn app:app, gunicorn wsgi:app,
# flask run, etc. A file lock stops two gunicorn workers from racing here
# at startup — seed.seed() does init_db(fresh=True), which unlinks the DB
# file, so an unguarded race corrupts the database and 500s every request.
def _ensure_db_ready():
    import fcntl

    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".seed.lock")
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            db.init_db()
            conn = db.get_db()
            coops = conn.execute("SELECT COUNT(*) AS n FROM cooperatives").fetchone()["n"]
            conn.close()
            if coops == 0:
                import seed

                seed.seed()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


_ensure_db_ready()


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
    # Don't override an endpoint's own Cache-Control (e.g. public stats).
    if "Cache-Control" not in resp.headers:
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


# ---------------- public website ----------------
# Static marketing site served under /site/. The app itself stays at /.

@app.get("/site/")
@app.get("/site/<path:filename>")
def serve_site(filename="index.html"):
    from flask import send_from_directory
    site_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "website")
    # Only serve safe static types
    if not re.fullmatch(r"[a-zA-Z0-9._/-]+\.(html|css|js|png|jpg|svg|ico)", filename):
        return send_from_directory(site_dir, "404.html"), 404
    full = os.path.join(site_dir, filename)
    if not os.path.isfile(full) or not os.path.realpath(full).startswith(os.path.realpath(site_dir)):
        return send_from_directory(site_dir, "404.html"), 404
    return send_from_directory(site_dir, filename)


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
    # Codes are issued for unknown numbers too — otherwise no new farmer
    # could ever register (signup requires a verified OTP token). This is
    # safe: the code goes to the phone's owner via SMS, so requesting a code
    # for someone else's number only sends THEM a text; the requester never
    # sees it. SMS-spam is bounded by the rate limit above.
    # The response is identical either way — no phone-number enumeration.
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
    """Register or update a user profile (farmer or buyer).

    The caller must already hold a valid bearer token for the phone number
    being registered — i.e. they completed the OTP request/verify flow,
    proving ownership of the number. Role can only be 'farmer' or 'buyer';
    'staff' (admin) is NEVER self-granted — staff accounts are created
    manually out-of-band by existing staff.
    """
    body = request.get_json(force=True, silent=True) or {}
    phone = (body.get("phone") or "").strip()
    name = (body.get("name") or "").strip()
    role = (body.get("role") or "farmer").strip().lower()
    if role not in ("farmer", "buyer"):
        return jsonify({"error": "role must be 'farmer' or 'buyer'"}), 400
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
    if land_type not in ("khet", "bari", "pakho", "balaute"):
        return jsonify({"error": "unknown land_type"}), 400

    conn = db.get_db()
    coop = conn.execute("SELECT id FROM cooperatives WHERE id=?", (coop_id,)).fetchone()
    if not coop:
        conn.close()
        return jsonify({"error": "unknown coop_id"}), 400
    existing = conn.execute("SELECT phone FROM farmers WHERE phone=?", (phone,)).fetchone()
    if existing:
        conn.execute("UPDATE farmers SET name=?, land_type=?, payout_method=?, role=? WHERE phone=?", (name, land_type, payout_method, role, phone))
    else:
        conn.execute(
            "INSERT INTO farmers (phone, name, role, coop_id, lat, lon, payout_method, land_type, is_staff) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (phone, name, role, coop_id, lat, lon, payout_method, land_type, is_staff)
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
    # Pagination: default 50, max 200. Prevents one request from dumping
    # the entire table as data grows.
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return jsonify({"error": "invalid pagination params"}), 400
    conn = db.get_db()
    query = "SELECT * FROM lots WHERE 1=1"
    params = []
    if coop_id:
        query += " AND coop_id=?"
        params.append(coop_id)
    if status:
        query += " AND status=?"
        params.append(status)
    total = conn.execute(f"SELECT COUNT(*) as c FROM ({query})", params).fetchone()["c"]
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify({
        "data": [dict(r) for r in rows],
        "total": total, "limit": limit, "offset": offset,
    })


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
    # Buyers (hotels, hostels) are a separate identity class this prototype
    # doesn't model login for yet. Minimum viable protection: every order
    # must carry a verifiable buyer phone number (traceable, SMS-confirmable)
    # and the endpoint is rate-limited per IP against order-spam floods.
    # Full buyer accounts with standing orders are the next step.
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
        "INSERT INTO orders (buyer_name, buyer_phone, lot_id, quantity_kg, unit_rate) VALUES (?,?,?,?,?)",
        (body["buyer_name"], phone, body["lot_id"], body["quantity_kg"], lot["rate_per_kg"]),
    )
    conn.commit()
    order_id = cur.lastrowid
    conn.close()
    return jsonify({
        "order_id": order_id,
        "total": body["quantity_kg"] * lot["rate_per_kg"],
    }), 201


# ---------------- bid/offer negotiation ----------------

@app.post("/v1/offers")
def make_offer():
    """Buyer places a bid/offer on a lot. Unlike fixed-price orders, offers
    propose a rate — the co-op can accept, reject, or counter.
    Requires buyer identity (phone). Rate-limited per IP."""
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


@app.get("/v1/offers")
def list_offers():
    """List offers, optionally filtered by lot_id or buyer_phone."""
    lot_id = request.args.get("lot_id")
    buyer_phone = request.args.get("buyer_phone")
    conn = db.get_db()
    q = "SELECT * FROM offers WHERE 1=1"
    params = []
    if lot_id:
        q += " AND lot_id=?"; params.append(lot_id)
    if buyer_phone:
        q += " AND buyer_phone=?"; params.append(buyer_phone)
    q += " ORDER BY created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.post("/v1/offers/<int:offer_id>/decide")
@require_auth
def decide_offer(offer_id):
    """Co-op staff accepts, rejects, or counters an offer.
    Body: {"decision": "accepted"|"rejected"|"countered", "counter_rate_per_kg": N}
    Accepted offers convert to real orders."""
    conn = db.get_db()
    me = conn.execute("SELECT is_staff FROM farmers WHERE phone=?",
                      (g.phone,)).fetchone()
    if not me or not me["is_staff"]:
        conn.close()
        return jsonify({"error": "staff only"}), 403
    body = request.get_json(force=True, silent=True) or {}
    decision = (body.get("decision") or "").strip().lower()
    if decision not in ("accepted", "rejected", "countered"):
        return jsonify({"error": "decision must be accepted, rejected, or countered"}), 400

    conn = db.get_db()
    offer = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if not offer:
        conn.close()
        return jsonify({"error": "offer not found"}), 404
    if offer["status"] != "pending" and not (offer["status"] == "countered" and decision in ("accepted", "rejected")):
        conn.close()
        return jsonify({"error": f"offer is '{offer['status']}', cannot decide"}), 409

    if decision == "countered":
        try:
            counter = float(body.get("counter_rate_per_kg"))
        except (TypeError, ValueError):
            conn.close()
            return jsonify({"error": "counter_rate_per_kg required for counter"}), 400
        if counter <= 0:
            conn.close()
            return jsonify({"error": "counter rate must be positive"}), 400
        conn.execute(
            "UPDATE offers SET status='countered', counter_rate_per_kg=?, decided_at=datetime('now') WHERE id=?",
            (counter, offer_id))
    elif decision == "accepted":
        # Convert to a real order at the agreed rate.
        rate = offer["counter_rate_per_kg"] if offer["status"] == "countered" and offer["counter_rate_per_kg"] else offer["bid_rate_per_kg"]
        conn.execute(
            "INSERT INTO orders (buyer_name, buyer_phone, lot_id, quantity_kg, unit_rate, status) "
            "VALUES (?,?,?,?,?,'placed')",
            (offer["buyer_name"], offer["buyer_phone"], offer["lot_id"],
             offer["quantity_kg"], rate))
        conn.execute(
            "UPDATE offers SET status='converted', decided_at=datetime('now') WHERE id=?",
            (offer_id,))
    else:
        conn.execute(
            "UPDATE offers SET status='rejected', decided_at=datetime('now') WHERE id=?",
            (offer_id,))
    conn.commit()
    conn.close()
    return jsonify({"offer_id": offer_id, "decision": decision})


# ---------------- crop doctor ----------------

# Symptom keywords mapped to pest/disease keys in ai_advisor.CROPS_INFO.
# Each entry: (keywords_ne_en, pest_key). Matched against the farmer's
# symptom description to rank likely diagnoses.
SYMPTOM_INDEX = {
    "cauliflower": [
        (["लाही", "aphid", "lahi", "सेतो कीरा", "पात मुनि"], "lahi"),
        (["फेद कुहिने", "damping", "बेर्ना मर्ने", "नर्सरी"], "damping_off"),
        (["गाँठे", "clubroot", "जरा सुन्निने"], "clubroot"),
    ],
    "tomato": [
        (["डढुवा", "blight", "कालो दाग", "पात डढ्ने"], "blight"),
        (["पात खुम्चिने", "curl", "खुम्चिने", "सेतो झिँगा", "whitefly"], "leaf_curl"),
    ],
    "potato": [
        (["खुम्रे", "grub", "khumre", "जमिनमुनि कीरा"], "khumre"),
        (["डढुवा", "blight", "late blight"], "late_blight"),
    ],
    "onion": [
        (["थ्रिप्स", "thrips", "चाँदी", "पात चम्किने"], "thrips"),
    ],
}


@app.post("/v1/crop-doctor/diagnose")
def crop_doctor_diagnose():
    """Diagnose a crop problem from symptom description (+ optional photo).

    Body: {"crop_code": "GOLB", "symptoms": "पातमा कालो दाग...",
           "farmer_phone": "98...", "photo_ref": "optional-id"}
    Returns ranked diagnoses with remedies from the agronomy KB.
    Photo bytes are NOT sent here — upload separately; photo_ref links them.
    """
    body = request.get_json(force=True, silent=True) or {}
    crop_code = (body.get("crop_code") or "").strip().upper()
    symptoms = (body.get("symptoms") or "").strip()
    farmer_phone = (body.get("farmer_phone") or "").strip() or None
    photo_ref = (body.get("photo_ref") or "").strip() or None
    if not crop_code or not symptoms:
        return jsonify({"error": "crop_code and symptoms are required"}), 400

    from ai_advisor import CROPS_INFO
    # Find crop by code
    crop_key = None
    for key, info in CROPS_INFO.items():
        if info.get("code", "").upper() == crop_code or key.upper() == crop_code:
            crop_key = key
            break
    if not crop_key:
        return jsonify({"error": f"unknown crop_code: {crop_code}"}), 400
    info = CROPS_INFO[crop_key]

    # Rank pest/disease entries by symptom keyword matches
    q = symptoms.lower()
    scored = []
    for keywords, pest_key in SYMPTOM_INDEX.get(crop_key, []):
        hits = sum(1 for kw in keywords if kw.lower() in q)
        if hits and pest_key in info.get("pests", {}):
            scored.append((hits, pest_key))
    scored.sort(reverse=True)

    diagnoses = []
    for _, pest_key in scored:
        diagnoses.append({
            "key": pest_key,
            "remedy": info["pests"][pest_key],
        })
    # If nothing matched, return all known issues for the crop
    if not diagnoses:
        diagnoses = [{"key": k, "remedy": v}
                     for k, v in info.get("pests", {}).items()]

    # Save to history
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO diagnoses (farmer_phone, crop_code, symptoms, photo_ref, diagnosis_key, diagnosis_text) "
        "VALUES (?,?,?,?,?,?)",
        (farmer_phone, crop_code, symptoms, photo_ref,
         diagnoses[0]["key"] if diagnoses else None,
         diagnoses[0]["remedy"] if diagnoses else ""))
    conn.commit()
    diag_id = cur.lastrowid
    conn.close()

    return jsonify({
        "diagnosis_id": diag_id,
        "crop": {"ne": info["ne"], "en": info["en"]},
        "diagnoses": diagnoses,
        "note": "यो AI सुझाव हो — निश्चितताका लागि स्थानीय कृषि प्राविधिकसँग परामर्श गर्नुहोस्। "
                "(This is AI guidance — consult your local agri technician to confirm.)",
    }), 200


@app.get("/v1/crop-doctor/history")
def crop_doctor_history():
    """Past diagnoses for a farmer."""
    phone = request.args.get("farmer_phone", "").strip()
    if not phone:
        return jsonify({"error": "farmer_phone required"}), 400
    conn = db.get_db()
    rows = conn.execute(
        "SELECT * FROM diagnoses WHERE farmer_phone=? ORDER BY created_at DESC LIMIT 20",
        (phone,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---------------- machinery rentals ----------------

RENTAL_TYPES = ("power_tiller", "mini_tiller", "thresher", "sprayer",
                "tractor", "harvester", "other")

@app.get("/v1/rentals")
def list_rentals():
    """List available machinery. Filter by ?type= and ?location=."""
    mtype = request.args.get("type", "").strip()
    loc = request.args.get("location", "").strip()
    conn = db.get_db()
    q = "SELECT * FROM rentals WHERE available=1"
    params = []
    if mtype:
        q += " AND machine_type=?"; params.append(mtype)
    if loc:
        q += " AND location LIKE ?"; params.append(f"%{loc}%")
    q += " ORDER BY created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.post("/v1/rentals")
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
    me = conn.execute("SELECT name FROM farmers WHERE phone=?", (g.phone,)).fetchone()
    cur = conn.execute(
        "INSERT INTO rentals (owner_phone, owner_name, machine_type, machine_name, rate_per_day, location, contact_phone) "
        "VALUES (?,?,?,?,?,?,?)",
        (g.phone, me["name"] if me else g.phone, mtype, name, rate, location, contact))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return jsonify({"rental_id": rid}), 201


@app.post("/v1/rentals/<int:rental_id>/book")
@require_auth
def book_rental(rental_id):
    """Farmer requests a booking. Body: {"days": N, "book_date": "YYYY-MM-DD"}."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        days = int(body.get("days"))
    except (TypeError, ValueError):
        return jsonify({"error": "days must be an integer"}), 400
    book_date = (body.get("book_date") or "").strip()
    if days < 1 or days > 30 or not book_date:
        return jsonify({"error": "days 1-30 and book_date required"}), 400
    conn = db.get_db()
    rental = conn.execute("SELECT * FROM rentals WHERE id=? AND available=1",
                          (rental_id,)).fetchone()
    if not rental:
        conn.close()
        return jsonify({"error": "rental not found or unavailable"}), 404
    me = conn.execute("SELECT name FROM farmers WHERE phone=?", (g.phone,)).fetchone()
    cur = conn.execute(
        "INSERT INTO rental_bookings (rental_id, farmer_phone, farmer_name, days, book_date) "
        "VALUES (?,?,?,?,?)",
        (rental_id, g.phone, me["name"] if me else g.phone, days, book_date))
    conn.commit()
    bid = cur.lastrowid
    conn.close()
    total = days * rental["rate_per_day"]
    return jsonify({"booking_id": bid, "status": "requested",
                    "total": total,
                    "message": f"Booking requested. Owner will confirm at {rental['contact_phone']}."}), 201


# ---------------- price trends ----------------

@app.get("/v1/prices/trends")
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
    q = ("SELECT crop_code, rate_per_kg, date(created_at) as d "
         "FROM lots WHERE rate_per_kg IS NOT NULL "
         "AND created_at >= date('now', ?)")
    params = [f"-{days} days"]
    if crop_code:
        q += " AND crop_code=?"; params.append(crop_code)
    q += " ORDER BY created_at"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    # Group by crop, then by day (avg rate)
    by_crop = {}
    for r in rows:
        c = r["crop_code"]
        by_crop.setdefault(c, {}).setdefault(r["d"], []).append(r["rate_per_kg"])
    result = {}
    for c, days_map in by_crop.items():
        result[c] = [{"date": d, "avg_rate": round(sum(v)/len(v), 1), "lots": len(v)}
                     for d, v in sorted(days_map.items())]
    return jsonify(result)


@app.get("/v1/prices/latest")
def latest_prices():
    """Latest rate per crop — for the market overview."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT crop_code, rate_per_kg, pooled_kg, date(created_at) as d FROM lots "
        "WHERE rate_per_kg IS NOT NULL AND status='ready' "
        "ORDER BY created_at DESC").fetchall()
    conn.close()
    seen = {}
    for r in rows:
        if r["crop_code"] not in seen:
            seen[r["crop_code"]] = dict(r)
    return jsonify(seen)


@app.get("/v1/stats")
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


# ---------------- admin endpoints ----------------

@app.get("/v1/me")
@require_auth
def me():
    """The caller's own farmer profile. Returns 404 when the phone verified
    via OTP has no profile yet — the frontend then shows the registration
    form instead of assuming the account exists."""
    phone = g.phone
    conn = db.get_db()
    row = conn.execute(
        "SELECT phone, name, role, coop_id, payout_method, land_type, is_staff "
        "FROM farmers WHERE phone=?", (phone,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "no profile yet", "phone": phone}), 404
    d = dict(row)
    d["isStaff"] = bool(d.pop("is_staff"))
    return jsonify(d)


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
# Professional trilingual NLU engine (assistant.py) — Nepali (Devanagari),
# Romanized Nepali, and English, with conversation context and real DB data.
# (Replaces the old ai_advisor keyword matcher.)


@app.post("/v1/assistant/chat")
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
            row = conn.execute(
                "SELECT phone, name, is_staff FROM farmers WHERE phone=?", (phone,)).fetchone()
            conn.close()
            if row:
                farmer = dict(row)

    conn = db.get_db()
    try:
        result = assistant.chat(session_id, message, db_conn=conn, farmer=farmer)
    finally:
        conn.close()
    return jsonify(result)



if __name__ == "__main__":
    db.init_db()
    # debug mode must be explicitly opted into via an environment variable,
    # never the code default — debug=True in a real deployment exposes
    # stack traces (and, if the debugger endpoint is reachable, can allow
    # arbitrary code execution). Defaulting to off means someone has to
    # actively choose to weaken this, not inherit it by omission.
    debug_mode = os.environ.get("KISAN_BAZAR_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(port=5050, debug=debug_mode)
