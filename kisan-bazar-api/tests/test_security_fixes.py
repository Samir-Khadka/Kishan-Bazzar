"""
Security regression tests for the Oct 2026 hardening pass.
Run: python3 tests/test_security_fixes.py

Covers the holes found in the external code review:
  1. POST /v1/auth/signup required a valid token for the phone (no more
     anonymous registration), never honors is_staff/role from the body,
     never returns a plaintext OTP.
  2. GET /v1/dev/latest-code/<phone> is 404 unless KISAN_BAZAR_DEMO_MODE=1.
  3. GET /v1/admin/overview requires staff auth (was fully open, and 500'd).
  4. POST /v1/listings ignores client-supplied coop_id (uses the farmer's own).
  5. POST /v1/orders cannot oversell a lot (ordered quantity is tracked).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading
import seed
import app as app_module
import routes_auth  # owns the DEMO_MODE flag the auth routes read
import auth

PHONE = "9841000001"       # Sarita Tamang, farmer
OTHER_PHONE = "9841000002" # Ram Bahadur K.C., farmer
STAFF_PHONE = "9801100000" # Kavre Co-op Desk, staff


def get_token(client, phone):
    auth._attempts.clear()
    r = client.post("/v1/auth/request-code", json={"phone": phone})
    assert r.status_code == 200, r.get_json()
    code = auth._dev_outbox[-1]["code"]
    r = client.post("/v1/auth/verify", json={"phone": phone, "code": code})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["token"]


def run():
    seed.seed()
    client = app_module.app.test_client()
    auth._attempts.clear()
    auth._dev_outbox.clear()
    passed = 0

    print("=== 1. Signup without a token is rejected ===")
    r = client.post("/v1/auth/signup", json={"phone": "9841099999", "name": "Attacker"})
    assert r.status_code == 401, r.get_json()
    print(f"  PASS — rejected with 401")
    passed += 1

    print("\n=== 2. Signup cannot self-grant staff ===")
    token = get_token(client, PHONE)
    r = client.post("/v1/auth/signup",
                    json={"phone": PHONE, "name": "Sarita Tamang", "role": "admin", "is_staff": True},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400, r.get_json()
    print(f"  PASS — invalid role rejected with 400")
    passed += 1

    print("\n=== 3. Signup for someone else's number is rejected ===")
    r = client.post("/v1/auth/signup",
                    json={"phone": OTHER_PHONE, "name": "Impostor"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, r.get_json()
    print(f"  PASS — rejected with 403")
    passed += 1

    print("\n=== 4. Dev OTP endpoint is 404 without DEMO_MODE ===")
    assert routes_auth.DEMO_MODE is False, "tests run with DEMO_MODE off"
    client.post("/v1/auth/request-code", json={"phone": PHONE})
    r = client.get(f"/v1/dev/latest-code/{PHONE}")
    assert r.status_code == 404, r.get_json()
    print(f"  PASS — 404 when DEMO_MODE is off")
    passed += 1

    print("\n=== 5. Dev OTP endpoint works with DEMO_MODE on ===")
    routes_auth.DEMO_MODE = True
    try:
        r = client.get(f"/v1/dev/latest-code/{PHONE}")
        assert r.status_code == 200, r.get_json()
        assert "code" in r.get_json()
        print(f"  PASS — 200 with code when DEMO_MODE is on")
    finally:
        routes_auth.DEMO_MODE = False
    passed += 1

    print("\n=== 6. Admin overview requires staff auth ===")
    r = client.get("/v1/admin/overview")
    assert r.status_code == 401, r.get_json()
    r = client.get("/v1/admin/overview", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, r.get_json()
    staff_token = get_token(client, STAFF_PHONE)
    r = client.get("/v1/admin/overview", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 200, r.get_json()
    assert "sms_outbox" not in r.get_json(), "OTP outbox must never be exposed"
    print(f"  PASS — 401 anonymous, 403 farmer, 200 staff, no OTP leak")
    passed += 1

    print("\n=== 7. Listing ignores forged coop_id ===")
    r = client.post("/v1/listings",
                    json={"farmer_phone": PHONE, "coop_id": "evil-coop",
                          "crop_code": "GOLB", "grade": "A", "quantity_kg": 10},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201, r.get_json()
    listing_id = r.get_json()["listing_id"]
    r = client.get("/v1/listings/mine", headers={"Authorization": f"Bearer {token}"})
    mine = [l for l in r.get_json() if l["id"] == listing_id]
    assert mine and mine[0]["coop_id"] == "kavre-veg", mine
    print(f"  PASS — listing landed in the farmer's real coop, not 'evil-coop'")
    passed += 1

    print("\n=== 8. Orders cannot oversell a lot ===")
    # Build a ready lot: two in-radius farmers list enough to trip the 130kg target.
    t2 = get_token(client, OTHER_PHONE)
    for tok, ph, kg in [(token, PHONE, 80), (t2, OTHER_PHONE, 80)]:
        r = client.post("/v1/listings",
                        json={"farmer_phone": ph, "coop_id": "kavre-veg",
                              "crop_code": "KAULI", "grade": "A", "quantity_kg": kg},
                        headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 201, r.get_json()
    r = client.get("/v1/lots", headers={"Authorization": f"Bearer {staff_token}"})
    lots = [l for l in r.get_json()["data"] if l["status"] == "forming" and l["crop_code"] == "KAULI"]
    assert lots, "expected a forming KAULI lot"
    lot_id = lots[0]["id"]
    # Staff prices the lot -> status becomes 'ready' and orderable.
    r = client.post(f"/v1/lots/{lot_id}/confirm", json={"rate_per_kg": 45},
                    headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 200, r.get_json()
    r = client.get("/v1/lots", headers={"Authorization": f"Bearer {staff_token}"})
    lot = [l for l in r.get_json()["data"] if l["id"] == lot_id][0]
    assert lot["status"] == "ready", lot
    pooled = lot["pooled_kg"]
    yak_headers = {"Authorization": f"Bearer {auth.issue_token('9800000001')}"}
    r = client.post("/v1/orders", json={"buyer_name": "Hotel Yak", "buyer_phone": "9800000001",
                                        "lot_id": lot_id, "quantity_kg": pooled},
                    headers=yak_headers)
    assert r.status_code == 201, r.get_json()
    r = client.post("/v1/orders", json={"buyer_name": "Hotel Yak", "buyer_phone": "9800000001",
                                        "lot_id": lot_id, "quantity_kg": 1},
                    headers=yak_headers)
    assert r.status_code == 400, r.get_json()
    print(f"  PASS — first order took all {pooled}kg, second order rejected")
    passed += 1

    # --- role cannot be self-escalated to staff ---
    r = client.post("/v1/auth/request-code", json={"phone": "9899000011"})
    code = auth._dev_outbox[-1]["code"]
    r = client.post("/v1/auth/verify",
                    json={"phone": "9899000011", "code": code})
    reg_token = r.get_json()["token"]
    r = client.post("/v1/auth/signup",
                    json={"phone": "9899000011", "name": "Mallory",
                          "role": "staff"},
                    headers={"Authorization": f"Bearer {reg_token}"})
    assert r.status_code == 400, r.get_json()
    assert "staff" in r.get_json()["error"].lower() or "role" in r.get_json()["error"].lower()
    # buyer role is accepted
    r = client.post("/v1/auth/signup",
                    json={"phone": "9899000011", "name": "Mallory",
                          "role": "buyer"},
                    headers={"Authorization": f"Bearer {reg_token}"})
    assert r.status_code == 201, r.get_json()
    r = client.get("/v1/me", headers={"Authorization": f"Bearer {reg_token}"})
    assert r.get_json()["role"] == "buyer", r.get_json()
    print("  PASS — staff role rejected at signup, buyer role stored and returned by /v1/me")
    passed += 1

    print("\n=== 10. Offers require the buyer's own login ===")
    # Register a buyer account to bid with.
    r = client.post("/v1/auth/request-code", json={"phone": "9899000012"})
    code = auth._dev_outbox[-1]["code"]
    r = client.post("/v1/auth/verify", json={"phone": "9899000012", "code": code})
    buyer_token = r.get_json()["token"]
    r = client.post("/v1/auth/signup",
                    json={"phone": "9899000012", "name": "Bidder", "role": "buyer"},
                    headers={"Authorization": f"Bearer {buyer_token}"})
    assert r.status_code == 201, r.get_json()
    offer_body = {"buyer_name": "Bidder", "buyer_phone": "9899000012",
                  "lot_id": "KVR-118", "bid_rate_per_kg": 40, "quantity_kg": 5}
    # Anonymous bid rejected.
    r = client.post("/v1/offers", json=offer_body)
    assert r.status_code == 401, r.get_json()
    # A valid token for a DIFFERENT phone cannot bid as this buyer.
    r = client.post("/v1/offers", json=offer_body,
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, r.get_json()
    # The buyer's own token works.
    r = client.post("/v1/offers", json=offer_body,
                    headers={"Authorization": f"Bearer {buyer_token}"})
    assert r.status_code == 201, r.get_json()
    offer_id = r.get_json()["offer_id"]
    print("  PASS — 401 anonymous, 403 cross-phone, 201 own phone")
    passed += 1

    print("\n=== 11. Offer listing is private: buyers see only their own ===")
    r = client.get("/v1/offers")
    assert r.status_code == 401, r.get_json()
    r = client.get("/v1/offers", headers={"Authorization": f"Bearer {buyer_token}"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json() and all(o["buyer_phone"] == "9899000012"
                                for o in r.get_json()), r.get_json()
    r = client.get("/v1/offers?buyer_phone=9841000001",
                   headers={"Authorization": f"Bearer {buyer_token}"})
    assert r.status_code == 403, r.get_json()
    r = client.get("/v1/offers", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 200 and len(r.get_json()) >= 1, r.get_json()
    print("  PASS — 401 anonymous, buyers scoped to own phone, staff see all")
    passed += 1

    print("\n=== 12. Concurrent orders cannot oversell a lot ===")
    # Fresh ready lot: two in-radius farmers list MULA past the pooling target.
    for tok, ph, kg in [(token, PHONE, 80), (t2, OTHER_PHONE, 80)]:
        r = client.post("/v1/listings",
                        json={"farmer_phone": ph, "coop_id": "kavre-veg",
                              "crop_code": "MULA", "grade": "A", "quantity_kg": kg},
                        headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 201, r.get_json()
    r = client.get("/v1/lots", headers={"Authorization": f"Bearer {staff_token}"})
    race_lots = [l for l in r.get_json()["data"]
                 if l["status"] == "forming" and l["crop_code"] == "MULA"]
    assert race_lots, "expected a forming MULA lot"
    race_lot_id = race_lots[0]["id"]
    r = client.post(f"/v1/lots/{race_lot_id}/confirm", json={"rate_per_kg": 30},
                    headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 200, r.get_json()
    r = client.get("/v1/lots", headers={"Authorization": f"Bearer {staff_token}"})
    race_lot = [l for l in r.get_json()["data"] if l["id"] == race_lot_id][0]
    race_pooled = race_lot["pooled_kg"]
    # Five buyers each try to buy the WHOLE lot at the same instant.
    # Each races with their OWN token (orders require buyer auth now);
    # tokens are issued directly to keep the threads setup-only.
    race_tokens = {i: auth.issue_token(f"98610000{i:02d}") for i in range(5)}
    results = []
    def fire(i):
        c = app_module.app.test_client()
        rr = c.post("/v1/orders",
                    json={"buyer_name": f"Racer {i}", "buyer_phone": f"98610000{i:02d}",
                          "lot_id": race_lot_id, "quantity_kg": race_pooled},
                    headers={"Authorization": f"Bearer {race_tokens[i]}"})
        results.append(rr.status_code)
    threads = [threading.Thread(target=fire, args=(i,)) for i in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert results.count(201) == 1, f"expected exactly one winner, got {results}"
    assert results.count(400) == 4, f"expected four rejections, got {results}"
    import db as db_module
    conn = db_module.get_db()
    total = conn.execute("SELECT COALESCE(SUM(quantity_kg),0) AS t FROM orders "
                         "WHERE lot_id=?", (race_lot_id,)).fetchone()["t"]
    conn.close()
    assert total == race_pooled, f"oversold: {total} > {race_pooled}"
    print(f"  PASS — 1 winner, 4 rejections, {total}kg sold of {race_pooled}kg")
    passed += 1

    print("\n=== 13. Accepting an offer cannot oversell ===")
    # race_lot_id is fully consumed above; an accepted offer for 1 more kg
    # must be refused.
    r = client.post("/v1/offers",
                    json={"buyer_name": "Bidder", "buyer_phone": "9899000012",
                          "lot_id": race_lot_id, "bid_rate_per_kg": 30,
                          "quantity_kg": 1},
                    headers={"Authorization": f"Bearer {buyer_token}"})
    assert r.status_code == 201, r.get_json()
    greedy = r.get_json()["offer_id"]
    r = client.post(f"/v1/offers/{greedy}/decide", json={"decision": "accepted"},
                    headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 409, r.get_json()
    # The earlier legitimate offer (5kg on lot_id, still plenty left) still
    # converts fine.
    r = client.post(f"/v1/offers/{offer_id}/decide", json={"decision": "accepted"},
                    headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 200, r.get_json()
    print("  PASS — accept on exhausted lot 409, valid accept still converts")
    passed += 1

    print("\n=== 14. Admin login is rate-limited ===")
    auth._attempts.clear()
    for i in range(10):
        r = client.post("/v1/auth/admin",
                        json={"username": "admin", "password": "wrong-guess"})
        assert r.status_code == 401, (i, r.get_json())
    r = client.post("/v1/auth/admin",
                    json={"username": "admin", "password": "wrong-guess"})
    assert r.status_code == 429, r.get_json()
    print("  PASS — 10 bad guesses allowed, 11th gets 429")
    passed += 1

    print(f"\nAll {passed} security regression checks passed.")


if __name__ == "__main__":
    run()
