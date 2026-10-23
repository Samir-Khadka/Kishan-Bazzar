"""
End-to-end test of the aggregation API, using Flask's test client (no
server process needed). Run: python3 tests/test_api.py

Walks the exact scenario used throughout this project: Kavre farmers
listing cauliflower, pooling past a 130kg threshold, a co-op confirming
the lot, and a buyer ordering against it — with assertions at each step,
including the one that matters most: the far-away farmer must NOT be
pooled into the lot.

Every mutating call now requires a bearer token (see auth.py / the
security hardening pass) — tokens are issued directly via auth.issue_token()
here rather than walking the full request-code/verify HTTP flow each time,
since that flow itself is what tests/test_auth.py exists to test
thoroughly. Using it here too would just be redundant setup noise.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seed
import app as app_module
import auth

STAFF_PHONE = "9801100000"


def token_header(phone):
    return {"Authorization": f"Bearer {auth.issue_token(phone)}"}


def post(client, path, body, as_phone=None):
    headers = token_header(as_phone) if as_phone else {}
    return client.post(path, json=body, headers=headers)


def get(client, path, as_phone=None):
    headers = token_header(as_phone) if as_phone else {}
    return client.get(path, headers=headers)


def run():
    seed.seed()
    client = app_module.app.test_client()

    print("=== 1. First listing: Sarita, 38kg cauliflower grade A ===")
    r = post(client, "/v1/listings", {
        "farmer_phone": "9841000001", "coop_id": "kavre-veg",
        "crop_code": "KAULI", "grade": "A", "quantity_kg": 38,
    }, as_phone="9841000001")
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    print(f"  listing_id={body['listing_id']}, lot_formed={'lot_formed' in body}")
    assert "lot_formed" not in body, "38kg alone should NOT cross the 130kg threshold"

    print("\n=== 2. Second listing: Ram Bahadur, 52kg (total 90kg, still short) ===")
    r = post(client, "/v1/listings", {
        "farmer_phone": "9841000002", "coop_id": "kavre-veg",
        "crop_code": "KAULI", "grade": "A", "quantity_kg": 52,
    }, as_phone="9841000002")
    body = r.get_json()
    assert "lot_formed" not in body, "90kg total should still be short of 130kg"
    print(f"  no lot yet, as expected (90kg pooled so far)")

    print("\n=== 3. Far-away farmer (Bishnu, ~50km out) lists 100kg ===")
    r = post(client, "/v1/listings", {
        "farmer_phone": "9841000005", "coop_id": "kavre-veg",
        "crop_code": "KAULI", "grade": "A", "quantity_kg": 100,
    }, as_phone="9841000005")
    body = r.get_json()
    assert "lot_formed" not in body, \
        "Bishnu's 100kg is outside the 18km collection radius and must NOT count toward the threshold"
    print("  PASS — out-of-radius listing correctly excluded from the pool")

    print("\n=== 4. Krishna, 44kg — should now cross 130kg (90 + 44 = 134) ===")
    r = post(client, "/v1/listings", {
        "farmer_phone": "9841000004", "coop_id": "kavre-veg",
        "crop_code": "KAULI", "grade": "A", "quantity_kg": 44,
    }, as_phone="9841000004")
    body = r.get_json()
    assert "lot_formed" in body, f"expected a lot to form at 134kg total, got: {body}"
    lot = body["lot_formed"]
    print(f"  lot formed: {lot['lot_id']}, pooled_kg={lot['pooled_kg']}")
    assert lot["pooled_kg"] == 130, f"expected exactly 130kg pooled (the target), got {lot['pooled_kg']}"
    contributors = {c["farmer"] for c in lot["contributions"]}
    assert "Bishnu Ghimire" not in contributors, "far-away farmer must not be a contributor"
    print(f"  contributors: {[c['farmer'] + ' ' + str(c['kg']) + 'kg' for c in lot['contributions']]}")

    lot_id = lot["lot_id"]

    print("\n=== 5. Krishna's listing should now be partially_pooled (44kg listed, only ~40 used) ===")
    r = get(client, "/v1/listings/mine", as_phone="9841000004")
    listings = r.get_json()
    krishna_listing = listings[0]
    print(f"  status={krishna_listing['status']}, committed={krishna_listing['quantity_committed_kg']}/{krishna_listing['quantity_kg']}")
    assert krishna_listing["status"] in ("partially_pooled", "pooled")

    print("\n=== 6. A regular farmer CANNOT confirm the lot (staff-only) ===")
    r = post(client, f"/v1/lots/{lot_id}/confirm", {"rate_per_kg": 58}, as_phone="9841000001")
    assert r.status_code == 403, r.get_json()
    print(f"  PASS — rejected with 403: {r.get_json()['error']}")

    print("\n=== 7. Co-op STAFF confirms the lot at Rs 58/kg ===")
    r = post(client, f"/v1/lots/{lot_id}/confirm", {"rate_per_kg": 58}, as_phone=STAFF_PHONE)
    assert r.status_code == 200, r.get_json()
    print(f"  {r.get_json()}")

    print("\n=== 8. Lot now appears in the buyer-facing market ===")
    r = get(client, "/v1/market/lots")
    market = r.get_json()
    assert any(l["id"] == lot_id for l in market), "confirmed lot should be visible to buyers"
    print(f"  market shows {len(market)} ready lot(s)")

    print("\n=== 9. Buyer orders 100kg from the lot ===")
    r = post(client, "/v1/orders", {
        "buyer_name": "Himalayan Hostel Kathmandu", "buyer_phone": "9841000001",
        "lot_id": lot_id, "quantity_kg": 100,
    }, as_phone="9841000001")
    assert r.status_code == 201, r.get_json()
    order = r.get_json()
    print(f"  order_id={order['order_id']}, total=Rs {order['total']}")
    assert order["total"] == 100 * 58, "order total should be quantity x confirmed rate"

    print("\n=== 9b. Order without buyer_phone should fail ===")
    r = post(client, "/v1/orders", {
        "buyer_name": "No Phone", "lot_id": lot_id, "quantity_kg": 10,
    }, as_phone="9841000001")
    assert r.status_code == 400, "buyer_phone is required"

    print("\n=== 9c. Order with invalid phone should fail ===")
    r = post(client, "/v1/orders", {
        "buyer_name": "Bad Phone", "buyer_phone": "12345", "lot_id": lot_id, "quantity_kg": 10,
    }, as_phone="9841000001")
    assert r.status_code == 400, "invalid phone should be rejected"

    print("\n=== 10. Over-ordering past the lot size should fail cleanly ===")
    r = post(client, "/v1/orders", {
        "buyer_name": "Greedy Buyer", "buyer_phone": "9841000002",
        "lot_id": lot_id, "quantity_kg": 9999,
    }, as_phone="9841000002")
    assert r.status_code == 400, "should reject an order larger than the lot"
    print(f"  PASS — rejected with {r.status_code}: {r.get_json()['error']}")

    print("\n=== 11. Confirming an already-ready lot again should fail (no double-pricing) ===")
    r = post(client, f"/v1/lots/{lot_id}/confirm", {"rate_per_kg": 99}, as_phone=STAFF_PHONE)
    assert r.status_code == 409, "should refuse to re-confirm a lot that's already ready"
    print(f"  PASS — rejected with {r.status_code}: {r.get_json()['error']}")

    print("\nAll checks passed.")


if __name__ == "__main__":
    run()
