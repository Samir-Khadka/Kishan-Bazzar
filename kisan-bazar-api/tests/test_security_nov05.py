"""Security regression tests for the Nov 5 2026 hardening pass.

Covers the holes found in the security-focus review:
  1. JSON booleans bypassed money-path validation: float(True) is 1.0,
     so bid_rate_per_kg / quantity_kg / counter_rate_per_kg /
     rate_per_day / amount / lat / lon sent as `true` were cast to 1.0
     BEFORE valid_positive_number saw them — re-admitting the bools
     the Nov 1 pass deliberately rejects. A `true` bid placed an offer
     at Rs 1/kg; a `true` rental rate listed a machine at Rs 1/day.
  2. Unbounded free-text on authenticated write paths: buyer_name,
     machine_name, location, contact_phone, reference, name,
     payout_method, symptoms, photo_ref, and the land-scan signals had
     no length caps — one account could bloat the long-lived SQLite
     file at will.
  3. POST /v1/crop-doctor/diagnose wrote a diagnoses row per call with
     no rate limit at all, unlike every sibling write endpoint.

Run: python3 tests/test_security_nov05.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seed
import app as app_module
import auth
import web

STAFF = "9801100000"
BUYER = "9841000005"
SARITA = "9841000001"
LOT = "MST-012"  # seeded ready lot (SYAU, rate 138)

IP_NUM = "203.0.113.11"
IP_TXT = "203.0.113.12"
IP_DIAG = "203.0.113.13"


def token(phone):
    return {"Authorization": f"Bearer {auth.issue_token(phone)}"}


def post(client, path, body, phone=None, ip="127.0.0.1"):
    kw = {"json": body, "environ_base": {"REMOTE_ADDR": ip}}
    if phone:
        kw["headers"] = token(phone)
    return client.post(path, **kw)


def run():
    seed.seed()
    client = app_module.app.test_client()
    auth._attempts.clear()
    passed = 0

    print("=== 1. parse_number rejects booleans, still coerces numerics ===")
    for bad in (True, False):
        try:
            web.parse_number(bad)
            raise AssertionError(f"parse_number accepted {bad!r}")
        except ValueError:
            pass
    assert web.parse_number("80") == 80.0
    assert web.parse_number(80) == 80.0
    assert web.parse_number(2.5) == 2.5
    for bad in ("abc", None):
        try:
            web.parse_number(bad)
            raise AssertionError(f"parse_number accepted {bad!r}")
        except (TypeError, ValueError):
            pass
    print("  PASS — bools rejected, numeric strings/ints/floats still coerce")
    passed += 1

    print("\n=== 2. make_offer: boolean bid_rate / quantity -> 400 ===")
    body = {"buyer_name": "T", "buyer_phone": BUYER, "lot_id": LOT,
            "bid_rate_per_kg": True, "quantity_kg": 10}
    r = post(client, "/v1/offers", body, phone=BUYER, ip=IP_NUM)
    assert r.status_code == 400, r.get_json()
    body["bid_rate_per_kg"] = 100
    body["quantity_kg"] = True
    r = post(client, "/v1/offers", body, phone=BUYER, ip=IP_NUM)
    assert r.status_code == 400, r.get_json()
    # string numbers still accepted
    body["bid_rate_per_kg"] = "100"
    body["quantity_kg"] = "10"
    r = post(client, "/v1/offers", body, phone=BUYER, ip=IP_NUM)
    assert r.status_code == 201, r.get_json()
    offer_id = r.get_json()["offer_id"]
    print("  PASS — bools 400, numeric strings still 201")
    passed += 1

    print("\n=== 3. decide_offer: boolean counter rate -> 400 ===")
    r = post(client, f"/v1/offers/{offer_id}/decide",
             {"decision": "countered", "counter_rate_per_kg": True},
             phone=STAFF, ip=IP_NUM)
    assert r.status_code == 400, r.get_json()
    r = post(client, f"/v1/offers/{offer_id}/decide",
             {"decision": "countered", "counter_rate_per_kg": "120"},
             phone=STAFF, ip=IP_NUM)
    assert r.status_code == 200, r.get_json()
    print("  PASS — bool counter 400, numeric-string counter 200")
    passed += 1

    print("\n=== 4. create_rental: boolean rate_per_day -> 400 ===")
    base = {"machine_type": "power_tiller", "machine_name": "M",
            "location": "L", "contact_phone": "9841000001"}
    r = post(client, "/v1/rentals", dict(base, rate_per_day=True),
             phone=SARITA, ip=IP_NUM)
    assert r.status_code == 400, r.get_json()
    r = post(client, "/v1/rentals", dict(base, rate_per_day="2500"),
             phone=SARITA, ip=IP_NUM)
    assert r.status_code == 201, r.get_json()
    print("  PASS — bool rate 400, numeric-string rate 201")
    passed += 1

    print("\n=== 5. settle_order: boolean amount -> 400 ===")
    r = post(client, "/v1/orders",
             {"buyer_name": "T", "buyer_phone": BUYER, "lot_id": LOT,
              "quantity_kg": 10},
             phone=BUYER, ip=IP_NUM)
    assert r.status_code == 201, r.get_json()
    order_id = r.get_json()["order_id"]
    total = r.get_json()["total"]
    r = post(client, f"/v1/orders/{order_id}/settle",
             {"amount": True, "method": "cash"}, phone=STAFF, ip=IP_NUM)
    assert r.status_code == 400, r.get_json()
    r = post(client, f"/v1/orders/{order_id}/settle",
             {"amount": total, "method": "cash"}, phone=STAFF, ip=IP_NUM)
    assert r.status_code == 201, r.get_json()
    print("  PASS — bool amount 400, exact total still settles")
    passed += 1

    print("\n=== 6. signup: boolean lat/lon -> 400 ===")
    NEWBIE = "9841000099"
    r = post(client, "/v1/auth/signup",
             {"phone": NEWBIE, "name": "N", "lat": True, "lon": 85.5},
             phone=NEWBIE, ip=IP_TXT)
    assert r.status_code == 400, r.get_json()
    r = post(client, "/v1/auth/signup",
             {"phone": NEWBIE, "name": "N", "lat": 27.7, "lon": 85.3},
             phone=NEWBIE, ip=IP_TXT)
    assert r.status_code == 201, r.get_json()
    print("  PASS — bool lat 400, real coords 201")
    passed += 1

    print("\n=== 7. Free-text length caps -> 400 ===")
    long = "x" * 121
    r = post(client, "/v1/offers",
             {"buyer_name": long, "buyer_phone": BUYER, "lot_id": LOT,
              "bid_rate_per_kg": 100, "quantity_kg": 10},
             phone=BUYER, ip=IP_TXT)
    assert r.status_code == 400, r.get_json()
    r = post(client, "/v1/orders",
             {"buyer_name": long, "buyer_phone": BUYER, "lot_id": LOT,
              "quantity_kg": 5},
             phone=BUYER, ip=IP_TXT)
    assert r.status_code == 400, r.get_json()
    r = post(client, "/v1/rentals",
             {"machine_type": "thresher", "machine_name": "M",
              "location": "L", "contact_phone": "1" * 33,
              "rate_per_day": 100},
             phone=SARITA, ip=IP_TXT)
    assert r.status_code == 400, r.get_json()
    r = post(client, "/v1/farmers/9841000001/land-scan",
             {"land_type": "bari", "confidence": "good",
              "texture": "y" * 65},
             phone=SARITA, ip=IP_TXT)
    assert r.status_code == 400, r.get_json()
    r = post(client, "/v1/auth/signup",
             {"phone": NEWBIE, "name": long, "lat": 27.7, "lon": 85.3},
             phone=NEWBIE, ip=IP_TXT)
    assert r.status_code == 400, r.get_json()
    print("  PASS — oversized buyer_name, contact_phone, texture, name all 400")
    passed += 1

    print("\n=== 8. crop-doctor: oversized symptoms/photo_ref -> 400 ===")
    r = post(client, "/v1/crop-doctor/diagnose",
             {"crop_code": "GOLB", "symptoms": "s" * 2001},
             phone=SARITA, ip=IP_TXT)
    assert r.status_code == 400, r.get_json()
    r = post(client, "/v1/crop-doctor/diagnose",
             {"crop_code": "GOLB", "symptoms": "पातमा कालो दाग",
              "photo_ref": "p" * 257},
             phone=SARITA, ip=IP_TXT)
    assert r.status_code == 400, r.get_json()
    r = post(client, "/v1/crop-doctor/diagnose",
             {"crop_code": "GOLB", "symptoms": "पातमा कालो दाग"},
             phone=SARITA, ip=IP_TXT)
    assert r.status_code == 200, r.get_json()
    print("  PASS — oversized fields 400, normal diagnosis 200")
    passed += 1

    print("\n=== 9. crop-doctor rate limit: 20 OK, 21st 429 ===")
    auth._attempts.pop(f"diagnose:{IP_DIAG}", None)
    for i in range(20):
        r = post(client, "/v1/crop-doctor/diagnose",
                 {"crop_code": "GOLB", "symptoms": f"लक्षण {i}"},
                 phone=SARITA, ip=IP_DIAG)
        assert r.status_code == 200, (i, r.get_json())
    r = post(client, "/v1/crop-doctor/diagnose",
             {"crop_code": "GOLB", "symptoms": "एक्काइसौं"},
             phone=SARITA, ip=IP_DIAG)
    assert r.status_code == 429, r.get_json()
    print("  PASS — 21st diagnosis from one IP rejected with 429")
    passed += 1

    print(f"\n{passed} passed, 0 failed.")


if __name__ == "__main__":
    run()
