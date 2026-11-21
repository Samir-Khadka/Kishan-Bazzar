"""Backend-logic regression tests for the 2026-11-21 daily-improvement
fix (focus: backend logic). Three real defects, all reproduced live
against the test client before fixing:

  1. POST /v1/farmers/<phone>/land-scan with a JSON list/dict
     land_type or confidence 500'd: `value not in VALID_LAND_TYPES`
     runs a set-membership test, and unhashable types raise
     TypeError. Wrong type is a 400, never a 500 — the same class
     the codebase has been fixing endpoint by endpoint.
  2. POST /v1/crop-doctor/diagnose with a JSON list/number
     farmer_phone 500'd: `(body.get("farmer_phone") or "").strip()`
     calls .strip() on a non-string. Same class, same fix.
  3. POST /v1/offers/<id>/decide could not re-counter a countered
     offer: the status guard allowed only accepted/rejected from
     'countered', so a staff member who mistyped a counter rate had
     no way to correct it — the only exits were accepting at the
     wrong rate (a money error) or rejecting and making the buyer
     re-bid. Re-countering overwrites the rate; converted/rejected
     stay terminal.

Run: python3 tests/test_backend_logic_nov21.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seed
import db as db_module
import auth
import app as app_module

STAFF = "9801100000"
SARITA = "9841000001"
RAM = "9841000002"
KRISHNA = "9841000004"
BUYER = "9899000012"

passed = 0


def check(name, cond, extra=""):
    global passed
    assert cond, f"{name} {extra}"
    passed += 1
    print(f"  PASS — {name}")


def tok(phone):
    return {"Authorization": f"Bearer {auth.issue_token(phone)}"}


def offer_row(offer_id):
    conn = db_module.get_db()
    try:
        return dict(conn.execute(
            "SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone())
    finally:
        conn.close()


def order_for_lot(lot_id):
    conn = db_module.get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM orders WHERE lot_id=?", (lot_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def form_and_confirm_lot(client):
    """Listings totalling 134kg against the 130kg threshold, then
    staff-confirm at Rs 50/kg — returns a ready lot id."""
    lot_id = None
    for phone, kg in ((SARITA, 38), (RAM, 52), (KRISHNA, 44)):
        r = client.post("/v1/listings", json={
            "farmer_phone": phone, "coop_id": "kavre-veg",
            "crop_code": "KAULI", "grade": "A", "quantity_kg": kg,
        }, headers=tok(phone))
        assert r.status_code == 201, r.get_json()
        if "lot_formed" in r.get_json():
            lot_id = r.get_json()["lot_formed"]["lot_id"]
    assert lot_id, "no lot formed from the listings"
    r = client.post(f"/v1/lots/{lot_id}/confirm",
                    json={"rate_per_kg": 50}, headers=tok(STAFF))
    assert r.status_code == 200, r.get_json()
    return lot_id


def place_offer(client, lot_id, rate=40, qty=1):
    r = client.post("/v1/offers", json={
        "buyer_name": "Bidder", "buyer_phone": BUYER,
        "lot_id": lot_id, "bid_rate_per_kg": rate, "quantity_kg": qty,
    }, headers=tok(BUYER))
    assert r.status_code == 201, r.get_json()
    return r.get_json()["offer_id"]


def main():
    seed.seed()
    conn = db_module.get_db()
    conn.execute(
        "INSERT INTO farmers (phone, name, coop_id, lat, lon, role) "
        "VALUES (?,?,?,?,?,?)",
        (BUYER, "Bidder", "kavre-veg", 27.62, 85.54, "buyer"))
    conn.commit()
    conn.close()

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()

    print("=== 1. Land-scan rejects wrong-typed land_type/confidence with 400 ===")
    r = client.post(f"/v1/farmers/{SARITA}/land-scan", json={
        "land_type": ["khet"], "confidence": "good",
    }, headers=tok(SARITA))
    check("list land_type -> 400, not 500", r.status_code == 400, r.get_json())
    r = client.post(f"/v1/farmers/{SARITA}/land-scan", json={
        "land_type": "khet", "confidence": {"x": 1},
    }, headers=tok(SARITA))
    check("dict confidence -> 400, not 500", r.status_code == 400, r.get_json())
    r = client.post(f"/v1/farmers/{SARITA}/land-scan", json={
        "land_type": 5, "confidence": "good",
    }, headers=tok(SARITA))
    check("numeric land_type -> 400", r.status_code == 400, r.get_json())
    r = client.post(f"/v1/farmers/{SARITA}/land-scan", json={
        "confidence": "good",
    }, headers=tok(SARITA))
    check("missing land_type -> 400", r.status_code == 400, r.get_json())
    r = client.post(f"/v1/farmers/{SARITA}/land-scan", json={
        "land_type": "khet", "confidence": "good",
        "color_key": "dark", "texture": "clay", "drainage": "slow",
        "had_photo": True,
    }, headers=tok(SARITA))
    check("valid scan still 201", r.status_code == 201, r.get_json())
    check("valid scan stored land_type=khet",
          r.get_json()["land_type"] == "khet", r.get_json())

    print("\n=== 2. Crop-doctor rejects wrong-typed farmer_phone with 400 ===")
    r = client.post("/v1/crop-doctor/diagnose", json={
        "crop_code": "GOLB", "symptoms": "पातमा कालो दाग",
        "farmer_phone": [SARITA],
    }, headers=tok(SARITA))
    check("list farmer_phone -> 400, not 500", r.status_code == 400, r.get_json())
    r = client.post("/v1/crop-doctor/diagnose", json={
        "crop_code": "GOLB", "symptoms": "पातमा कालो दाग",
        "farmer_phone": 9841000001,
    }, headers=tok(SARITA))
    check("int farmer_phone -> 400, not 500", r.status_code == 400, r.get_json())
    r = client.post("/v1/crop-doctor/diagnose", json={
        "crop_code": "GOLB", "symptoms": "पातमा कालो दाग",
        "farmer_phone": {"phone": SARITA},
    }, headers=tok(SARITA))
    check("dict farmer_phone -> 400, not 500", r.status_code == 400, r.get_json())
    r = client.post("/v1/crop-doctor/diagnose", json={
        "crop_code": "GOLB", "symptoms": "पातमा कालो दाग",
        "farmer_phone": RAM,
    }, headers=tok(SARITA))
    check("another farmer's phone still 403", r.status_code == 403, r.get_json())
    r = client.post("/v1/crop-doctor/diagnose", json={
        "crop_code": "GOLB", "symptoms": "पातमा कालो दाग",
    }, headers=tok(SARITA))
    check("valid diagnose still 200", r.status_code == 200, r.get_json())
    check("valid diagnose returns a diagnosis",
          len(r.get_json()["diagnoses"]) >= 1, r.get_json())

    print("\n=== 3. A countered offer can be re-countered ===")
    lot_id = form_and_confirm_lot(client)
    offer_id = place_offer(client, lot_id)
    r = client.post(f"/v1/offers/{offer_id}/decide",
                    json={"decision": "countered", "counter_rate_per_kg": 45},
                    headers=tok(STAFF))
    check("first counter -> 200", r.status_code == 200, r.get_json())
    check("counter rate stored as 45",
          offer_row(offer_id)["counter_rate_per_kg"] == 45.0)
    r = client.post(f"/v1/offers/{offer_id}/decide",
                    json={"decision": "countered", "counter_rate_per_kg": 48},
                    headers=tok(STAFF))
    check("second counter -> 200 (was 409)", r.status_code == 200, r.get_json())
    row = offer_row(offer_id)
    check("offer still 'countered' after re-counter",
          row["status"] == "countered", row)
    check("counter rate overwritten to 48",
          row["counter_rate_per_kg"] == 48.0, row)
    check("no order from the counters", order_for_lot(lot_id) == [])

    print("\n=== 4. Accept after re-counter converts at the NEW counter rate ===")
    r = client.post(f"/v1/offers/{offer_id}/decide",
                    json={"decision": "accepted"}, headers=tok(STAFF))
    check("accept after re-counter -> 200", r.status_code == 200, r.get_json())
    orders = order_for_lot(lot_id)
    check("exactly one order created", len(orders) == 1, orders)
    check("order at the re-countered rate 48, not 45 or the bid 40",
          orders[0]["unit_rate"] == 48.0, orders)
    check("offer now 'converted'",
          offer_row(offer_id)["status"] == "converted")

    print("\n=== 5. Converted/rejected offers stay terminal ===")
    r = client.post(f"/v1/offers/{offer_id}/decide",
                    json={"decision": "countered", "counter_rate_per_kg": 49},
                    headers=tok(STAFF))
    check("counter on converted offer -> 409", r.status_code == 409, r.get_json())
    offer2 = place_offer(client, lot_id)
    r = client.post(f"/v1/offers/{offer2}/decide",
                    json={"decision": "rejected"}, headers=tok(STAFF))
    check("reject -> 200", r.status_code == 200, r.get_json())
    r = client.post(f"/v1/offers/{offer2}/decide",
                    json={"decision": "countered", "counter_rate_per_kg": 49},
                    headers=tok(STAFF))
    check("counter on rejected offer -> 409", r.status_code == 409, r.get_json())
    r = client.post(f"/v1/offers/{offer2}/decide",
                    json={"decision": "accepted"}, headers=tok(STAFF))
    check("accept on rejected offer -> 409", r.status_code == 409, r.get_json())

    print(f"\n{passed} passed, 0 failed.")


if __name__ == "__main__":
    main()
