"""
Regression tests for the 2026-10-23 A+ fix pass.
Run: python3 tests/test_regressions_oct23.py

Each block maps to a finding in ANALYSIS-2026-10-23.md:
  A. /v1/orders requires the buyer's own token (was: fully public)
  B. Assistant ledger answers from REAL orders, prorated per farmer
     (was: broken query + swallowed error -> always "no payments"),
     and degrades honestly when the lookup fails
  C. Assistant price change computed from recorded history
     (was: fabricated "steady" for every crop)
  D. Offer decisions: auth, counter->accept conversion rate, and the
     double-decide race (two racing accepts -> exactly one order)
  E. Admin login fails CLOSED when credentials aren't configured
  F. Crop-doctor history/diagnose bound to the authenticated farmer
  G. Rentals: overlap refused, owner confirm / cancel lifecycle
  H. Idempotency keys end-to-end (orders + listings)
  I. OTP lifecycle: previous code invalidated, expiry, verify limit
  J. Lot formation under concurrency (one lot, no double-commit)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("KISAN_BAZAR_DEMO_MODE", None)

import threading

import seed
import db
import auth
import assistant
import app as app_module
import routes_auth

SARITA = "9841000001"
RAM = "9841000002"
MINA = "9841000003"
KRISHNA = "9841000004"
STAFF = "9801100000"

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


def run():
    seed.seed()
    client = app_module.app.test_client()
    auth._attempts.clear()
    auth._dev_outbox.clear()

    # ---------- A. orders auth ----------
    print("\n=== A. /v1/orders requires the buyer's own token ===")
    payload = {"buyer_name": "Anon", "buyer_phone": SARITA,
               "lot_id": "KVR-118", "quantity_kg": 10}
    r = client.post("/v1/orders", json=payload)
    check("no token -> 401", r.status_code == 401, r.get_json())
    r = client.post("/v1/orders", json=payload,
                    headers={"Authorization": "Bearer not-a-token"})
    check("garbage token -> 401", r.status_code == 401, r.get_json())
    r = client.post("/v1/orders", json=payload, headers=tok(RAM))
    check("another buyer's token -> 403", r.status_code == 403, r.get_json())
    r = client.post("/v1/orders", json=payload, headers=tok(SARITA))
    check("buyer's own token -> 201", r.status_code == 201, r.get_json())

    # ---------- B. ledger answer ----------
    print("\n=== B. Assistant ledger = prorated share of real orders ===")
    conn = db.get_db()
    conn.execute(
        "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, pooled_kg, rate_per_kg, status) "
        "VALUES ('TST-900', 'kavre-veg', 'KAULI', 'A', 130, 130, 45, 'ready')")
    cur = conn.execute(
        "INSERT INTO listings (farmer_phone, coop_id, crop_code, grade, quantity_kg, quantity_committed_kg, status) "
        "VALUES (?, 'kavre-veg', 'KAULI', 'A', 80, 80, 'pooled')", (SARITA,))
    sarita_listing = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO listings (farmer_phone, coop_id, crop_code, grade, quantity_kg, quantity_committed_kg, status) "
        "VALUES (?, 'kavre-veg', 'KAULI', 'A', 50, 50, 'pooled')", (RAM,))
    ram_listing = cur.lastrowid
    conn.execute("INSERT INTO lot_listings (lot_id, listing_id, kg_contributed) VALUES ('TST-900', ?, 80)",
                 (sarita_listing,))
    conn.execute("INSERT INTO lot_listings (lot_id, listing_id, kg_contributed) VALUES ('TST-900', ?, 50)",
                 (ram_listing,))
    conn.commit()
    conn.close()
    # A real 100 kg order against the pooled lot, through the API.
    r = client.post("/v1/orders",
                    json={"buyer_name": "Ledger Buyer", "buyer_phone": "9841000999",
                          "lot_id": "TST-900", "quantity_kg": 100},
                    headers=tok("9841000999"))
    check("seed order placed", r.status_code == 201, r.get_json())

    conn = db.get_db()
    try:
        # 100 kg x Rs45 = Rs 4,500 of sales; Sarita pooled 80/130, Ram 50/130.
        res = assistant.chat("led-s", "मेरो बाँकी हिसाब कति छ?",
                             db_conn=conn, farmer=farmer_row(SARITA))
        check("Sarita's due is her prorated Rs 2,769",
              "2,769" in res["reply"], res["reply"])
        res = assistant.chat("led-r", "मेरो बाँकी हिसाब कति छ?",
                             db_conn=conn, farmer=farmer_row(RAM))
        check("Ram's due is his prorated Rs 1,731",
              "1,731" in res["reply"], res["reply"])
        res = assistant.chat("led-k", "मेरो बाँकी हिसाब कति छ?",
                             db_conn=conn, farmer=farmer_row(KRISHNA))
        check("farmer with no pooled sales is told exactly that",
              "छैन" in res["reply"] and "2,769" not in res["reply"], res["reply"])
    finally:
        conn.close()

    class BrokenConn:
        def execute(self, *a, **k):
            raise RuntimeError("db on fire")

    res = assistant.chat("led-x", "what is my pending payment?",
                         db_conn=BrokenConn(), farmer=farmer_row(SARITA))
    check("lookup failure degrades honestly (no fake 'nothing due')",
          "couldn't fetch your ledger" in res["reply"], res["reply"])

    # ---------- C. price change from real history ----------
    print("\n=== C. Assistant price change from real history ===")
    conn = db.get_db()
    try:
        res = assistant.chat("pr-1", "टमाटरको भाउ कति छ?",
                             db_conn=conn, farmer=None)
        check("single recorded rate: price stated, NO invented trend",
              "48" in res["reply"] and "स्थिर" not in res["reply"]
              and "➡️" not in res["reply"], res["reply"])
        conn.execute(
            "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, pooled_kg, rate_per_kg, status, created_at) "
            "VALUES ('TST-901', 'kavre-veg', 'GOLB', 'A', 100, 100, 40, 'ready', '2026-01-01 00:00:00')")
        conn.commit()
        rate, chg = assistant._get_price(conn, "tomato")
        check("_get_price computes +20% from the two recorded rates",
              rate == 48 and chg is not None and abs(chg - 20.0) < 0.01,
              f"{rate}, {chg}")
        res = assistant.chat("pr-2", "टमाटरको भाउ कति छ?",
                             db_conn=conn, farmer=None)
        check("chat shows the real upward trend", "📈" in res["reply"],
              res["reply"])
    finally:
        conn.close()

    # ---------- D. offer decisions ----------
    print("\n=== D. Offer decide: branches + double-convert race ===")
    buyer = "9841000777"
    r = client.post("/v1/offers",
                    json={"buyer_name": "Bidder", "buyer_phone": buyer,
                          "lot_id": "KVR-118", "quantity_kg": 5,
                          "bid_rate_per_kg": 50},
                    headers=tok(buyer))
    check("offer created", r.status_code == 201, r.get_json())
    offer1 = r.get_json()["offer_id"]
    r = client.post(f"/v1/offers/{offer1}/decide",
                    json={"decision": "accepted"}, headers=tok(RAM))
    check("non-staff decide -> 403", r.status_code == 403, r.get_json())
    r = client.post(f"/v1/offers/{offer1}/decide",
                    json={"decision": "countered", "counter_rate_per_kg": 52},
                    headers=tok(STAFF))
    check("staff counter -> 200", r.status_code == 200, r.get_json())
    conn = db.get_db()
    before = conn.execute(
        "SELECT COALESCE(SUM(quantity_kg),0) AS s FROM orders WHERE lot_id='KVR-118'"
    ).fetchone()["s"]
    conn.close()
    r = client.post(f"/v1/offers/{offer1}/decide",
                    json={"decision": "accepted"}, headers=tok(STAFF))
    check("accepting a countered offer -> 200", r.status_code == 200, r.get_json())
    conn = db.get_db()
    row = conn.execute(
        "SELECT quantity_kg, unit_rate FROM orders WHERE lot_id='KVR-118' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    after = conn.execute(
        "SELECT COALESCE(SUM(quantity_kg),0) AS s FROM orders WHERE lot_id='KVR-118'"
    ).fetchone()["s"]
    conn.close()
    check("conversion books the COUNTER rate (52), not the bid",
          row["unit_rate"] == 52 and after - before == 5, dict(row))
    r = client.post(f"/v1/offers/{offer1}/decide",
                    json={"decision": "accepted"}, headers=tok(STAFF))
    check("deciding a converted offer -> 409", r.status_code == 409, r.get_json())

    r = client.post("/v1/offers",
                    json={"buyer_name": "Racer", "buyer_phone": buyer,
                          "lot_id": "KVR-118", "quantity_kg": 10,
                          "bid_rate_per_kg": 50},
                    headers=tok(buyer))
    race_offer = r.get_json()["offer_id"]
    conn = db.get_db()
    orders_before = conn.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE buyer_phone=?", (buyer,)).fetchone()["n"]
    conn.close()
    statuses = []

    def decide():
        c = app_module.app.test_client()
        rr = c.post(f"/v1/offers/{race_offer}/decide",
                    json={"decision": "accepted"}, headers=tok(STAFF))
        statuses.append(rr.status_code)

    threads = [threading.Thread(target=decide) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    conn = db.get_db()
    orders_after = conn.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE buyer_phone=?", (buyer,)).fetchone()["n"]
    conn.close()
    check("racing accepts: exactly one 200, one 409",
          sorted(statuses) == [200, 409], statuses)
    check("racing accepts: exactly ONE order created",
          orders_after - orders_before == 1,
          f"{orders_before} -> {orders_after}")

    # ---------- E. admin fail-closed ----------
    print("\n=== E. Admin login fails closed when unconfigured ===")
    saved = {k: os.environ.get(k) for k in ("KISAN_ADMIN_USER", "KISAN_ADMIN_PASS")}
    try:
        os.environ.pop("KISAN_ADMIN_USER", None)
        os.environ.pop("KISAN_ADMIN_PASS", None)
        auth._attempts.clear()
        check("test env is not demo mode", routes_auth.DEMO_MODE is False)
        r = client.post("/v1/auth/admin", json={"username": "admin", "password": ""})
        check("empty password with unconfigured env -> 401 (was: logged in!)",
              r.status_code == 401, r.get_json())
        r = client.post("/v1/auth/admin", json={"username": "", "password": ""})
        check("empty/empty -> 401", r.status_code == 401, r.get_json())
        os.environ["KISAN_ADMIN_USER"] = "admin"
        os.environ["KISAN_ADMIN_PASS"] = "s3cret-test"
        r = client.post("/v1/auth/admin", json={"username": "admin", "password": "wrong"})
        check("configured + wrong password -> 401", r.status_code == 401)
        r = client.post("/v1/auth/admin", json={"username": "admin", "password": "s3cret-test"})
        check("configured + right password -> 200", r.status_code == 200, r.get_json())
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---------- F. crop doctor auth ----------
    print("\n=== F. Crop-doctor history/diagnose bound to the owner ===")
    r = client.get(f"/v1/crop-doctor/history?farmer_phone={SARITA}")
    check("history without token -> 401", r.status_code == 401, r.get_json())
    r = client.get(f"/v1/crop-doctor/history?farmer_phone={SARITA}", headers=tok(RAM))
    check("history with someone else's token -> 403", r.status_code == 403, r.get_json())
    r = client.get(f"/v1/crop-doctor/history?farmer_phone={SARITA}", headers=tok(SARITA))
    check("history with own token -> 200", r.status_code == 200, r.get_json())
    r = client.get(f"/v1/crop-doctor/history?farmer_phone={SARITA}", headers=tok(STAFF))
    check("history with staff token -> 200", r.status_code == 200, r.get_json())
    r = client.post("/v1/crop-doctor/diagnose",
                    json={"crop_code": "GOLB", "symptoms": "पातमा कालो दाग"})
    check("diagnose without token -> 401", r.status_code == 401, r.get_json())
    r = client.post("/v1/crop-doctor/diagnose",
                    json={"crop_code": "GOLB", "symptoms": "पातमा कालो दाग",
                          "farmer_phone": RAM},
                    headers=tok(SARITA))
    check("diagnose filed under another farmer -> 403",
          r.status_code == 403, r.get_json())
    r = client.post("/v1/crop-doctor/diagnose",
                    json={"crop_code": "GOLB", "symptoms": "पातमा कालो दाग"},
                    headers=tok(SARITA))
    check("diagnose with own token -> 200", r.status_code == 200, r.get_json())

    # ---------- G. rentals lifecycle ----------
    print("\n=== G. Rentals: overlap, confirm, cancel ===")
    conn = db.get_db()
    rental = conn.execute(
        "SELECT * FROM rentals WHERE machine_type='power_tiller' AND available=1").fetchone()
    conn.close()
    rid, owner = rental["id"], rental["owner_phone"]
    r = client.post(f"/v1/rentals/{rid}/book",
                    json={"days": 2, "book_date": "2026-11-10"}, headers=tok(SARITA))
    check("first booking -> 201", r.status_code == 201, r.get_json())
    booking1 = r.get_json()["booking_id"]
    r = client.post(f"/v1/rentals/{rid}/book",
                    json={"days": 1, "book_date": "2026-11-11"}, headers=tok(MINA))
    check("overlapping booking -> 409", r.status_code == 409, r.get_json())
    r = client.post(f"/v1/rentals/{rid}/book",
                    json={"days": 1, "book_date": "2026-11-12"}, headers=tok(MINA))
    check("adjacent booking (starts the day the other ends) -> 201",
          r.status_code == 201, r.get_json())
    booking2 = r.get_json()["booking_id"]
    r = client.post(f"/v1/rentals/{rid}/book",
                    json={"days": 1, "book_date": "10/11/2026"}, headers=tok(MINA))
    check("malformed book_date -> 400", r.status_code == 400, r.get_json())
    r = client.post(f"/v1/rentals/bookings/{booking1}/confirm", headers=tok(SARITA))
    check("booker cannot confirm own booking -> 403", r.status_code == 403, r.get_json())
    r = client.post(f"/v1/rentals/bookings/{booking1}/confirm", headers=tok(owner))
    check("owner confirms -> 200", r.status_code == 200, r.get_json())
    r = client.post(f"/v1/rentals/bookings/{booking1}/confirm", headers=tok(owner))
    check("double confirm -> 409", r.status_code == 409, r.get_json())
    r = client.post(f"/v1/rentals/bookings/{booking2}/cancel", headers=tok(KRISHNA))
    check("third party cannot cancel -> 403", r.status_code == 403, r.get_json())
    r = client.post(f"/v1/rentals/bookings/{booking2}/cancel", headers=tok(MINA))
    check("booker cancels -> 200", r.status_code == 200, r.get_json())
    r = client.post(f"/v1/rentals/{rid}/book",
                    json={"days": 1, "book_date": "2026-11-12"}, headers=tok(KRISHNA))
    check("cancelled dates are bookable again -> 201",
          r.status_code == 201, r.get_json())

    # ---------- H. idempotency ----------
    print("\n=== H. Idempotency keys end-to-end ===")
    idem = {"Idempotency-Key": "reg-oct23-order-1"}
    body = {"buyer_name": "Idem Buyer", "buyer_phone": SARITA,
            "lot_id": "KVR-118", "quantity_kg": 3}
    r1 = client.post("/v1/orders", json=body, headers={**tok(SARITA), **idem})
    check("first order with key -> 201", r1.status_code == 201, r1.get_json())
    conn = db.get_db()
    n1 = conn.execute("SELECT COUNT(*) AS n FROM orders WHERE buyer_name='Idem Buyer'").fetchone()["n"]
    conn.close()
    r2 = client.post("/v1/orders", json=body, headers={**tok(SARITA), **idem})
    conn = db.get_db()
    n2 = conn.execute("SELECT COUNT(*) AS n FROM orders WHERE buyer_name='Idem Buyer'").fetchone()["n"]
    conn.close()
    check("replay returns the SAME order, no duplicate row",
          r2.get_json().get("order_id") == r1.get_json().get("order_id") and n1 == n2 == 1,
          f"{r1.get_json()} vs {r2.get_json()}, rows {n1}->{n2}")
    idem2 = {"Idempotency-Key": "reg-oct23-listing-1"}
    lbody = {"farmer_phone": SARITA, "coop_id": "kavre-veg",
             "crop_code": "GOLB", "grade": "A", "quantity_kg": 10}
    r1 = client.post("/v1/listings", json=lbody, headers={**tok(SARITA), **idem2})
    r2 = client.post("/v1/listings", json=lbody, headers={**tok(SARITA), **idem2})
    check("listing replay returns the same listing id",
          r1.status_code in (200, 201)
          and r2.get_json().get("listing_id") == r1.get_json().get("listing_id"),
          f"{r1.get_json()} vs {r2.get_json()}")

    # ---------- I. OTP lifecycle ----------
    print("\n=== I. OTP: previous code invalidated, expiry, verify limit ===")
    phone = "9841000555"
    auth._attempts.clear()
    client.post("/v1/auth/request-code", json={"phone": phone})
    code1 = auth._dev_outbox[-1]["code"]
    client.post("/v1/auth/request-code", json={"phone": phone})
    code2 = auth._dev_outbox[-1]["code"]
    r = client.post("/v1/auth/verify", json={"phone": phone, "code": code1})
    check("superseded code no longer logs in -> 401",
          r.status_code == 401, r.get_json())
    r = client.post("/v1/auth/verify", json={"phone": phone, "code": code2})
    check("latest code still works -> 200", r.status_code == 200, r.get_json())

    phone = "9841000566"
    client.post("/v1/auth/request-code", json={"phone": phone})
    code = auth._dev_outbox[-1]["code"]
    conn = db.get_db()
    conn.execute("UPDATE auth_codes SET expires_at=datetime('now','-1 minute') WHERE phone=?",
                 (phone,))
    conn.commit()
    conn.close()
    r = client.post("/v1/auth/verify", json={"phone": phone, "code": code})
    check("expired code -> 401", r.status_code == 401, r.get_json())

    phone = "9841000577"
    auth._attempts.clear()
    client.post("/v1/auth/request-code", json={"phone": phone})
    codes = [client.post("/v1/auth/verify", json={"phone": phone, "code": "000000"}).status_code
             for _ in range(5)]
    r = client.post("/v1/auth/verify", json={"phone": phone, "code": "000000"})
    check("5 wrong verifies -> 401s, 6th -> 429",
          codes == [401] * 5 and r.status_code == 429, f"{codes}, {r.status_code}")

    # ---------- J. concurrent lot formation ----------
    print("\n=== J. Lot formation under concurrency ===")
    conn = db.get_db()
    conn.execute(
        "INSERT INTO cooperatives (id, name, lat, lon, collection_radius_km) "
        "VALUES ('test-coop', 'Test Co-op', 27.6200, 85.5400, 15)")
    for ph, nm in (("9841000601", "Pool A"), ("9841000602", "Pool B")):
        conn.execute(
            "INSERT INTO farmers (phone, name, coop_id, lat, lon) VALUES (?,?, 'test-coop', 27.6205, 85.5405)",
            (ph, nm))
    conn.commit()
    conn.close()
    results = []

    def list100(phone):
        c = app_module.app.test_client()
        rr = c.post("/v1/listings",
                    json={"farmer_phone": phone, "coop_id": "test-coop",
                          "crop_code": "KAULI",
                          "grade": "A", "quantity_kg": 100},
                    headers=tok(phone))
        results.append(rr.status_code)

    threads = [threading.Thread(target=list100, args=(p,))
               for p in ("9841000601", "9841000602")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    conn = db.get_db()
    lots = conn.execute(
        "SELECT * FROM lots WHERE coop_id='test-coop'").fetchall()
    pooled = conn.execute(
        "SELECT COALESCE(SUM(kg_contributed),0) AS s FROM lot_listings "
        "WHERE lot_id IN (SELECT id FROM lots WHERE coop_id='test-coop')").fetchone()["s"]
    conn.close()
    check("both concurrent listings accepted", sorted(results) == [201, 201], results)
    check("exactly ONE lot formed from the raced pool", len(lots) == 1,
          [dict(x) for x in lots])
    check("the lot holds exactly the 130 kg target (no double-commit)",
          pooled == 130, pooled)

    print(f"\nAll {passed} Oct-23 regression checks passed.")


if __name__ == "__main__":
    run()
