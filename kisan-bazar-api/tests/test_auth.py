"""
Authentication and security test suite. Run: python3 tests/test_auth.py

This is the suite that actually matters for the "can someone breach it"
question — it walks the real login flow end to end via HTTP (not the
issue_token() shortcut the other test files use for convenience), and
specifically tries to do the things an attacker would try: forge a
token, replay a used code, brute-force a code, guess someone else's
identity, and read another farmer's data with a token of their own.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seed
import app as app_module
import auth

PHONE = "9841000001"
OTHER_PHONE = "9841000002"


def run():
    seed.seed()
    client = app_module.app.test_client()
    # reset in-memory rate-limit state between runs of this file
    auth._attempts.clear()
    auth._dev_outbox.clear()

    print("=== 1. Full real login flow: request-code -> verify -> get a working token ===")
    r = client.post("/v1/auth/request-code", json={"phone": PHONE})
    assert r.status_code == 200, r.get_json()
    assert len(auth._dev_outbox) == 1, "a code should have been 'sent' (appended to the dev outbox)"
    sent_code = auth._dev_outbox[-1]["code"]
    print(f"  code requested OK (never appears in the HTTP response body — only in the mock outbox)")

    r = client.post("/v1/auth/verify", json={"phone": PHONE, "code": sent_code})
    assert r.status_code == 200, r.get_json()
    token = r.get_json()["token"]
    print(f"  verified OK, got a token ({len(token)} chars)")

    r = client.get("/v1/listings/mine", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.get_json()
    print("  PASS — the token from the real HTTP flow actually authenticates a real request")

    print("\n=== 2. A used code cannot be reused (replay protection) ===")
    r = client.post("/v1/auth/verify", json={"phone": PHONE, "code": sent_code})
    assert r.status_code == 401, r.get_json()
    print(f"  PASS — rejected with 401: {r.get_json()['error']}")

    print("\n=== 3. A wrong code is rejected ===")
    auth._attempts.clear()
    r = client.post("/v1/auth/request-code", json={"phone": PHONE})
    real_code = auth._dev_outbox[-1]["code"]
    wrong_code = f"{(int(real_code) + 1) % 1000000:06d}"
    r = client.post("/v1/auth/verify", json={"phone": PHONE, "code": wrong_code})
    assert r.status_code == 401, r.get_json()
    print(f"  PASS — wrong code rejected with 401")

    print("\n=== 4. A code for phone A cannot verify phone B, even if guessed correctly ===")
    r = client.post("/v1/auth/verify", json={"phone": OTHER_PHONE, "code": real_code})
    assert r.status_code == 401, r.get_json()
    print(f"  PASS — a code is bound to its own phone number, not just 'any valid code'")

    print("\n=== 5. Codes never appear in ANY HTTP response body ===")
    auth._attempts.clear()
    r = client.post("/v1/auth/request-code", json={"phone": PHONE})
    body_text = r.get_data(as_text=True)
    real_code = auth._dev_outbox[-1]["code"]
    assert real_code not in body_text, \
        "CRITICAL: the verification code must never be returned in the API response"
    print(f"  PASS — response body contains no trace of the actual code")

    print("\n=== 6. Rate limiting: request-code capped per phone ===")
    auth._attempts.clear()
    results = []
    for i in range(5):
        r = client.post("/v1/auth/request-code", json={"phone": PHONE})
        results.append(r.status_code)
    assert 429 in results, f"expected a 429 somewhere in {results} after repeated requests"
    print(f"  PASS — status codes over 5 rapid requests: {results} (429 = rate limited)")

    print("\n=== 7. Rate limiting: verify attempts capped per phone (anti-brute-force) ===")
    auth._attempts.clear()
    client.post("/v1/auth/request-code", json={"phone": PHONE})
    verify_results = []
    for i in range(8):
        r = client.post("/v1/auth/verify", json={"phone": PHONE, "code": "000000"})
        verify_results.append(r.status_code)
    assert 429 in verify_results, f"expected a 429 somewhere in {verify_results}"
    print(f"  PASS — status codes over 8 rapid wrong-code attempts: {verify_results}")

    print("\n=== 8. Protected endpoints reject requests with NO token ===")
    protected = [
        ("POST", "/v1/listings", {"farmer_phone": PHONE, "coop_id": "kavre-veg", "crop_code": "KAULI", "grade": "A", "quantity_kg": 10}),
        ("GET",  "/v1/listings/mine", None),
        ("POST", f"/v1/farmers/{PHONE}/land-scan", {"land_type": "khet", "confidence": "good"}),
        ("GET",  f"/v1/farmers/{PHONE}/land-scan", None),
    ]
    for method, path, body in protected:
        r = client.post(path, json=body) if method == "POST" else client.get(path)
        assert r.status_code == 401, f"{method} {path} should require auth, got {r.status_code}: {r.get_json()}"
    print(f"  PASS — all {len(protected)} protected endpoints reject unauthenticated requests")

    print("\n=== 9. A malformed/forged token is rejected, not silently trusted ===")
    forged_attempts = [
        "Bearer garbage",
        "Bearer " + "a"*200,
        "Bearer eyJwaG9uZSI6IjAwMDAwMDAwMDAiLCJleHAiOjk5OTk5OTk5OTl9.deadbeef",  # right shape, wrong signature
        "NotBearer sometoken",
        "",
    ]
    for auth_header in forged_attempts:
        headers = {"Authorization": auth_header} if auth_header else {}
        r = client.get("/v1/listings/mine", headers=headers)
        assert r.status_code == 401, f"forged header '{auth_header[:40]}' should be rejected, got {r.status_code}"
    print(f"  PASS — all {len(forged_attempts)} forged/malformed tokens rejected")

    print("\n=== 10. A token's signature actually matters — tampering with the payload breaks it ===")
    real_token = auth.issue_token(PHONE)
    payload_part, sig_part = real_token.rsplit(".", 1)
    # flip a character in the payload — if verify_token still accepted this,
    # signatures wouldn't be doing anything
    tampered_payload = payload_part[:-1] + ("A" if payload_part[-1] != "A" else "B")
    tampered_token = f"{tampered_payload}.{sig_part}"
    result = auth.verify_token(tampered_token)
    assert result is None, "a tampered payload with the OLD signature must not verify"
    print("  PASS — tampering with the token payload invalidates it")

    print("\n=== 11. Cross-farmer impersonation: a valid token for A cannot act as B ===")
    token_a = auth.issue_token(PHONE)
    r = client.post("/v1/listings", json={
        "farmer_phone": OTHER_PHONE, "coop_id": "kavre-veg", "crop_code": "KAULI", "grade": "A", "quantity_kg": 10,
    }, headers={"Authorization": f"Bearer {token_a}"})
    assert r.status_code == 403, r.get_json()
    print(f"  PASS — rejected with 403: {r.get_json()['error']}")

    print("\n=== 12. Staff-only endpoint rejects a regular farmer, even authenticated ===")
    seed.seed()  # fresh state so lot ids are predictable-ish; just need ANY forming lot check to 403 before reaching lot lookup
    token_farmer = auth.issue_token(PHONE)
    r = client.post("/v1/lots/NONEXISTENT/confirm", json={"rate_per_kg": 50},
                     headers={"Authorization": f"Bearer {token_farmer}"})
    # must be 403 (not staff) even though the lot doesn't exist — authorization
    # is checked before the resource lookup, so existence of a lot is never
    # leaked to a caller who wouldn't be allowed to act on it anyway
    assert r.status_code == 403, f"expected 403 (staff-only) before 404 (not found), got {r.status_code}: {r.get_json()}"
    print(f"  PASS — authorization checked before resource existence (no information leak)")

    print("\n=== 13. Unhandled server errors never leak internals ===")
    r = client.post("/v1/listings", json="not-a-json-object", headers={"Authorization": f"Bearer {token_a}"})
    assert r.status_code < 500 or "Traceback" not in r.get_data(as_text=True)
    body_text = r.get_data(as_text=True)
    assert "Traceback" not in body_text and "File \"" not in body_text, \
        "response must never contain a Python stack trace"
    print(f"  PASS — malformed request handled without leaking a stack trace (status {r.status_code})")

    print("\n=== 14. Security headers are present on responses ===")
    r = client.get("/v1/market/lots")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    print("  PASS — X-Content-Type-Options and X-Frame-Options set")

    print("\n=== 15. quantity_kg bounds are enforced (reject absurd/negative values) ===")
    bad_quantities = [-5, 0, 999999, "50", None]
    token_farmer = auth.issue_token(PHONE)
    for q in bad_quantities:
        r = client.post("/v1/listings", json={
            "farmer_phone": PHONE, "coop_id": "kavre-veg", "crop_code": "KAULI", "grade": "A", "quantity_kg": q,
        }, headers={"Authorization": f"Bearer {token_farmer}"})
        assert r.status_code == 400, f"quantity_kg={q!r} should be rejected, got {r.status_code}: {r.get_json()}"
    print(f"  PASS — all {len(bad_quantities)} invalid quantities rejected with 400")

    print("\nAll auth/security checks passed.")


if __name__ == "__main__":
    run()
