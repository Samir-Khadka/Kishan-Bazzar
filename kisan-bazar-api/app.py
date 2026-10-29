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
    Request-code is additionally capped per IP, since a per-phone cap
    alone cannot stop an attacker iterating over phone numbers.
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
from flask import Flask, request, jsonify, send_file, redirect
import db

app = Flask(__name__)


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
            else:
                # Migration: backfill demo lots for pre-lot seeds.
                import seed

                conn = db.get_db()
                seed.ensure_demo_lots(conn)
                conn.close()
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
# require_auth / require_self / require_staff / idempotent /
# valid_positive_kg / reject live in web.py — shared by every route
# blueprint, so each invariant exists exactly once.


@app.get("/v1/health")
def health():
    """Liveness probe for the hosting platform. No auth, no PII."""
    return jsonify({"status": "ok", "service": "kisan-bazar-api"})


@app.get("/")
def serve_root():
    # The main address is the WEBSITE's front door — Samir's standing
    # requirement is that website login and app login are separate
    # doors, so the bare domain must not drop visitors straight into
    # the app. The public website owns "/"; the app lives at /app.
    return redirect("/site/", code=302)


@app.get("/app")
@app.get("/app/")
def serve_app():
    html_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kisan-bazar.html")
    if os.path.exists(html_path):
        return send_file(html_path)
    return jsonify({"service": "kisan-bazar-api", "status": "running"})


# ---------------- public website ----------------
# Static marketing site served under /site/. The app lives at /app.

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


# ---------------- route blueprints ----------------
# Domain routes live in routes_*.py modules (auth / market / farmer /
# services / staff); this file is the composition root: app creation,
# startup, cross-cutting hooks, health, and the static site.
from routes_auth import bp as auth_bp
from routes_market import bp as market_bp
from routes_farmer import bp as farmer_bp
from routes_services import bp as services_bp
from routes_staff import bp as staff_bp

app.register_blueprint(auth_bp)
app.register_blueprint(market_bp)
app.register_blueprint(farmer_bp)
app.register_blueprint(services_bp)
app.register_blueprint(staff_bp)


if __name__ == "__main__":
    db.init_db()
    # debug mode must be explicitly opted into via an environment variable,
    # never the code default — debug=True in a real deployment exposes
    # stack traces (and, if the debugger endpoint is reachable, can allow
    # arbitrary code execution). Defaulting to off means someone has to
    # actively choose to weaken this, not inherit it by omission.
    debug_mode = os.environ.get("KISAN_BAZAR_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(port=5050, debug=debug_mode)
