"""
A+ residual tests (2026-10-23, second pass).
Run: python3 tests/test_aplus.py

Covers the two Spec residuals the A− pass named:
  K. Order settlement lifecycle — staff-recorded payment state
     (unpaid -> settled), auth, amount/method validation,
     double-settle, the buyer read model, and the assistant ledger
     splitting settled vs pending from that real state.
  L. Collection route built from REAL operational data — pickup
     stops from pooled listings behind ready lots, delivery stops
     from real orders, unlocated buyers reported honestly, staff
     gating, distances as labelled estimates.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("KISAN_BAZAR_DEMO_MODE", None)

import threading

import seed
import db
import auth
import assistant
import repositories
import app as app_module

SARITA = "9841000001"
RAM = "9841000002"
MINA = "9841000003"
KRISHNA = "9841000004"
STAFF = "9801100000"
GHOST = "9841000999"   # valid-format phone with NO farmer record

passed = 0


def check(name, cond, extra=""):
    global passed
    assert cond, f"{name} {extra}"
    passed += 1
    print(f"  PASS — {name}")


def tok(phone):
    return {"Authorization": f"Bearer {auth.issue_token(phone)}"}


def farmer_row(phone):
    conn = db.get_db()
    row = conn.execute("SELECT * FROM farmers WHERE phone=?", (phone,)).fetchone()
    conn.close()
    return dict(row)


def pooled_lot(client, lot_id="TST-910"):
    """Sarita 80 kg + Ram 50 kg pooled into one ready lot @ Rs 45 —
    the same shape as the Oct-23 ledger regression, built directly."""
    conn = db.get_db()
    conn.execute(
        "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, pooled_kg, rate_per_kg, status) "
        "VALUES (?, 'kavre-veg', 'KAULI', 'A', 130, 130, 45, 'ready')", (lot_id,))
    ids = {}
    for phone, kg in ((SARITA, 80), (RAM, 50)):
        cur = conn.execute(
            "INSERT INTO listings (farmer_phone, coop_id, crop_code, grade, quantity_kg, quantity_committed_kg, status) "
            "VALUES (?, 'kavre-veg', 'KAULI', 'A', ?, ?, 'pooled')", (phone, kg, kg))
        ids[phone] = cur.lastrowid
        conn.execute(
            "INSERT INTO lot_listings (lot_id, listing_id, kg_contributed) VALUES (?, ?, ?)",
            (lot_id, cur.lastrowid, kg))
    conn.commit()
    conn.close()


def run():
    seed.seed()
    client = app_module.app.test_client()
    auth._attempts.clear()
    auth._dev_outbox.clear()

    # ================= K. settlement lifecycle =================
    print("\n=== K. Order settlement: staff-recorded paid state ===")
    pooled_lot(client)
    r = client.post("/v1/orders",
                    json={"buyer_name": "Hotel Buyer", "buyer_phone": GHOST,
                          "lot_id": "TST-910", "quantity_kg": 100},
                    headers=tok(GHOST))
    check("seed order placed (100 kg @ 45 = Rs 4,500)", r.status_code == 201, r.get_json())
    order_id = r.get_json()["order_id"]

    r = client.get("/v1/orders/mine")
    check("orders/mine without token -> 401", r.status_code == 401, r.get_json())
    r = client.get("/v1/orders/mine", headers=tok(GHOST))
    mine = r.get_json()
    check("buyer sees own order, unpaid, no settlement fields set",
          len(mine) == 1 and mine[0]["payment_status"] == "unpaid"
          and mine[0]["settled_amount"] is None, mine)
    r = client.get("/v1/orders/mine", headers=tok(MINA))
    check("another buyer sees none of those orders", r.get_json() == [], r.get_json())

    # Ledger BEFORE settlement: everything pending, nothing settled.
    conn = db.get_db()
    dues = repositories.farmer_dues(conn, SARITA)
    conn.close()
    check("dues before settle: Sarita pending 2769.23, settled 0",
          abs(dues["pending"] - 2769.23) < 0.01 and dues["settled"] == 0, dues)

    r = client.post(f"/v1/orders/{order_id}/settle",
                    json={"amount": 4500, "method": "esewa"})
    check("settle without token -> 401", r.status_code == 401, r.get_json())
    r = client.post(f"/v1/orders/{order_id}/settle",
                    json={"amount": 4500, "method": "esewa"}, headers=tok(SARITA))
    check("settle by non-staff farmer -> 403", r.status_code == 403, r.get_json())
    r = client.post(f"/v1/orders/{order_id}/settle",
                    json={"amount": 4500, "method": "cheque"}, headers=tok(STAFF))
    check("settle with a non-recordable method -> 400", r.status_code == 400, r.get_json())
    r = client.post(f"/v1/orders/{order_id}/settle",
                    json={"amount": 4000, "method": "cash"}, headers=tok(STAFF))
    check("settle with wrong amount -> 400 naming the real total",
          r.status_code == 400 and "4,500" in r.get_json()["error"], r.get_json())
    r = client.post("/v1/orders/999999/settle",
                    json={"amount": 1, "method": "cash"}, headers=tok(STAFF))
    check("settle unknown order -> 404", r.status_code == 404, r.get_json())

    r = client.post(f"/v1/orders/{order_id}/settle",
                    json={"amount": 4500, "method": "esewa", "reference": "ESW-123"},
                    headers=tok(STAFF))
    body = r.get_json()
    check("staff settles at the full total -> 201 settled",
          r.status_code == 201 and body["payment_status"] == "settled"
          and body["method"] == "esewa" and body["reference"] == "ESW-123"
          and body["settled_by"] == STAFF and body["settled_at"], body)

    r = client.post(f"/v1/orders/{order_id}/settle",
                    json={"amount": 4500, "method": "cash"}, headers=tok(STAFF))
    check("double settle -> 409", r.status_code == 409, r.get_json())

    # Racing settles on a fresh order: exactly one record may land.
    r = client.post("/v1/orders",
                    json={"buyer_name": "Race Buyer", "buyer_phone": GHOST,
                          "lot_id": "TST-910", "quantity_kg": 30},
                    headers=tok(GHOST))
    race_order = r.get_json()["order_id"]
    results = []
    def settle_race():
        c = app_module.app.test_client()
        rr = c.post(f"/v1/orders/{race_order}/settle",
                    json={"amount": 1350, "method": "cash"}, headers=tok(STAFF))
        results.append(rr.status_code)
    threads = [threading.Thread(target=settle_race) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check("two racing settles -> exactly one 201 and one 409",
          sorted(results) == [201, 409], results)
    conn = db.get_db()
    n = conn.execute("SELECT COUNT(*) AS c FROM order_settlements WHERE order_id=?",
                     (race_order,)).fetchone()["c"]
    conn.close()
    check("exactly one settlement row exists after the race", n == 1, n)

    # Read models reflect the state.
    r = client.get("/v1/orders/mine", headers=tok(GHOST))
    by_id = {o["id"]: o for o in r.get_json()}
    check("buyer read model: settled order shows method + amount",
          by_id[order_id]["payment_status"] == "settled"
          and by_id[order_id]["settlement_method"] == "esewa"
          and by_id[order_id]["settled_amount"] == 4500, by_id[order_id])
    r = client.get("/v1/admin/overview", headers=tok(STAFF))
    ov_order = [o for o in r.get_json()["orders"] if o["id"] == order_id]
    check("admin overview carries payment_status for staff UI",
          ov_order and ov_order[0]["payment_status"] == "settled", ov_order)

    # Ledger AFTER both orders settled: Sarita fully settled...
    conn = db.get_db()
    dues = repositories.farmer_dues(conn, SARITA)
    check("dues after settles: Sarita pending 0, settled 3600.00 "
          "(4500+1350 prorated 80/130)",
          abs(dues["pending"]) < 0.01 and abs(dues["settled"] - 3600.0) < 0.01, dues)
    res = assistant.chat("aplus-led", "मेरो बाँकी हिसाब कति छ?",
                         db_conn=conn, farmer=farmer_row(SARITA))
    check("assistant reports the settled figure, not 'pending'",
          "3,600" in res["reply"] and "भइसकेको" in res["reply"], res["reply"])
    conn.close()

    # ...and a partial state splits correctly: one more unsettled order.
    pooled_lot(client, lot_id="TST-911")
    r = client.post("/v1/orders",
                    json={"buyer_name": "Partial Buyer", "buyer_phone": GHOST,
                          "lot_id": "TST-911", "quantity_kg": 100},
                    headers=tok(GHOST))
    check("partial-scenario order placed", r.status_code == 201, r.get_json())
    conn = db.get_db()
    dues = repositories.farmer_dues(conn, SARITA)
    conn.close()
    check("partial split: pending back to 2769.23 while settled stays 3600",
          abs(dues["pending"] - 2769.23) < 0.01
          and abs(dues["settled"] - 3600.0) < 0.01, dues)
    conn = db.get_db()
    res = assistant.chat("aplus-led3", "मेरो बाँकी हिसाब कति छ?",
                         db_conn=conn, farmer=farmer_row(SARITA))
    conn.close()
    check("assistant states BOTH pending and settled when both exist",
          "2,769" in res["reply"] and "3,600" in res["reply"], res["reply"])

    # ================= L. collection route from real data =================
    print("\n=== L. Collection route from real operational data ===")
    # Fresh DB so the route reflects ONLY this scenario.
    seed.seed()
    auth._attempts.clear()
    # Listings through the API. Aggregation pools in listing order up
    # to the 130 kg target: Sarita 80 + Ram 50 forms lot one; Krishna
    # 130 forms lot two on his own.
    lot_ids = []
    for phone, kg in ((SARITA, 80), (RAM, 50), (KRISHNA, 130)):
        r = client.post("/v1/listings",
                        json={"farmer_phone": phone, "coop_id": "kavre-veg",
                              "crop_code": "KAULI", "grade": "A", "quantity_kg": kg},
                        headers=tok(phone))
        check(f"listing {kg} kg accepted", r.status_code == 201, r.get_json())
        formed = r.get_json().get("lot_formed")
        if formed:
            lot_ids.append(formed["lot_id"])
    check("aggregation formed two lots from the listings", len(lot_ids) == 2, lot_ids)
    lot_id = lot_ids[0]
    for lid in lot_ids:
        r = client.post(f"/v1/lots/{lid}/confirm",
                        json={"rate_per_kg": 45}, headers=tok(STAFF))
        check(f"staff confirms {lid} -> ready", r.status_code == 200, r.get_json())

    # Mina (registered, has coordinates) orders 40 kg; GHOST (no
    # farmer record, no coordinates) orders 10 kg.
    r = client.post("/v1/orders",
                    json={"buyer_name": "Mina Shrestha", "buyer_phone": MINA,
                          "lot_id": lot_id, "quantity_kg": 40},
                    headers=tok(MINA))
    check("registered buyer order placed", r.status_code == 201, r.get_json())
    r = client.post("/v1/orders",
                    json={"buyer_name": "Ghost Buyer", "buyer_phone": GHOST,
                          "lot_id": lot_id, "quantity_kg": 10},
                    headers=tok(GHOST))
    check("unregistered buyer order placed", r.status_code == 201, r.get_json())

    r = client.get("/v1/coop/route")
    check("route without token -> 401", r.status_code == 401, r.get_json())
    r = client.get("/v1/coop/route", headers=tok(SARITA))
    check("route for non-staff -> 403", r.status_code == 403, r.get_json())
    r = client.get("/v1/coop/route", headers=tok(STAFF))
    route = r.get_json()
    check("staff gets the route, labelled as live data with estimate basis",
          r.status_code == 200 and route["source"] == "live data"
          and "haversine" in route["distance_basis"], route.get("source"))

    stops = route["stops"]
    pickups = [s for s in stops if s["kind"] == "pickup"]
    deliveries = [s for s in stops if s["kind"] == "delivery"]
    by_phone = {s["phone"]: s for s in pickups}
    check("pickup stops are the pooling farmers with their real kg",
          by_phone.get(SARITA, {}).get("kg") == 80
          and by_phone.get(RAM, {}).get("kg") == 50
          and by_phone.get(KRISHNA, {}).get("kg") == 130, pickups)
    check("pickup stop names the formed lot",
          lot_id in by_phone[SARITA]["lots"], by_phone[SARITA])
    check("nearest-first: Sarita (at the co-op) < Krishna (~1 km) < Ram (~7 km)",
          by_phone[SARITA]["seq"] < by_phone[KRISHNA]["seq"] < by_phone[RAM]["seq"],
          stops)
    mina = [s for s in deliveries if s["phone"] == MINA]
    check("delivery stop is Mina's real order (40 kg, 1 order)",
          len(mina) == 1 and mina[0]["kg"] == 40 and mina[0]["orders"] == 1,
          deliveries)
    ghost = route["unlocated"]["deliveries"]
    check("coordinate-less buyer is unlocated, never mapped with fake coords",
          len(ghost) == 1 and ghost[0]["phone"] == GHOST
          and ghost[0]["kg"] == 10 and "lat" not in ghost[0], ghost)
    check("totals derive from the seeded operations",
          route["totals"]["pickup_kg"] == 260
          and route["totals"]["delivery_kg"] == 50
          and route["totals"]["est_distance_km"] > 0, route["totals"])
    check("sequence numbers are 1..N in order",
          [s["seq"] for s in stops] == list(range(1, len(stops) + 1)), stops)

    print(f"\nAll {passed} A+ residual checks passed.")


if __name__ == "__main__":
    run()
