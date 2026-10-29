"""Security regression tests for the Oct 29 2026 hardening pass.

Covers the holes found in the security-focus review:
  1. POST /v1/auth/request-code had NO per-IP rate limit: the per-phone
     cap (3/10min) does not stop an attacker iterating over phone
     numbers, minting a code for each (SMS-spam once a real gateway is
     configured, auth_codes/outbox growth even in demo mode).
  2. auth_codes rows were never pruned: expired/consumed codes
     accumulated forever in the long-lived SQLite file, grown via an
     unauthenticated endpoint.
  3. auth._dev_outbox grew without bound on a long-lived process.
  4. verify_token 500'd on a correctly signed non-dict JSON payload
     (payload.get on a str/list) instead of returning None.

Run: python3 tests/test_security_oct29.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hashlib
import hmac

import seed
import app as app_module
import auth
import db

IP_A = "203.0.113.1"   # per-IP cap test
IP_B = "203.0.113.2"   # per-phone cap still enforced
IP_C = "203.0.113.3"   # pruning test


def request_code(client, phone, ip):
    return client.post("/v1/auth/request-code", json={"phone": phone},
                       environ_base={"REMOTE_ADDR": ip})


def run():
    seed.seed()
    client = app_module.app.test_client()
    auth._attempts.clear()
    auth._dev_outbox.clear()
    passed = 0

    print("=== 1. Per-IP request-code cap: 60 distinct numbers OK, 61st 429 ===")
    for i in range(60):
        r = request_code(client, f"980000{i:04d}", IP_A)
        assert r.status_code == 200, (i, r.get_json())
    r = request_code(client, "9800009999", IP_A)
    assert r.status_code == 429, r.get_json()
    print("  PASS — 61st distinct-number request from one IP rejected with 429")
    passed += 1

    print("\n=== 2. Per-phone cap still enforced (4th request for one number 429) ===")
    for i in range(3):
        r = request_code(client, "9841000001", IP_B)
        assert r.status_code == 200, (i, r.get_json())
    r = request_code(client, "9841000001", IP_B)
    assert r.status_code == 429, r.get_json()
    print("  PASS — per-phone limit unchanged")
    passed += 1

    print("\n=== 3. Expired auth_codes rows are pruned on mint ===")
    conn = db.get_db()
    conn.execute(
        "INSERT INTO auth_codes (phone, code_hash, expires_at) "
        "VALUES (?, ?, datetime('now', '-1 hour'))",
        ("9800099999", "deadbeef"))
    conn.commit()
    n_before = conn.execute("SELECT COUNT(*) AS n FROM auth_codes "
                            "WHERE phone='9800099999'").fetchone()["n"]
    assert n_before == 1
    conn.close()
    r = request_code(client, "9841000002", IP_C)
    assert r.status_code == 200, r.get_json()
    conn = db.get_db()
    n_after = conn.execute("SELECT COUNT(*) AS n FROM auth_codes "
                           "WHERE phone='9800099999'").fetchone()["n"]
    conn.close()
    assert n_after == 0, "expired row survived minting"
    print("  PASS — expired row deleted when a new code was minted")
    passed += 1

    print("\n=== 4. Dev outbox capped at 200 entries ===")
    auth._dev_outbox.clear()
    conn = db.get_db()
    for i in range(205):
        auth.create_auth_code(conn, f"980100{i:04d}")
    conn.close()
    assert len(auth._dev_outbox) == 200, len(auth._dev_outbox)
    assert auth._dev_outbox[-1]["phone"] == "9801000204"
    print("  PASS — 205 mints leave exactly the newest 200 codes")
    passed += 1

    print("\n=== 5. verify_token rejects non-dict payloads instead of 500ing ===")
    for raw in (b'"just-a-string"', b'[1, 2, 3]', b'42'):
        payload_b64 = auth._b64(raw)
        sig = hmac.new(auth._secret_bytes(), payload_b64.encode("ascii"),
                       hashlib.sha256).hexdigest()
        assert auth.verify_token(f"{payload_b64}.{sig}") is None, raw
    # Sanity: a real token still verifies.
    token = auth.issue_token("9841000001")
    assert auth.verify_token(token) == "9841000001"
    print("  PASS — str/list/int payloads return None, real tokens still verify")
    passed += 1

    print(f"\nAll {passed} Oct-29 security checks passed.")


if __name__ == "__main__":
    run()
