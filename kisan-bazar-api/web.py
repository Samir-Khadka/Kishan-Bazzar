"""
Shared HTTP plumbing for the Kisan Bazar API: configuration constants
and the cross-cutting decorators (auth, self-check, staff-check,
idempotency).

These used to live in app.py, which meant every route file would have
had to import the app module (a cycle) to reuse them. They live here
so the route blueprints in routes_*.py can all share ONE copy of each
invariant — the staff check, in particular, previously existed as
three hand-rolled copies that had already started to drift.
"""
import os
from functools import wraps

from flask import request, jsonify, g, current_app

import db
import auth

# Vehicle-load target per lot — in production this comes from the
# co-op's configured target or a standing buyer commitment, not a
# constant; kept simple here so the aggregation logic is easy to see.
DEFAULT_TARGET_KG = 130

# Sane bounds for a single listing/order — not a business rule, a
# sanity check: nothing in this project's corridor involves a farmer
# moving 50 tonnes in one listing, so reject before it corrupts
# aggregation math or looks like an attempted overflow/abuse attempt.
MAX_REASONABLE_KG = 5000

# Demo mode: when 1, the /v1/dev/latest-code endpoint serves simulated
# OTP codes so visitors can log in to a public demo without a real SMS
# gateway. Explicit opt-in only — never enable where real farmer
# accounts exist, because it lets anyone read anyone's login code.
DEMO_MODE = os.environ.get("KISAN_BAZAR_DEMO_MODE", "").lower() in ("1", "true", "yes")


def require_auth(f):
    """Rejects the request with 401 unless a valid, unexpired bearer
    token is present. On success, g.phone holds the authenticated
    phone for the rest of the request."""
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
    """Beyond require_auth: the authenticated phone must match the
    phone the action is being taken for (a URL path segment or a JSON
    body field, checked by name). Prevents farmer A from acting as
    farmer B even with a perfectly valid token of their own."""
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


def idempotent(f):
    """Idempotency for POST endpoints. The client sends a unique
    `Idempotency-Key` header per logical operation (e.g. a UUID
    generated when the user taps the button). If the same key arrives
    again — a retry after a dropped connection — the original response
    is replayed instead of creating a duplicate. Without the header,
    the request runs normally (backward compatible). Keys expire
    after 24 hours."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        key = request.headers.get("Idempotency-Key", "").strip()
        if not key:
            return f(*args, **kwargs)
        if len(key) > 128:
            return jsonify({"error": "Idempotency-Key too long"}), 400
        conn = db.get_db()
        # Lazy cleanup of expired keys (older than 24h). Commit
        # immediately and close — the connection must NOT stay open
        # while the handler runs, or its writes will deadlock on the
        # lock we hold.
        conn.execute(
            "DELETE FROM idempotency_keys WHERE created_at < datetime('now', '-1 day')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT status_code, response_body FROM idempotency_keys WHERE key=? AND endpoint=?",
            (key, request.path),
        ).fetchone()
        conn.close()
        if row:
            return current_app.response_class(
                response=row["response_body"],
                status=row["status_code"],
                mimetype="application/json",
            )
        # First time: run the handler, then store its response.
        # Fresh connection — the lookup connection is already closed.
        resp = f(*args, **kwargs)
        # Normalize to (body, status) — handlers return jsonify(...)
        # or (jsonify, code).
        if isinstance(resp, tuple):
            body_resp, status = resp[0], resp[1]
        else:
            body_resp, status = resp, 200
        try:
            body_text = body_resp.get_data(as_text=True)
        except Exception:
            body_text = ""
        conn = db.get_db()
        try:
            conn.execute(
                "INSERT INTO idempotency_keys (key, endpoint, status_code, response_body) "
                "VALUES (?,?,?,?)",
                (key, request.path, status, body_text),
            )
            conn.commit()
        except Exception:
            # Race: another worker stored this key first. Replay it.
            conn.rollback()
            row = conn.execute(
                "SELECT status_code, response_body FROM idempotency_keys WHERE key=? AND endpoint=?",
                (key, request.path),
            ).fetchone()
            conn.close()
            if row:
                return current_app.response_class(
                    response=row["response_body"],
                    status=row["status_code"],
                    mimetype="application/json",
                )
            return resp
        conn.close()
        return resp
    return wrapper


def valid_positive_kg(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= MAX_REASONABLE_KG


def reject(conn, error, status):
    """Roll back any in-flight transaction, close the connection, and
    return a JSON error response tuple. Used by endpoints that open a
    write transaction (BEGIN IMMEDIATE) before validating — every
    failure path must release the lock, or the next writer blocks for
    the busy timeout."""
    try:
        conn.rollback()
    finally:
        conn.close()
    return jsonify({"error": error}), status
