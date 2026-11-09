"""Security regression tests for the Nov 9 2026 hardening pass.

Covers the holes found in the security-focus review:
  1. No request-body size limit: Flask parsed arbitrarily large JSON
     bodies into memory, so one unauthenticated multi-MB POST could
     OOM the single free-tier worker. Verified live before the fix:
     a 5 MB body (and a 50 MB body) to /v1/auth/request-code got a
     200. Bodies are now capped at 1 MB with a JSON 413.
  2. Wrong-type JSON values on unauthenticated endpoints 500'd:
     assistant/chat with a non-string `message` (.strip() on an
     int/list) or a non-string `session_id` (list used as a dict key),
     auth/verify with a non-string `phone` (SQLite cannot bind a
     list), admin login with a non-string `username`/`password`
     (.strip()/.encode() on an int). Anyone could trigger 500s (and
     log noise) with one malformed request — now honest 400s (401
     for the admin login, same shape as wrong credentials).

Run: python3 tests/test_security_nov09.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seed
import app as app_module
import auth

PHONE = "9841000001"
IP = "203.0.113.21"


def post_raw(client, path, data, ip=IP):
    return client.post(path, data=data, content_type="application/json",
                       environ_base={"REMOTE_ADDR": ip})


def post(client, path, body, ip=IP):
    return client.post(path, json=body, environ_base={"REMOTE_ADDR": ip})


def run():
    seed.seed()
    client = app_module.app.test_client()
    auth._attempts.clear()
    passed = 0

    print("=== 1. Oversized request bodies get a JSON 413 ===")
    big = b'{"phone": "98' + b'1' * 1_500_000 + b'"}'
    assert len(big) > 1_000_000
    r = post_raw(client, "/v1/auth/request-code", big)
    assert r.status_code == 413, r.status_code
    assert r.get_json()["error"].startswith("request body too large"), r.get_json()
    print("  PASS — 1.5 MB body rejected with JSON 413")
    passed += 1

    print("=== 2. A body just under the limit still reaches the handler ===")
    under = b'{"message": "' + b'a' * 900_000 + b'"}'
    assert len(under) < 1_000_000
    r = post_raw(client, "/v1/assistant/chat", under)
    # Past the size gate, so the app answers on the merits: the message
    # itself is over the 500-char cap.
    assert r.status_code == 400, r.status_code
    assert r.get_json()["error"] == "message too long", r.get_json()
    print("  PASS — 900 KB body reaches the handler (400 message too long, not 413)")
    passed += 1

    print("=== 3. assistant/chat: non-string message is a 400, not a 500 ===")
    for bad in (123, ["x"], {"a": 1}, None, True):
        r = post(client, "/v1/assistant/chat", {"message": bad})
        assert r.status_code == 400, (bad, r.status_code)
    print("  PASS — int/list/dict/null/bool message all 400")
    passed += 1
    auth._attempts.clear()
    r = post(client, "/v1/assistant/chat", {"message": "आलुको भाउ?"})
    assert r.status_code == 200, r.status_code
    assert "reply" in r.get_json() or "text" in r.get_json() or r.get_json(), r.get_json()
    print("  PASS — string message still 200")
    passed += 1

    print("=== 4. assistant/chat: non-string session_id falls back, no 500 ===")
    for bad in (["x"], 123, {"a": 1}):
        auth._attempts.clear()
        r = post(client, "/v1/assistant/chat",
                 {"message": "hi", "session_id": bad})
        assert r.status_code == 200, (bad, r.status_code)
    print("  PASS — list/int/dict session_id all 200 (falls back to IP session)")
    passed += 1

    print("=== 5. auth/verify: non-string phone is a 400, not a 500 ===")
    for bad in (["9800000000"], {"phone": "x"}, 9800000000.5):
        auth._attempts.clear()
        r = post(client, "/v1/auth/verify", {"phone": bad, "code": "123456"})
        assert r.status_code == 400, (bad, r.status_code)
    print("  PASS — list/dict/float phone all 400")
    passed += 1

    print("=== 6. auth/verify: the real flow still works ===")
    auth._attempts.clear()
    r = post(client, "/v1/auth/request-code", {"phone": PHONE})
    assert r.status_code == 200, r.status_code
    code = auth._dev_outbox[-1]["code"]
    assert auth._dev_outbox[-1]["phone"] == PHONE
    r = post(client, "/v1/auth/verify", {"phone": PHONE, "code": code})
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert r.get_json()["token"], r.get_json()
    print("  PASS — request-code -> verify still issues a token")
    passed += 1

    print("=== 7. admin login: non-string credentials are 401, not 500 ===")
    for bad_user, bad_pass in ((123, "x"), ("admin", ["x"]),
                               (["admin"], "x"), ("admin", 123)):
        auth._attempts.clear()
        r = post(client, "/v1/auth/admin",
                 {"username": bad_user, "password": bad_pass})
        assert r.status_code == 401, ((bad_user, bad_pass), r.status_code)
        assert r.get_json()["error"] == "invalid credentials", r.get_json()
    print("  PASS — wrong-type username/password all 401, same shape as wrong creds")
    passed += 1
    auth._attempts.clear()
    r = post(client, "/v1/auth/admin",
             {"username": "admin", "password": "wrong"})
    assert r.status_code == 401, r.status_code
    print("  PASS — wrong string credentials still 401")
    passed += 1

    print(f"\n{passed} passed, 0 failed.")


if __name__ == "__main__":
    run()
