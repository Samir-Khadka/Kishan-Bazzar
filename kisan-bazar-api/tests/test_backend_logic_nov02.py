"""Backend-logic regression tests for the 2026-11-02 daily-improvement
fix (focus: backend logic). One real inconsistency on the offer
acceptance path:

  POST /v1/orders refused forming lots with 409 "not orderable"
  (availability.place_order_locked gates on status='ready'), but
  POST /v1/offers/<id>/decide {"decision":"accepted"} called
  availability.insert_order directly with NO status check — so a
  staff accept converted a bid into a real order on a FORMING lot
  (rate_per_kg still NULL). That order then sat outside collection
  routing (ready lots only) while still counting in the farmer's
  dues: produce sold that the co-op never collects.

  The fix: decide_offer's accepted branch now requires status='ready'
  with the same 409 message the direct-order path returns. Bids may
  still be placed and countered on forming lots — only the conversion
  to an order waits for the confirmed lot.

Run: python3 tests/test_backend_logic_nov02.py
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


def order_count(lot_id):
    conn = db_module.get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE lot_id=?", (lot_id,)
        ).fetchone()["c"]
    finally:
        conn.close()


def lot_status(lot_id):
    conn = db_module.get_db()
    try:
        return conn.execute(
            "SELECT status FROM lots WHERE id=?", (lot_id,)
        ).fetchone()["status"]
    finally:
        conn.close()


def form_lot(client):
    """Listings totalling 134kg against the 130kg threshold — returns a
    forming lot id, mirroring test_backend_logic_oct26."""
    for phone, kg in ((SARITA, 38), (RAM, 52), (KRISHNA, 44)):
        r = client.post("/v1/listings", json={
            "farmer_phone": phone, "coop_id": "kavre-veg",
            "crop_code": "KAULI", "grade": "A", "quantity_kg": kg,
        }, headers=tok(phone))
        assert r.status_code == 201, r.get_json()
        if "lot_formed" in r.get_json():
            return r.get_json()["lot_formed"]["lot_id"]
    raise AssertionError("no lot formed from the listings")


def place_offer(client, lot_id):
    r = client.post("/v1/offers", json={
        "buyer_name": "Bidder", "buyer_phone": BUYER,
        "lot_id": lot_id, "bid_rate_per_kg": 40, "quantity_kg": 1,
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

    print("=== 1. Offer accepted on a forming lot is refused ===")
    lot_id = form_lot(client)
    check("lot formed in 'forming' status", lot_status(lot_id) == "forming")
    # Bids may still be placed on forming lots — the gate is on
    # acceptance (conversion to an order), not on bidding.
    offer_id = place_offer(client, lot_id)
    check("offer can be placed on a forming lot", True)
    r = client.post(f"/v1/offers/{offer_id}/decide",
                    json={"decision": "accepted"}, headers=tok(STAFF))
    check("accept on forming lot -> 409", r.status_code == 409,
          f"got {r.status_code}: {r.get_json()}")
    check("409 message matches the direct-order gate",
          r.get_json().get("error") == "lot is 'forming', not orderable")
    check("no order row was created", order_count(lot_id) == 0)
    check("offer stays pending after the refused accept",
          db_module.get_db().execute(
              "SELECT status FROM offers WHERE id=?", (offer_id,)
          ).fetchone()["status"] == "pending")

    print("=== 2. Counter on a forming lot still works ===")
    # Countering records a rate, it doesn't convert to an order —
    # staff may negotiate while the lot is still forming.
    offer2 = place_offer(client, lot_id)
    r = client.post(f"/v1/offers/{offer2}/decide",
                    json={"decision": "countered", "counter_rate_per_kg": 45},
                    headers=tok(STAFF))
    check("counter on forming lot -> 200", r.status_code == 200, r.get_json())
    check("no order row from the counter", order_count(lot_id) == 0)

    print("=== 3. Accept after the lot is confirmed converts fine ===")
    r = client.post(f"/v1/lots/{lot_id}/confirm",
                    json={"rate_per_kg": 42}, headers=tok(STAFF))
    check("lot confirmed to ready", r.status_code == 200, r.get_json())
    check("lot status is ready", lot_status(lot_id) == "ready")
    r = client.post(f"/v1/offers/{offer_id}/decide",
                    json={"decision": "accepted"}, headers=tok(STAFF))
    check("accept on ready lot -> 200", r.status_code == 200, r.get_json())
    check("exactly one order created", order_count(lot_id) == 1)
    conn = db_module.get_db()
    order = conn.execute(
        "SELECT unit_rate FROM orders WHERE lot_id=?", (lot_id,)
    ).fetchone()
    conn.close()
    check("converted order uses the bid rate", order["unit_rate"] == 40)

    print("=== 4. Direct order on a forming lot still 409 ===")
    lot2 = form_lot(client)
    r = client.post("/v1/orders", json={
        "buyer_name": "Bidder", "buyer_phone": BUYER,
        "lot_id": lot2, "quantity_kg": 1}, headers=tok(BUYER))
    check("direct order on forming lot -> 409", r.status_code == 409,
          r.get_json())
    check("same gate message as offer accept",
          r.get_json().get("error") == "lot is 'forming', not orderable")

    print(f"\nAll {passed} backend-logic checks passed.")


if __name__ == "__main__":
    main()
