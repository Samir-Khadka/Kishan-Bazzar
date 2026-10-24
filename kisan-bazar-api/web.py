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
import sqlite3
import time
from datetime import datetime
from functools import wraps

from flask import request, jsonify, g, current_app

import db
import auth
import repositories

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
        try:
            is_staff = repositories.farmer_is_staff(conn, g.phone)
        finally:
            conn.close()
        if not is_staff:
            return jsonify({"error": "staff access required"}), 403
        return f(*args, **kwargs)
    return wrapper


# A claim row's status_code while its handler is still running. A row in
# this state must NEVER be replayed — the response body is not stored
# yet, so replaying it would hand back an empty/partial response.
_IDEMPOTENT_PENDING = 0
# A claim older than this is a dead worker's (crash/kill between claim
# and store): the next duplicate may reclaim the key and run fresh
# instead of waiting forever on a row that will never complete.
_IDEMPOTENT_STALE_SECONDS = 300
# How long a request that loses the claim race waits for the winner to
# finish before telling the client the operation is still in flight.
_IDEMPOTENT_WAIT_SECONDS = 10


def _idem_lookup(conn, key):
    return conn.execute(
        "SELECT status_code, response_body, created_at FROM idempotency_keys "
        "WHERE key=? AND endpoint=?",
        (key, request.path),
    ).fetchone()


def _idem_claim_age_seconds(row):
    try:
        created = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
        return (datetime.utcnow() - created).total_seconds()
    except Exception:
        return 0.0


def _idem_replay(row):
    return current_app.response_class(
        response=row["response_body"],
        status=row["status_code"],
        mimetype="application/json",
    )


def _idem_claim(key):
    """Atomically claim an idempotency key for this request.

    Runs under BEGIN IMMEDIATE, so exactly one of two racing requests
    becomes the winner ('run'); the loser waits for the winner's stored
    response ('wait') instead of running the handler a second time.
    Returns one of:
      ('replay', row)  — a completed response is already stored;
      ('run',)         — this request owns the claim and must run the handler;
      ('run-nostore',) — the key is claimed on another endpoint (the
                         table's PK is on key alone); run the handler
                         normally without storing, the old behaviour;
      ('wait',)        — another request holds a live claim; wait for it.
    """
    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Lazy cleanup of expired keys (older than 24h).
        conn.execute(
            "DELETE FROM idempotency_keys WHERE created_at < datetime('now', '-1 day')"
        )
        row = _idem_lookup(conn, key)
        if row is not None and row["status_code"] != _IDEMPOTENT_PENDING:
            conn.commit()
            return "replay", row
        if row is None:
            try:
                conn.execute(
                    "INSERT INTO idempotency_keys (key, endpoint, status_code, response_body) "
                    "VALUES (?,?,0,'')",
                    (key, request.path),
                )
            except sqlite3.IntegrityError:
                # Lost the claim race: either a concurrent same-key claim
                # (wait for it below) or the key is claimed on another
                # endpoint (PK is on key alone — run without storing).
                conn.rollback()
                row = _idem_lookup(conn, key)
                if row is not None and row["status_code"] == _IDEMPOTENT_PENDING:
                    return ("wait",)
                if row is not None:
                    return "replay", row
                return ("run-nostore",)
            conn.commit()
            return ("run",)
        # A pending claim on our endpoint: reclaim it if its worker died.
        if _idem_claim_age_seconds(row) > _IDEMPOTENT_STALE_SECONDS:
            conn.execute("DELETE FROM idempotency_keys WHERE key=?", (key,))
            conn.execute(
                "INSERT INTO idempotency_keys (key, endpoint, status_code, response_body) "
                "VALUES (?,?,0,'')",
                (key, request.path),
            )
            conn.commit()
            return ("run",)
        conn.commit()
        return ("wait",)
    except Exception:
        try:
            conn.rollback()
        finally:
            conn.close()
        raise
    conn.close()


def _idem_await_completion(key):
    """Wait for the claim winner to store its response; the stored row
    when it completes, else None (caller reports 'still in progress')."""
    deadline = time.time() + _IDEMPOTENT_WAIT_SECONDS
    while time.time() < deadline:
        time.sleep(0.25)
        conn = db.get_db()
        try:
            row = _idem_lookup(conn, key)
        finally:
            conn.close()
        if row is not None and row["status_code"] != _IDEMPOTENT_PENDING:
            return row
    return None


def _idem_release_claim(key):
    """Drop a pending claim (the handler raised) so a later retry with
    the same key runs fresh instead of waiting on a doomed row."""
    conn = db.get_db()
    try:
        conn.execute(
            "DELETE FROM idempotency_keys WHERE key=? AND endpoint=? AND status_code=0",
            (key, request.path),
        )
        conn.commit()
    finally:
        conn.close()


def _idem_store(key, resp):
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
            "UPDATE idempotency_keys SET status_code=?, response_body=? "
            "WHERE key=? AND endpoint=? AND status_code=0",
            (status, body_text, key, request.path),
        )
        conn.commit()
    finally:
        conn.close()


def idempotent(f):
    """Idempotency for POST endpoints. The client sends a unique
    `Idempotency-Key` header per logical operation (e.g. a UUID
    generated when the user taps the button). If the same key arrives
    again — a retry after a dropped connection — the original response
    is replayed instead of creating a duplicate. Without the header,
    the request runs normally (backward compatible). Keys expire
    after 24 hours.

    Concurrency: the key is CLAIMED atomically (BEGIN IMMEDIATE) before
    the handler runs, so two requests racing with the same key cannot
    both execute the handler — the loser waits for the winner's stored
    response. Previously the SELECT-then-run check raced: both requests
    could run the handler (two orders, two listings) and the loser only
    got the winner's response replayed afterwards.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        key = request.headers.get("Idempotency-Key", "").strip()
        if not key:
            return f(*args, **kwargs)
        if len(key) > 128:
            return jsonify({"error": "Idempotency-Key too long"}), 400
        action = _idem_claim(key)
        if action[0] == "replay":
            return _idem_replay(action[1])
        if action[0] == "wait":
            row = _idem_await_completion(key)
            if row is not None:
                return _idem_replay(row)
            return jsonify(
                {"error": "duplicate request is still being processed, please retry"}
            ), 409
        store = action[0] == "run"
        # The winner: run the handler, then store its response.
        try:
            resp = f(*args, **kwargs)
        except Exception:
            _idem_release_claim(key)
            raise
        if store:
            _idem_store(key, resp)
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
