"""Auth & identity routes: OTP request/verify, demo code retrieval,
admin password login, farmer signup, and /v1/me (the caller's own
profile). Shared auth invariants live in web.py; token mechanics
in auth.py."""
import os

from flask import Blueprint, current_app, g, jsonify, request

import auth
import db
import repositories
from web import DEMO_MODE, require_auth, require_self

bp = Blueprint("auth", __name__)


@bp.post("/v1/auth/request-code")
def request_code():
    body = request.get_json(force=True, silent=True) or {}
    phone = body.get("phone")
    if not phone or not isinstance(phone, str):
        return jsonify({"error": "phone is required"}), 400

    # rate-limited per phone: at most 3 code requests per 10 minutes —
    # stops one attacker from SMS-bombing a farmer's phone with codes
    if not auth.check_rate_limit(f"request-code:{phone}", max_attempts=3, window_seconds=600):
        return jsonify({"error": "too many code requests for this number, try again later"}), 429

    # Codes are issued for unknown numbers too — otherwise no new farmer
    # could ever register (signup requires a verified OTP token). This is
    # safe: the code goes to the phone's owner via SMS, so requesting a code
    # for someone else's number only sends THEM a text; the requester never
    # sees it. SMS-spam is bounded by the rate limit above.
    # The response is identical either way — no phone-number enumeration.
    conn = db.get_db()
    auth.create_auth_code(conn, phone)
    conn.close()
    # NEVER log the code itself — server logs (Render's log stream
    # included) are readable by anyone with dashboard access, and a
    # live code is a full login credential for its 5-minute life.
    # The masked line still proves issuance for debugging; in demo
    # mode the code is retrievable via /v1/dev/latest-code instead.
    import sys
    masked = phone[:4] + "•••" + phone[-2:] if len(phone) >= 6 else "•••"
    sys.stderr.write(f"\n🔔 [SMS GATEWAY MOCK] OTP issued for {masked} (code not logged)\n")
    sys.stderr.flush()
    return jsonify({"message": "if this number is registered, a code has been sent"})


@bp.get("/v1/dev/latest-code/<phone>")
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


@bp.post("/v1/auth/admin")
def admin_login():
    """Password login for the site owner/admin. Separate from the OTP flow
    that farmers/buyers use — the admin needs direct access without waiting
    for an SMS. Credentials come from env vars (never hardcoded); in demo
    mode a default is used. Returns a staff token that unlocks the admin panel."""
    import hashlib, hmac
    # Brute-force protection: admin credentials are a high-value target and
    # this endpoint is public. 10 attempts per 10 minutes per IP — a wrong
    # guess past the limit gets 429 instead of another try.
    if not auth.check_rate_limit(f"admin-login:{request.remote_addr}", max_attempts=10, window_seconds=600):
        return jsonify({"error": "too many attempts, try again later"}), 429
    body = request.get_json(force=True, silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    expected_user = os.environ.get("KISAN_ADMIN_USER", "admin" if DEMO_MODE else "")
    expected_pass = os.environ.get("KISAN_ADMIN_PASS", "kisan123" if DEMO_MODE else "")
    # FAIL CLOSED on misconfiguration: if either credential is unset/
    # empty (outside demo mode, where defaults apply), NOBODY can log
    # in. The password side previously had no such guard — with
    # KISAN_ADMIN_USER set but KISAN_ADMIN_PASS unset, expected_pass
    # was "" and an EMPTY submitted password hashed to a match.
    if not expected_user or not expected_pass:
        current_app.logger.warning(
            "admin login attempted but KISAN_ADMIN_USER/KISAN_ADMIN_PASS "
            "are not both configured — denying (fail closed)")
        return jsonify({"error": "invalid credentials"}), 401
    if not hmac.compare_digest(username, expected_user):
        return jsonify({"error": "invalid credentials"}), 401
    # Compare password hashes to avoid timing leaks on the raw value.
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    exp_hash = hashlib.sha256(expected_pass.encode()).hexdigest()
    if not hmac.compare_digest(pw_hash, exp_hash):
        return jsonify({"error": "invalid credentials"}), 401
    # Mint a staff token for the admin phone (created in seed).
    # issue_token embeds the phone; role=staff is read from the DB.
    conn = db.get_db()
    row = repositories.get_farmer(conn, "9801100000")
    conn.close()
    if not row:
        return jsonify({"error": "admin account not set up"}), 500
    token = auth.issue_token(row["phone"])
    return jsonify({
        "token": token, "phone": row["phone"], "name": row["name"],
        "role": row["role"],
    })


@bp.post("/v1/auth/verify")
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


@bp.post("/v1/auth/signup")
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
    coop = repositories.get_cooperative(conn, coop_id)
    if not coop:
        conn.close()
        return jsonify({"error": "unknown coop_id"}), 400
    existing = repositories.get_farmer(conn, phone)
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


@bp.get("/v1/me")
@require_auth
def me():
    """The caller's own farmer profile. Returns 404 when the phone verified
    via OTP has no profile yet — the frontend then shows the registration
    form instead of assuming the account exists."""
    phone = g.phone
    conn = db.get_db()
    farmer = repositories.get_farmer(conn, phone)
    conn.close()
    if not farmer:
        return jsonify({"error": "no profile yet", "phone": phone}), 404
    d = {
        "phone": farmer["phone"], "name": farmer["name"],
        "role": farmer["role"], "coop_id": farmer["coop_id"],
        "payout_method": farmer["payout_method"],
        "land_type": farmer["land_type"],
    }
    d["isStaff"] = bool(farmer["is_staff"])
    return jsonify(d)
