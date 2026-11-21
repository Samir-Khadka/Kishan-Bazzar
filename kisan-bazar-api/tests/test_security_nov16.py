"""Security regression tests for the Nov 16 2026 hardening pass.

Covers the holes found in the security-focus review:
  1. Per-IP rate limits collapsed behind the reverse proxy: without
     ProxyFix, request.remote_addr is the PROXY's IP for every client,
     so all per-IP limits (assistant 30/min, admin login 10/10 min,
     request-code 60/10 min, orders/offers 10/10 min) share ONE
     bucket — one chatty user locks every other user out, and the
     limits stop distinguishing clients at all. ProxyFix(x_for=1)
     restores the real client IP from the LAST X-Forwarded-For hop
     (the one Render itself appends), so distinct clients get
     distinct buckets and a client-supplied fake first hop is
     ignored.
  2. Wrong-typed JSON values 500'd on authenticated write paths:
     .strip() on a list/dict (AttributeError) or a non-string reaching
     a SQLite bind (InterfaceError) — all now honest 400s, with the
     valid paths still working.

Run: python3 tests/test_security_nov16.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seed
import app as app_module
import auth

FARMER = "9841000001"
STAFF = "9801100000"
IP_A = "203.0.113.10"   # assistant-bucket test
IP_B = "203.0.113.11"   # a different client
IP_C = "203.0.113.12"   # wrong-type probes (rate-limit headroom)


def chat(client, ip, message="hello"):
    return client.post("/v1/assistant/chat",
                       json={"message": message, "session_id": f"s-{ip}"},
                       headers={"X-Forwarded-For": ip})


def run():
    seed.seed()
    client = app_module.app.test_client()
    auth._attempts.clear()
    farmer_tok = auth.issue_token(FARMER)
    staff_tok = auth.issue_token(STAFF)
    fh = {"Authorization": f"Bearer {farmer_tok}"}
    sh = {"Authorization": f"Bearer {staff_tok}"}
    passed = 0

    print("=== 1. ProxyFix: a request with no X-Forwarded-For still works ===")
    r = client.post("/v1/assistant/chat", json={"message": "hello"},
                    environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 200, r.get_json()
    print("  PASS — direct (no-proxy) request unaffected")
    passed += 1

    print("\n=== 2. ProxyFix: 30 chats from one client IP OK, 31st 429 ===")
    for i in range(30):
        r = chat(client, IP_A)
        assert r.status_code == 200, (i, r.get_json())
    r = chat(client, IP_A)
    assert r.status_code == 429, r.get_json()
    print("  PASS — 31st chat from the same client IP rejected with 429")
    passed += 1

    print("\n=== 3. ProxyFix: a DIFFERENT client IP is unaffected by (2) ===")
    r = chat(client, IP_B)
    assert r.status_code == 200, r.get_json()
    print("  PASS — second client IP still gets 200 after the first hit its cap")
    passed += 1

    print("\n=== 4. ProxyFix: only the LAST X-Forwarded-For hop is trusted ===")
    # "198.51.100.99" is a client-supplied fake first hop; the last hop
    # (203.0.113.10, the proxy-appended real IP) is the bucket that
    # counts — and that bucket is already exhausted from (2).
    r = chat(client, "198.51.100.99, 203.0.113.10")
    assert r.status_code == 429, r.get_json()
    print("  PASS — spoofed first hop ignored, exhausted last-hop bucket still 429")
    passed += 1

    print("\n=== 5. Wrong types are 400s, not 500s (rentals) ===")
    base_rental = {"machine_type": "tractor", "machine_name": "T",
                   "location": "Kavre", "contact_phone": FARMER,
                   "rate_per_day": 100}
    for field in ("machine_type", "machine_name", "location", "contact_phone"):
        bad = dict(base_rental, **{field: ["x"]})
        r = client.post("/v1/rentals", json=bad, headers=fh,
                        environ_base={"REMOTE_ADDR": IP_C})
        assert r.status_code == 400, (field, r.status_code, r.get_json())
    r = client.post("/v1/rentals", json=base_rental, headers=fh,
                    environ_base={"REMOTE_ADDR": IP_C})
    assert r.status_code == 201, r.get_json()
    print("  PASS — list machine_type/name/location/contact_phone all 400; valid rental 201")
    passed += 1

    print("\n=== 6. Wrong types are 400s, not 500s (rental booking) ===")
    # The booking date is relative to TODAY in Nepal time: the
    # endpoint rejects past dates, so a hardcoded date went stale
    # the day after it was written (same class as the Oct-23
    # rentals block). Book tomorrow; the geometry is unchanged.
    from datetime import datetime, timedelta
    try:
        from zoneinfo import ZoneInfo
        tomorrow = (datetime.now(ZoneInfo("Asia/Kathmandu"))
                    + timedelta(days=1)).date().isoformat()
    except Exception:
        tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
    r = client.post("/v1/rentals/1/book",
                    json={"days": 2, "book_date": [tomorrow]},
                    headers=fh, environ_base={"REMOTE_ADDR": IP_C})
    assert r.status_code == 400, (r.status_code, r.get_json())
    r = client.post("/v1/rentals/1/book",
                    json={"days": 2, "book_date": tomorrow},
                    headers=fh, environ_base={"REMOTE_ADDR": IP_C})
    assert r.status_code == 201, r.get_json()
    print("  PASS — list book_date 400; valid booking 201")
    passed += 1

    print("\n=== 7. Wrong types are 400s, not 500s (orders + offers lot_id) ===")
    order = {"buyer_name": "X", "buyer_phone": FARMER,
             "lot_id": "DHD-207", "quantity_kg": 5}
    for bad_lot in (["DHD-207"], {"id": "DHD-207"}, 123):
        r = client.post("/v1/orders", json=dict(order, lot_id=bad_lot),
                        headers=fh, environ_base={"REMOTE_ADDR": IP_C})
        assert r.status_code == 400, (bad_lot, r.status_code, r.get_json())
    r = client.post("/v1/orders", json=order, headers=fh,
                    environ_base={"REMOTE_ADDR": IP_C})
    assert r.status_code == 201, r.get_json()
    offer = {"buyer_name": "X", "buyer_phone": FARMER, "lot_id": "DHD-207",
             "bid_rate_per_kg": 45, "quantity_kg": 5}
    r = client.post("/v1/offers", json=dict(offer, lot_id=["DHD-207"]),
                    headers=fh, environ_base={"REMOTE_ADDR": IP_C})
    assert r.status_code == 400, (r.status_code, r.get_json())
    r = client.post("/v1/offers", json=offer, headers=fh,
                    environ_base={"REMOTE_ADDR": IP_C})
    assert r.status_code == 201, r.get_json()
    print("  PASS — list/dict/int lot_id 400 on both endpoints; valid order + offer 201")
    passed += 1

    print("\n=== 8. Wrong types are 400s, not 500s (listings crop_code) ===")
    listing = {"farmer_phone": FARMER, "coop_id": "kavre-veg",
               "crop_code": "GOLB", "grade": "A", "quantity_kg": 10}
    for bad_crop in (["GOLB"], {"code": "GOLB"}):
        r = client.post("/v1/listings", json=dict(listing, crop_code=bad_crop),
                        headers=fh, environ_base={"REMOTE_ADDR": IP_C})
        assert r.status_code == 400, (bad_crop, r.status_code, r.get_json())
    r = client.post("/v1/listings", json=listing, headers=fh,
                    environ_base={"REMOTE_ADDR": IP_C})
    assert r.status_code == 201, r.get_json()
    print("  PASS — list/dict crop_code 400; valid listing 201")
    passed += 1

    print("\n=== 9. Wrong types are 400s, not 500s (decide + settle) ===")
    r = client.post("/v1/offers/1/decide", json={"decision": ["accepted"]},
                    headers=sh, environ_base={"REMOTE_ADDR": IP_C})
    assert r.status_code == 400, (r.status_code, r.get_json())
    r = client.post("/v1/orders/1/settle",
                    json={"method": ["cash"], "amount": 100},
                    headers=sh, environ_base={"REMOTE_ADDR": IP_C})
    assert r.status_code == 400, (r.status_code, r.get_json())
    r = client.post("/v1/orders/1/settle",
                    json={"method": "cash", "amount": 100, "reference": ["x"]},
                    headers=sh, environ_base={"REMOTE_ADDR": IP_C})
    assert r.status_code == 400, (r.status_code, r.get_json())
    print("  PASS — list decision/method/reference all 400")
    passed += 1

    print("\n=== 10. Wrong types are 400s, not 500s (crop doctor) ===")
    diag = {"crop_code": "GOLB", "symptoms": "पातमा कालो दाग"}
    for field in ("crop_code", "symptoms", "photo_ref"):
        r = client.post("/v1/crop-doctor/diagnose",
                        json=dict(diag, **{field: ["x"]}),
                        headers=fh, environ_base={"REMOTE_ADDR": IP_C})
        assert r.status_code == 400, (field, r.status_code, r.get_json())
    r = client.post("/v1/crop-doctor/diagnose", json=diag, headers=fh,
                    environ_base={"REMOTE_ADDR": IP_C})
    assert r.status_code == 200, r.get_json()
    print("  PASS — list crop_code/symptoms/photo_ref all 400; valid diagnosis 200")
    passed += 1

    print("\n=== 11. Wrong types are 400s, not 500s (land scan signals) ===")
    scan = {"land_type": "bari", "confidence": "good", "color_key": "dark"}
    for field in ("color_key", "texture", "drainage"):
        r = client.post(f"/v1/farmers/{FARMER}/land-scan",
                        json=dict(scan, **{field: ["x"]}),
                        headers=fh, environ_base={"REMOTE_ADDR": IP_C})
        assert r.status_code == 400, (field, r.status_code, r.get_json())
    r = client.post(f"/v1/farmers/{FARMER}/land-scan", json=scan,
                    headers=fh, environ_base={"REMOTE_ADDR": IP_C})
    assert r.status_code == 201, r.get_json()
    print("  PASS — list color_key/texture/drainage all 400; valid scan 201")
    passed += 1

    print("\n=== 12. Wrong types are 400s, not 500s (signup) ===")
    new_phone = "9841000099"
    new_tok = auth.issue_token(new_phone)
    nh = {"Authorization": f"Bearer {new_tok}"}
    signup = {"phone": new_phone, "name": "Test Farmer", "role": "farmer"}
    for field in ("name", "role", "coop_id", "payout_method"):
        r = client.post("/v1/auth/signup",
                        json=dict(signup, **{field: ["x"]}),
                        headers=nh, environ_base={"REMOTE_ADDR": IP_C})
        assert r.status_code == 400, (field, r.status_code, r.get_json())
    r = client.post("/v1/auth/signup", json=signup, headers=nh,
                    environ_base={"REMOTE_ADDR": IP_C})
    assert r.status_code == 201, r.get_json()
    print("  PASS — list name/role/coop_id/payout_method all 400; valid signup 201")
    passed += 1

    print(f"\nAll {passed} Nov-16 security checks passed.")


if __name__ == "__main__":
    run()
