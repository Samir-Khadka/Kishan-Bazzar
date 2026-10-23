"""
Authentication for the Kisan Bazar API: phone + one-time-code login,
issuing short-lived signed bearer tokens. No new dependencies — built on
stdlib hmac/hashlib, the same primitive real JWT libraries use, kept
deliberately simple rather than reaching for a library this project
doesn't otherwise need.

Flow:
  1. POST /v1/auth/request-code {phone}  -> a 6-digit code is generated,
     stored HASHED (never in plaintext), and "sent" — in this prototype
     that means appended to an in-memory outbox tests can read; in
     production it's one call to the same SMS gateway already used by
     the SMS/USSD channel (see ../kisan-bazar-sms/).
  2. POST /v1/auth/verify {phone, code} -> if the code matches and hasn't
     expired or been used, a bearer token is issued.
  3. Every mutating endpoint requires `Authorization: Bearer <token>`.
     The token's phone must match the phone the action is being taken
     as — a farmer cannot list produce, scan land, or do anything else
     under another farmer's identity, even if they guess that farmer's
     phone number.

Explicitly NOT covered by this module (see ../kisan-bazar-security-notes.md
for the full picture): SIM-swap risk on the phone-as-identity model,
distributed rate limiting (this is in-memory and per-process — fine for
one instance, not for a real multi-server deployment), token revocation
before natural expiry, and anything below the application layer (TLS
termination, WAF, DDoS protection are deployment concerns, not code).
"""
import hashlib
import hmac
import json
import base64
import secrets
import time
import os

CODE_TTL_SECONDS = 5 * 60        # a request-code is valid for 5 minutes
TOKEN_TTL_SECONDS = 24 * 60 * 60  # a login session lasts 24 hours

# ---------------- the signing secret ----------------
# Resolved lazily on first use (not at import: the database may not
# exist yet when this module is first imported), then cached for the
# life of the process. Priority order:
#
#   1. KISAN_BAZAR_SECRET from the environment — explicit deployment
#      configuration always wins.
#   2. A secret PERSISTED IN THE DATABASE (app_settings table). It is
#      created atomically on first use (see _load_or_create_db_secret),
#      so every process/worker sharing the database converges on ONE
#      value, and — critically — it survives restarts and redeploys.
#   3. An ephemeral random secret, only if the database is unreachable
#      when first needed. Safe, but tokens die with the process.
#
# History (2026-10-23): this used to be option 1-or-3 only, generated
# at import time. On any deployment where the env var isn't actually
# present (a blueprint declaring it is not the same as the running
# service having it), EVERY restart/redeploy — and with multiple
# workers, potentially every request — invalidated all sessions, and
# OTP code hashes too (they're keyed with the same secret). Users hit
# a raw "server 401" with no recovery. Option 2 removes that failure
# mode without hardcoding anything: the persisted value is random,
# generated on the server, and never appears in source.
_SECRET_BYTES = None


def _load_or_create_db_secret():
    """Read the signing secret from app_settings, creating it if absent.

    BEGIN IMMEDIATE takes the database write lock BEFORE the read, so
    two workers booting simultaneously cannot both create a secret:
    the second blocks on the lock (busy_timeout in db.get_db), then
    reads the row the first one committed. INSERT OR IGNORE + re-read
    under the same lock is belt-and-suspenders for the same guarantee.
    Returns the secret string, or None if the DB is unreachable."""
    try:
        import db as _db  # local import: keeps auth importable standalone
    except Exception:
        return None
    import sqlite3 as _sqlite3
    import time as _time
    for _attempt in range(3):
        conn = None
        try:
            conn = _db.get_db()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS app_settings "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                row = conn.execute(
                    "SELECT value FROM app_settings WHERE key='signing_secret'"
                ).fetchone()
                if row is not None:
                    value = row[0]
                else:
                    value = secrets.token_hex(32)
                    conn.execute(
                        "INSERT OR IGNORE INTO app_settings (key, value) "
                        "VALUES ('signing_secret', ?)",
                        (value,),
                    )
                    # Re-read: the stored row is the single source of
                    # truth even if a race somehow inserted first.
                    row = conn.execute(
                        "SELECT value FROM app_settings WHERE key='signing_secret'"
                    ).fetchone()
                    value = row[0]
                conn.commit()
                return value
            except Exception:
                conn.rollback()
                raise
        except _sqlite3.OperationalError:
            # Lock contention beyond busy_timeout — brief backoff, retry.
            _time.sleep(0.2 * (_attempt + 1))
        except Exception:
            return None
        finally:
            if conn is not None:
                conn.close()
    return None


def _secret_bytes():
    """The HMAC key for code hashes and token signatures. Cached after
    first resolution; a process restart re-resolves to the SAME value
    whenever the env var or the database persists — which is the whole
    point (see the history note above)."""
    global _SECRET_BYTES
    if _SECRET_BYTES is not None:
        return _SECRET_BYTES
    env_secret = os.environ.get("KISAN_BAZAR_SECRET")
    if env_secret:
        _SECRET_BYTES = env_secret.encode("utf-8")
        return _SECRET_BYTES
    persisted = _load_or_create_db_secret()
    if persisted:
        _SECRET_BYTES = persisted.encode("utf-8")
        return _SECRET_BYTES
    _SECRET_BYTES = secrets.token_hex(32).encode("utf-8")
    print("WARNING: KISAN_BAZAR_SECRET not set and the database is "
          "unreachable — using a random secret for this process only. "
          "Tokens will stop working on restart. Set KISAN_BAZAR_SECRET "
          "or make the database available in any real deployment.")
    return _SECRET_BYTES

# Test-visible "outbox" standing in for the real SMS gateway — tests import
# this module directly and read it, the same way a real integration test
# would read a test double for an SMS provider, rather than the HTTP API
# ever returning a code in its response body (which a production API must
# never do — that would let anyone log in as anyone by just asking).
_dev_outbox = []


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ---------------- in-memory rate limiting ----------------
# Per-process, per-key sliding window. Good enough for one instance; a real
# multi-server deployment needs a shared store (Redis) instead — noted so
# this isn't mistaken for a complete answer at scale.
_attempts = {}


def check_rate_limit(key: str, max_attempts: int, window_seconds: int) -> bool:
    """Returns True if this attempt is allowed (and records it), False if
    the key has hit the limit within the window."""
    now = time.time()
    window_start = now - window_seconds
    history = [t for t in _attempts.get(key, []) if t > window_start]
    if not history:
        # Drop the key entirely once its bucket is empty: on a long-lived
        # process every distinct key (IP/phone) would otherwise linger
        # forever as an empty list — a slow, unbounded memory leak.
        _attempts.pop(key, None)
    if len(history) >= max_attempts:
        _attempts[key] = history
        return False
    history.append(now)
    _attempts[key] = history
    return True


# ---------------- verification codes ----------------

def _hash_code(phone: str, code: str) -> str:
    # phone is mixed into the hash so a leaked hash for one phone can't be
    # replayed against a different phone even if the code digits collide
    return hmac.new(_secret_bytes(), f"{phone}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def create_auth_code(conn, phone: str) -> str:
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = _hash_code(phone, code)
    # One live code per phone: minting a new code retires every earlier
    # unconsumed one. Without this, a second request left the first
    # code valid too — two (rate limit: three) simultaneous chances to
    # guess, and real confusion about which texted code is current.
    conn.execute(
        "UPDATE auth_codes SET consumed_at=datetime('now') "
        "WHERE phone=? AND consumed_at IS NULL",
        (phone,),
    )
    conn.execute(
        "INSERT INTO auth_codes (phone, code_hash, expires_at) "
        "VALUES (?, ?, datetime('now', ?))",
        (phone, code_hash, f"+{CODE_TTL_SECONDS} seconds"),
    )
    conn.commit()
    _dev_outbox.append({"phone": phone, "code": code})  # stands in for the SMS send
    return code


def verify_code(conn, phone: str, code: str) -> bool:
    code_hash = _hash_code(phone, code)
    row = conn.execute(
        "SELECT id, code_hash FROM auth_codes "
        "WHERE phone=? AND consumed_at IS NULL AND expires_at > datetime('now') "
        "ORDER BY created_at DESC",
        (phone,),
    ).fetchall()
    for r in row:
        # constant-time comparison — a regular == here would let an
        # attacker time how many leading characters matched and narrow
        # down the code digit by digit across many requests
        if hmac.compare_digest(r["code_hash"], code_hash):
            conn.execute("UPDATE auth_codes SET consumed_at=datetime('now') WHERE id=?", (r["id"],))
            conn.commit()
            return True
    return False


# ---------------- bearer tokens ----------------

def issue_token(phone: str) -> str:
    payload = {"phone": phone, "exp": int(time.time()) + TOKEN_TTL_SECONDS}
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_secret_bytes(), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_token(token: str):
    """Returns the phone number the token is valid for, or None if the
    token is malformed, has an invalid signature, or has expired."""
    if not token or "." not in token:
        return None
    payload_b64, sig = token.rsplit(".", 1)
    expected_sig = hmac.new(_secret_bytes(), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        return None
    try:
        payload = json.loads(_b64d(payload_b64))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload.get("phone")
