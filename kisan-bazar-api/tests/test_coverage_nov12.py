"""
Test-coverage pass, 2026-11-12.

Coverage was 96% overall, but the missing lines clustered on real
branches no suite had ever exercised: a whole endpoint (/v1/stats),
a whole decision branch (offer "rejected"), error paths
(create_listing 400s/404s, make_offer 404/409, get_lot and confirm_lot
404s), the order/offer rate-limit 429s, the assistant follow-up /
scan / agronomy paths, the session sweep and eviction bounds, the
idempotency wait-timeout 409, and app-level plumbing (generic error
handler, CORS echo, the coops==0 reseed, the legacy role-column
migration).

Every check pins behaviour verified against the live code first; a
failure means the behaviour changed, not that the test is
aspirational. Deliberately NOT tested (reviewed, unreachable):
- web._idem_claim's IntegrityError branch: under BEGIN IMMEDIATE the
  loser blocks on the write lock and always sees the row in its
  lookup, so the INSERT can never collide.
- assistant._agronomy's name-match fallback: every CROPS_INFO entry
  is already hit by the direct dict lookup, so the loop can never
  match a crop the lookup missed.
- auth._secret_bytes' random-secret fallback and db's WAL retry loop:
  defensive paths that need a dead database to trigger.
- decide_offer's "lot not found" 404: an offer's lot cannot be
  deleted while the offer exists (FK RESTRICT), so the row can never
  go missing underneath a live offer.
- serve_app's missing-HTML fallback: kisan-bazar.html ships in the
  repo, so the branch cannot fire in this checkout.

Run: python3 tests/test_coverage_nov12.py
"""
import sys
import os
import json
import hmac
import hashlib
import sqlite3
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify

import seed
import db
import auth
import app as app_module
import web
import assistant
import repositories
import routing
import messaging

SARITA = "9841000001"
STAFF = "9801100000"
BUYER = "9841000777"
GHOST = "9841999998"

passed = 0


def check(name, cond, extra=""):
    global passed
    assert cond, f"{name} {extra}"
    passed += 1
    print(f"  PASS — {name}")


def tok(phone):
    return {"Authorization": f"Bearer {auth.issue_token(phone)}"}


def run():
    # ---------- 0. Legacy role-column migration (own temp DB) ----------
    print("\n=== 0. db.init_db migrates a pre-role farmers table ===")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    real_path = db.DB_PATH
    try:
        db.DB_PATH = tmp.name
        legacy = sqlite3.connect(tmp.name)
        # Farmers table as it shipped before the `role` column existed.
        legacy.execute(
            "CREATE TABLE farmers (phone TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "coop_id TEXT REFERENCES cooperatives(id), lat REAL NOT NULL, "
            "lon REAL NOT NULL, payout_method TEXT NOT NULL DEFAULT 'cash', "
            "land_type TEXT, is_staff INTEGER NOT NULL DEFAULT 0)")
        legacy.execute(
            "INSERT INTO farmers (phone, name, lat, lon, is_staff) "
            "VALUES ('9841000001', 'Sarita', 27.6, 85.5, 1)")
        legacy.commit()
        legacy.close()
        db.init_db()
        conn = db.get_db()
        cols = [r["name"] for r in conn.execute(
            "PRAGMA table_info(farmers)").fetchall()]
        check("migration adds the missing role column", "role" in cols,
              str(cols))
        row = conn.execute(
            "SELECT role FROM farmers WHERE phone='9841000001'").fetchone()
        check("backfill keeps staff role for is_staff=1",
              row["role"] == "staff", dict(row))
        conn.close()
    finally:
        db.DB_PATH = real_path
        os.unlink(tmp.name)

    seed.seed()
    client = app_module.app.test_client()
    auth._attempts.clear()

    # ---------- 1. /v1/stats + the coops==0 reseed ----------
    print("\n=== 1. /v1/stats + _ensure_db_ready reseeds an empty DB ===")
    r = client.get("/v1/stats")
    j = r.get_json()
    conn = db.get_db()
    expect = {
        "farmers": conn.execute(
            "SELECT COUNT(*) AS c FROM farmers WHERE is_staff=0"
        ).fetchone()["c"],
        "coops": conn.execute(
            "SELECT COUNT(*) AS c FROM cooperatives").fetchone()["c"],
        "active_lots": conn.execute(
            "SELECT COUNT(*) AS c FROM lots WHERE status='ready'"
        ).fetchone()["c"],
    }
    conn.close()
    check("stats counts match the DB (5 farmers, 1 co-op, 5 ready lots)",
          j == expect, f"got {j} expected {expect}")
    check("stats is cacheable for 60s",
          r.headers.get("Cache-Control") == "public, max-age=60",
          r.headers.get("Cache-Control"))

    conn = db.get_db()
    conn.execute("DELETE FROM rentals")
    conn.execute("DELETE FROM lots")
    conn.execute("DELETE FROM farmers")
    conn.execute("DELETE FROM cooperatives")
    conn.commit()
    conn.close()
    app_module._ensure_db_ready()
    conn = db.get_db()
    coops = conn.execute(
        "SELECT COUNT(*) AS c FROM cooperatives").fetchone()["c"]
    farmers = conn.execute(
        "SELECT COUNT(*) AS c FROM farmers").fetchone()["c"]
    lots = conn.execute("SELECT COUNT(*) AS c FROM lots").fetchone()["c"]
    conn.close()
    check("empty DB reseeds on startup (1 co-op, 6 farmers, 5 lots)",
          (coops, farmers, lots) == (1, 6, 5),
          f"coops={coops} farmers={farmers} lots={lots}")

    # ---------- 2. seed.ensure_demo_lots backfill ----------
    print("\n=== 2. seed.ensure_demo_lots backfills + is idempotent ===")
    conn = db.get_db()
    conn.execute("DELETE FROM lots")
    conn.commit()
    seed.ensure_demo_lots(conn)
    n = conn.execute("SELECT COUNT(*) AS c FROM lots").fetchone()["c"]
    check("empty lots table backfilled to 5 demo lots", n == 5, f"n={n}")
    seed.ensure_demo_lots(conn)
    n2 = conn.execute("SELECT COUNT(*) AS c FROM lots").fetchone()["c"]
    check("second call adds nothing (idempotent)", n2 == 5, f"n={n2}")
    # ... and it bails out cleanly when the co-op row itself is gone.
    conn.execute("DELETE FROM lots")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM cooperatives WHERE id='kavre-veg'")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()
    seed.ensure_demo_lots(conn)
    n3 = conn.execute("SELECT COUNT(*) AS c FROM lots").fetchone()["c"]
    check("no co-op row -> nothing backfilled", n3 == 0, f"n={n3}")
    conn.execute(
        "INSERT INTO cooperatives (id, name, lat, lon, "
        "collection_radius_km, commission_pct) VALUES "
        "('kavre-veg','Kavre Vegetable Co-op',27.6215,85.5442,18,8.0)")
    conn.commit()
    seed.ensure_demo_lots(conn)
    n4 = conn.execute("SELECT COUNT(*) AS c FROM lots").fetchone()["c"]
    check("co-op restored -> backfill works again", n4 == 5, f"n={n4}")
    conn.close()

    # ---------- 3. create_listing validation branches ----------
    print("\n=== 3. create_listing: missing/grade/farmer/crop errors ===")
    base = {"farmer_phone": SARITA, "coop_id": "kavre-veg",
            "crop_code": "GOLB", "grade": "A", "quantity_kg": 50}
    r = client.post("/v1/listings",
                    json={k: v for k, v in base.items() if k != "grade"},
                    headers={**tok(SARITA),
                             "Idempotency-Key": "nov12-list-missing"})
    check("missing grade -> 400", r.status_code == 400, str(r.status_code))
    r = client.post("/v1/listings", json={**base, "grade": "D"},
                    headers={**tok(SARITA),
                             "Idempotency-Key": "nov12-list-grade"})
    check("grade D -> 400", r.status_code == 400, str(r.status_code))
    ghost_body = {**base, "farmer_phone": "9841999999"}
    r = client.post("/v1/listings", json=ghost_body,
                    headers={**tok("9841999999"),
                             "Idempotency-Key": "nov12-list-farmer"})
    check("unknown farmer_phone -> 404", r.status_code == 404,
          str(r.status_code))
    r = client.post("/v1/listings", json={**base, "crop_code": "XXXX"},
                    headers={**tok(SARITA),
                             "Idempotency-Key": "nov12-list-crop"})
    check("unknown crop_code -> 400", r.status_code == 400,
          str(r.status_code))
    r = client.post("/v1/listings", json=base,
                    headers={**tok(SARITA),
                             "Idempotency-Key": "nov12-list-ok"})
    check("valid listing still 201", r.status_code == 201,
          r.get_data(as_text=True))

    # ---------- 4. get_lot / confirm_lot 404s ----------
    print("\n=== 4. get_lot / confirm_lot: unknown-lot 404s ===")
    r = client.get("/v1/lots/NOPE-1")
    check("GET unknown lot -> 404", r.status_code == 404, str(r.status_code))
    r = client.post("/v1/lots/NOPE-1/confirm",
                    json={"rate_per_kg": 50}, headers=tok(STAFF))
    check("confirm unknown lot -> 404", r.status_code == 404,
          str(r.status_code))
    r = client.get("/v1/lots/MST-012")
    j = r.get_json()
    check("GET known lot -> 200 with coop name",
          r.status_code == 200 and j["coop_name"] == "Kavre Vegetable Co-op",
          str(j)[:120])

    # ---------- 5. make_offer error branches ----------
    print("\n=== 5. make_offer: missing fields / lot 404 / lot closed ===")
    offer_base = {"buyer_name": "Bidder", "buyer_phone": BUYER,
                  "lot_id": "MST-012", "bid_rate_per_kg": 50,
                  "quantity_kg": 5}
    r = client.post("/v1/offers",
                    json={k: v for k, v in offer_base.items()
                          if k != "bid_rate_per_kg"},
                    headers=tok(BUYER))
    check("missing bid_rate_per_kg -> 400", r.status_code == 400,
          str(r.status_code))
    r = client.post("/v1/offers", json={**offer_base, "lot_id": "NOPE-1"},
                    headers=tok(BUYER))
    check("offer on unknown lot -> 404", r.status_code == 404,
          str(r.status_code))
    conn = db.get_db()
    conn.execute(
        "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, "
        "pooled_kg, rate_per_kg, status) "
        "VALUES ('SOLD-1','kavre-veg','GOLB','A',100,100,50,'sold')")
    conn.commit()
    conn.close()
    r = client.post("/v1/offers", json={**offer_base, "lot_id": "SOLD-1"},
                    headers=tok(BUYER))
    check("offer on sold lot -> 409", r.status_code == 409,
          str(r.status_code))
    r = client.post("/v1/offers", json=offer_base, headers=tok(BUYER))
    check("offer on ready lot still 201", r.status_code == 201,
          r.get_data(as_text=True))

    # ---------- 6. decide_offer: rejected + invalid decision ----------
    print("\n=== 6. decide_offer: rejected branch + invalid decision ===")
    conn = db.get_db()
    orders_before = conn.execute(
        "SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
    conn.close()
    r = client.post("/v1/offers", json=offer_base, headers=tok(BUYER))
    offer_a = r.get_json()["offer_id"]
    r = client.post(f"/v1/offers/{offer_a}/decide",
                    json={"decision": "rejected"}, headers=tok(STAFF))
    check("reject -> 200", r.status_code == 200, r.get_data(as_text=True))
    conn = db.get_db()
    status = conn.execute(
        "SELECT status FROM offers WHERE id=?", (offer_a,)).fetchone()[0]
    orders_after = conn.execute(
        "SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
    conn.close()
    check("rejected offer stays rejected, no order created",
          status == "rejected" and orders_after == orders_before,
          f"status={status} orders {orders_before}->{orders_after}")
    r = client.post("/v1/offers", json=offer_base, headers=tok(BUYER))
    offer_b = r.get_json()["offer_id"]
    r = client.post(f"/v1/offers/{offer_b}/decide",
                    json={"decision": "countered", "counter_rate_per_kg": 52},
                    headers=tok(STAFF))
    check("counter -> 200", r.status_code == 200, r.get_data(as_text=True))
    r = client.post(f"/v1/offers/{offer_b}/decide",
                    json={"decision": "rejected"}, headers=tok(STAFF))
    check("countered offer can be rejected -> 200",
          r.status_code == 200, r.get_data(as_text=True))
    conn = db.get_db()
    status = conn.execute(
        "SELECT status FROM offers WHERE id=?", (offer_b,)).fetchone()[0]
    conn.close()
    check("countered->rejected recorded, still no order",
          status == "rejected", f"status={status}")
    r = client.post("/v1/offers", json=offer_base, headers=tok(BUYER))
    offer_c = r.get_json()["offer_id"]
    r = client.post(f"/v1/offers/{offer_c}/decide",
                    json={"decision": "maybe"}, headers=tok(STAFF))
    check("invalid decision -> 400", r.status_code == 400,
          str(r.status_code))
    conn = db.get_db()
    status = conn.execute(
        "SELECT status FROM offers WHERE id=?", (offer_c,)).fetchone()[0]
    conn.close()
    check("invalid decision leaves the offer pending",
          status == "pending", f"status={status}")

    # ---------- 7. Settlement reference cap ----------
    print("\n=== 7. place_order_locked quantity guard + settle ===")
    r = client.post("/v1/orders",
                    json={"buyer_name": "Buyer", "buyer_phone": BUYER,
                          "lot_id": "MST-012", "quantity_kg": 200},
                    headers={**tok(BUYER),
                             "Idempotency-Key": "nov12-order-too-big"})
    check("200kg against a 150kg lot -> 400, not an oversell",
          r.status_code == 400 and
          "exceeds" in r.get_json().get("error", ""), str(r.status_code))
    r = client.post("/v1/orders",
                    json={"buyer_name": "Buyer", "buyer_phone": BUYER,
                          "lot_id": "MST-012", "quantity_kg": 5},
                    headers={**tok(BUYER),
                             "Idempotency-Key": "nov12-settle-order"})
    check("order for settlement created", r.status_code == 201,
          r.get_data(as_text=True))
    order_id = r.get_json()["order_id"]
    r = client.post(f"/v1/orders/{order_id}/settle",
                    json={"amount": 690, "method": "cash",
                          "reference": "x" * 501}, headers=tok(STAFF))
    check("501-char reference -> 400", r.status_code == 400,
          str(r.status_code))
    r = client.post(f"/v1/orders/{order_id}/settle",
                    json={"amount": 690, "method": "cash",
                          "reference": "TXN-1"}, headers=tok(STAFF))
    j = r.get_json()
    check("valid settle -> 201 settled",
          r.status_code == 201 and j["payment_status"] == "settled",
          str(j)[:120])
    r = client.post(f"/v1/orders/{order_id}/settle",
                    json={"amount": 690, "method": "cash"},
                    headers=tok(STAFF))
    check("settling twice -> 409", r.status_code == 409, str(r.status_code))

    # ---------- 8. price_trends bad days ----------
    print("\n=== 8. price_trends: non-numeric days falls back to 30 ===")
    r = client.get("/v1/prices/trends?days=abc")
    check("days=abc -> 200 (not 500)", r.status_code == 200,
          str(r.status_code))
    r = client.get("/v1/prices/trends?crop_code=GOLB&days=7")
    check("days=7 + crop filter -> 200", r.status_code == 200,
          str(r.status_code))

    # ---------- 9. lots_page filters + list_offers lot filter ----------
    print("\n=== 9. /v1/lots filters + /v1/offers lot filter ===")
    conn = db.get_db()
    ready_kavre = conn.execute(
        "SELECT COUNT(*) AS c FROM lots WHERE coop_id='kavre-veg' "
        "AND status='ready'").fetchone()["c"]
    conn.close()
    r = client.get("/v1/lots?coop_id=kavre-veg&status=ready")
    j = r.get_json()
    check("coop+status filter matches the DB count",
          j["total"] == ready_kavre and
          all(row["status"] == "ready" for row in j["data"]),
          f"total={j['total']} expected={ready_kavre}")
    r = client.get("/v1/lots?status=settled")
    j = r.get_json()
    check("no lot has status settled -> empty page",
          j["total"] == 0 and j["data"] == [], str(j)[:80])
    r = client.post("/v1/offers",
                    json={**offer_base, "lot_id": "KVR-118"},
                    headers=tok(BUYER))
    check("offer on KVR-118 created", r.status_code == 201,
          r.get_data(as_text=True))
    r = client.get("/v1/offers?lot_id=KVR-118", headers=tok(STAFF))
    j = r.get_json()
    check("offer list filtered by lot_id",
          len(j) >= 1 and all(o["lot_id"] == "KVR-118" for o in j),
          f"rows={len(j)}")
    r = client.get("/v1/offers?lot_id=MST-012", headers=tok(STAFF))
    j = r.get_json()
    check("other lot's filter excludes KVR-118 offers",
          all(o["lot_id"] == "MST-012" for o in j), f"rows={len(j)}")

    # ---------- 10. farmer_dues zero-pooled guard ----------
    print("\n=== 10. farmer_dues skips a zero-pooled lot (no /0) ===")
    conn = db.get_db()
    before = repositories.farmer_dues(conn, SARITA)
    cur = conn.execute(
        "INSERT INTO listings (farmer_phone, coop_id, crop_code, grade, "
        "quantity_kg) VALUES (?,?,?,?,?)",
        (SARITA, "kavre-veg", "GOLB", "A", 40))
    listing_id = cur.lastrowid
    conn.execute(
        "INSERT INTO lots (id, coop_id, crop_code, grade, target_kg, "
        "pooled_kg, rate_per_kg, status) "
        "VALUES ('ZERO-1','kavre-veg','GOLB','A',40,0,NULL,'forming')")
    conn.execute(
        "INSERT INTO lot_listings (lot_id, listing_id, kg_contributed) "
        "VALUES ('ZERO-1',?,40)", (listing_id,))
    conn.execute(
        "INSERT INTO orders (buyer_name, buyer_phone, lot_id, "
        "quantity_kg, unit_rate) VALUES ('Buyer',?, 'ZERO-1', 10, 50)",
        (BUYER,))
    conn.commit()
    after = repositories.farmer_dues(conn, SARITA)
    check("zero-pooled order contributes nothing, no exception",
          after == before, f"before={before} after={after}")
    check("farmer_pending_due matches farmer_dues()['pending']",
          repositories.farmer_pending_due(conn, SARITA) ==
          after["pending"])
    conn.close()

    # ---------- 11. routes_farmer error branches ----------
    print("\n=== 11. land-scan confidence / crop-doctor / history ===")
    r = client.post(f"/v1/farmers/{SARITA}/land-scan",
                    json={"land_type": "khet", "confidence": "maybe"},
                    headers=tok(SARITA))
    check("land-scan confidence 'maybe' -> 400", r.status_code == 400,
          str(r.status_code))
    r = client.post("/v1/crop-doctor/diagnose",
                    json={"crop_code": "XXXX",
                          "symptoms": "leaves turning yellow"},
                    headers=tok(SARITA))
    check("crop-doctor unknown crop_code -> 400", r.status_code == 400,
          str(r.status_code))
    r = client.get("/v1/crop-doctor/history", headers=tok(SARITA))
    check("diagnosis history without farmer_phone -> 400",
          r.status_code == 400, str(r.status_code))

    # ---------- 11b. signup validation branches ----------
    print("\n=== 11b. signup: phone / name / payout_method validation ===")
    r = client.post("/v1/auth/signup",
                    json={"phone": "123", "name": "Short",
                          "role": "farmer", "coop_id": "kavre-veg"},
                    headers=tok("123"))
    check("phone shorter than 6 digits -> 400", r.status_code == 400,
          str(r.status_code))
    r = client.post("/v1/auth/signup",
                    json={"phone": SARITA, "role": "farmer",
                          "coop_id": "kavre-veg"},
                    headers=tok(SARITA))
    check("missing name -> 400", r.status_code == 400, str(r.status_code))
    r = client.post("/v1/auth/signup",
                    json={"phone": SARITA, "name": "Sarita",
                          "role": "farmer", "coop_id": "kavre-veg",
                          "payout_method": "x" * 65},
                    headers=tok(SARITA))
    check("65-char payout_method -> 400", r.status_code == 400,
          str(r.status_code))

    # ---------- 12. Staff route for a co-op that no longer exists ----------
    print("\n=== 12. /v1/coop/route: missing co-op -> 404 ===")
    conn = db.get_db()
    check("routing.collection_route is None for unknown co-op",
          routing.collection_route(conn, "ghost-coop") is None)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO farmers (phone, name, coop_id, lat, lon, is_staff) "
        "VALUES (?,?,?,?,?,1)", (GHOST, "Ghost Desk", "ghost-coop",
                                 27.6, 85.5))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()
    conn.close()
    r = client.get("/v1/coop/route", headers=tok(GHOST))
    check("staff whose co-op row is gone -> 404", r.status_code == 404,
          str(r.status_code))
    conn = db.get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM farmers WHERE phone=?", (GHOST,))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()
    conn.close()

    # ---------- 13. auth.verify_token: expired + malformed ----------
    print("\n=== 13. verify_token: expired + malformed payloads ===")

    def sign(payload_b64):
        return hmac.new(auth._secret_bytes(), payload_b64.encode("ascii"),
                        hashlib.sha256).hexdigest()

    expired = {"phone": SARITA, "exp": int(time.time()) - 10}
    pb = auth._b64(json.dumps(expired, separators=(",", ":")).encode())
    check("expired token -> None",
          auth.verify_token(f"{pb}.{sign(pb)}") is None)
    pb_bad = auth._b64(b"not-json")
    check("signed non-JSON payload -> None",
          auth.verify_token(f"{pb_bad}.{sign(pb_bad)}") is None)
    pb_list = auth._b64(json.dumps(["x"]).encode())
    check("signed JSON list payload -> None",
          auth.verify_token(f"{pb_list}.{sign(pb_list)}") is None)
    good = auth.issue_token(SARITA)
    check("tampered signature -> None",
          auth.verify_token(good[:-1] + ("0" if good[-1] != "0" else "1"))
          is None)
    check("fresh token still verifies",
          auth.verify_token(good) == SARITA)

    # ---------- 14. messaging helpers ----------
    print("\n=== 14. messaging: normalize + no-config send_otp ===")
    check("bare Nepali mobile -> E.164",
          messaging.normalize_phone_for_messaging("9841000001") ==
          "+9779841000001")
    check("E.164 passes through",
          messaging.normalize_phone_for_messaging("+9779841000001") ==
          "+9779841000001")
    check("10 digits not starting with 9 pass through unchanged",
          messaging.normalize_phone_for_messaging("1234567890") ==
          "1234567890")
    saved = {k: os.environ.pop(k, None) for k in
             ("KISAN_MESSAGING_API_URL", "KISAN_MESSAGING_API_KEY")}
    try:
        check("send_otp without gateway config -> False",
              messaging.send_otp("9841000001", "123456") is False)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

    # ---------- 15. assistant: follow-up, scan, agronomy, sessions ----------
    print("\n=== 15. assistant: follow-up / scan / agronomy / sessions ===")
    check("follow-up overrides a scored intent (price 0.8)",
          assistant.classify_intent(
              "and potato weather?", {"last_intent": "price"}) ==
          ("price", 0.8))
    check("short follow-up keeps the plant context",
          assistant.classify_intent(
              "potato weather?", {"last_intent": "plant"}) ==
          ("plant", 0.8))
    check("same text without context scores weather, not price",
          assistant.classify_intent("and potato weather?", {})[0] ==
          "weather")
    check("roman crop alias detects as Romanized Nepali",
          assistant.detect_language("aalu") == "ro")
    conn = db.get_db()
    r1 = assistant.chat("nov12-carry", "tomato ko bhav?", db_conn=conn)
    check("price question -> price intent", r1.get("intent") == "price",
          str(r1.get("intent")))
    r2 = assistant.chat("nov12-carry", "ani?", db_conn=conn)
    check("'ani?' carries the tomato price context over",
          r2.get("intent") == "price" and "Tomato" in r2.get("reply", ""),
          r2.get("reply", "")[:80])
    r3 = assistant.chat("nov12-scan", "land scan garnu paryo", db_conn=conn)
    check("'land scan' -> scan intent", r3.get("intent") == "scan",
          str(r3.get("intent")))
    r4 = assistant.chat("nov12-agro", "tomato ma blight lagyo",
                        db_conn=conn)
    check("tomato blight -> agronomy with remedies",
          r4.get("intent") == "agronomy" and
          "blight" in r4.get("reply", "").lower(),
          r4.get("reply", "")[:80])
    r5 = assistant.chat("nov12-agro2", "maize ma rog lagyo", db_conn=conn)
    check("crop outside the KB asks which crop",
          "Which crop" in r5.get("reply", "") or
          "Kun balima" in r5.get("reply", ""),
          r5.get("reply", "")[:80])
    r6 = assistant.chat("nov12-plant", "ke lagaune?", db_conn=conn)
    check("plant question with no crop -> generic advice",
          r6.get("intent") == "plant" and
          ("sallah tab" in r6.get("reply", "") or
           "Advice tab" in r6.get("reply", "")),
          r6.get("reply", "")[:80])
    conn.close()
    # Session bounds: TTL sweep + 2000-session FIFO eviction.
    assistant._sessions.clear()
    assistant._last_sweep = 0
    assistant._sessions["nov12-old"] = {
        "last_intent": None, "last_crops": [], "turns": 1,
        "updated": time.time() - 2000}
    assistant.get_context("nov12-fresh")
    check("expired session swept on next call",
          "nov12-old" not in assistant._sessions)
    assistant._sessions.clear()
    for i in range(2005):
        assistant.get_context(f"nov12-s{i}")
    check("2005 sessions capped at exactly 2000",
          len(assistant._sessions) == 2000,
          f"len={len(assistant._sessions)}")
    check("oldest session evicted, newest kept",
          "nov12-s0" not in assistant._sessions and
          "nov12-s2004" in assistant._sessions)
    assistant._sessions.clear()

    # ---------- 16. web idempotency: stuck claim, exception, store ----------
    print("\n=== 16. idempotent: stuck claim 409 / exception release ===")
    stub = Flask(__name__)
    calls = {"n": 0}

    @stub.route("/idem-stuck", methods=["POST"])
    @web.idempotent
    def idem_stuck():
        calls["n"] += 1
        return jsonify({"ok": True}), 200

    @stub.route("/idem-boom", methods=["POST"])
    @web.idempotent
    def idem_boom():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return jsonify({"ok": True}), 200

    sclient = stub.test_client()
    conn = db.get_db()
    conn.execute(
        "INSERT INTO idempotency_keys (key, endpoint, status_code, "
        "response_body) VALUES ('nov12-stuck','/idem-stuck',0,'')")
    conn.commit()
    conn.close()
    t0 = time.time()
    r = sclient.post("/idem-stuck",
                     headers={"Idempotency-Key": "nov12-stuck"})
    elapsed = time.time() - t0
    check("stuck claim waits then answers 409, handler never runs",
          r.status_code == 409 and calls["n"] == 0 and elapsed >= 9,
          f"status={r.status_code} calls={calls['n']} "
          f"elapsed={elapsed:.1f}s")
    r = sclient.post("/idem-boom",
                     headers={"Idempotency-Key": "nov12-boom"})
    check("handler exception -> 500", r.status_code == 500,
          str(r.status_code))
    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM idempotency_keys WHERE key='nov12-boom'").fetchone()
    conn.close()
    check("claim released after the handler raised", row is None)
    r = sclient.post("/idem-boom",
                     headers={"Idempotency-Key": "nov12-boom"})
    check("retry after exception runs fresh -> 200",
          r.status_code == 200 and calls["n"] == 2,
          f"status={r.status_code} calls={calls['n']}")
    with stub.test_request_context("/idem-x"):
        action = web._idem_claim("nov12-store")
        assert action[0] == "run", action

        class BadResp:
            def get_data(self, as_text=False):
                raise RuntimeError("no body")

        web._idem_store("nov12-store", (BadResp(), 200))
    conn = db.get_db()
    row = conn.execute(
        "SELECT status_code, response_body FROM idempotency_keys "
        "WHERE key='nov12-store'").fetchone()
    conn.close()
    check("unreadable response body stored as empty, status kept",
          row["status_code"] == 200 and row["response_body"] == "",
          dict(row))
    check("_resp_status: (body, 201) -> 201",
          web._resp_status(({}, 201)) == 201)
    check("_resp_status: bare tuple -> 200",
          web._resp_status(({},)) == 200)
    check("_resp_status: object without status_code -> 200",
          web._resp_status(object()) == 200)

    # ---------- 17. app plumbing: error handler + CORS ----------
    print("\n=== 17. app: generic error handler + CORS echo ===")
    r = client.get("/v1/definitely-not-a-route")
    check("unknown route -> JSON 404 via the generic handler",
          r.status_code == 404 and "not found" in
          r.get_json()["error"].lower(), r.get_data(as_text=True)[:100])
    r = client.post("/v1/health")
    check("wrong method -> JSON 405 via the HTTPException branch",
          r.status_code == 405 and "method" in
          r.get_json()["error"].lower(), r.get_data(as_text=True)[:100])
    with app_module.app.test_request_context():
        resp, status = app_module.handle_unexpected_error(
            RuntimeError("boom"))
        check("unexpected exception -> generic JSON 500",
              status == 500 and
              resp.get_json()["error"] == "internal server error",
              str(status))
    r = client.get("/v1/health", headers={"Origin": "http://localhost:5050"})
    check("allowed Origin echoed back with Vary",
          r.headers.get("Access-Control-Allow-Origin") ==
          "http://localhost:5050" and
          r.headers.get("Vary") == "Origin",
          str(dict(r.headers)))
    r = client.get("/v1/health",
                   headers={"Origin": "https://evil.example"})
    check("disallowed Origin gets no CORS header",
          r.headers.get("Access-Control-Allow-Origin") is None)

    # ---------- 18. Order/offer rate limits — LAST (shared per-IP) ----------
    print("\n=== 18. order + offer rate limits (10/10min per IP) ===")
    auth._attempts.clear()
    for i in range(10):
        r = client.post("/v1/orders",
                        json={"buyer_name": "Buyer", "buyer_phone": BUYER,
                              "lot_id": "MST-012", "quantity_kg": 1},
                        headers={**tok(BUYER),
                                 "Idempotency-Key": f"nov12-rate-o{i}"})
        assert r.status_code == 201, (i, r.status_code,
                                      r.get_data(as_text=True))
    check("10 orders from one IP succeed", True)
    r = client.post("/v1/orders",
                    json={"buyer_name": "Buyer", "buyer_phone": BUYER,
                          "lot_id": "MST-012", "quantity_kg": 1},
                    headers={**tok(BUYER),
                             "Idempotency-Key": "nov12-rate-o10"})
    check("11th order -> 429", r.status_code == 429, str(r.status_code))
    for i in range(10):
        r = client.post("/v1/offers", json=offer_base, headers=tok(BUYER))
        assert r.status_code == 201, (i, r.status_code,
                                      r.get_data(as_text=True))
    check("10 offers from one IP succeed (separate bucket)", True)
    r = client.post("/v1/offers", json=offer_base, headers=tok(BUYER))
    check("11th offer -> 429", r.status_code == 429, str(r.status_code))

    # ---------- 19. Admin login with the staff row missing ----------
    print("\n=== 19. admin login: missing staff account -> 500 ===")
    # The handler reads the admin creds from the environment at call
    # time; set them explicitly so this does not depend on demo mode.
    saved_admin = {k: os.environ.get(k) for k in
                   ("KISAN_ADMIN_USER", "KISAN_ADMIN_PASS")}
    os.environ["KISAN_ADMIN_USER"] = "admin"
    os.environ["KISAN_ADMIN_PASS"] = "kisan123"
    try:
        conn = db.get_db()
        conn.execute("DELETE FROM farmers WHERE phone=?", (STAFF,))
        conn.commit()
        conn.close()
        r = client.post("/v1/auth/admin",
                        json={"username": "admin", "password": "kisan123"})
        check("correct creds but no staff row -> honest 500",
              r.status_code == 500 and
              r.get_json()["error"] == "admin account not set up",
              f"status={r.status_code} body={r.get_data(as_text=True)[:80]}")
        conn = db.get_db()
        conn.execute(
            "INSERT INTO farmers (phone, name, role, coop_id, lat, lon, "
            "is_staff) VALUES (?,?,?,?,?,?,1)",
            (STAFF, "Kavre Co-op Desk", "staff", "kavre-veg",
             27.6215, 85.5442))
        conn.commit()
        conn.close()
        r = client.post("/v1/auth/admin",
                        json={"username": "admin", "password": "kisan123"})
        check("staff row restored -> admin login works again",
              r.status_code == 200 and r.get_json().get("phone") == STAFF,
              f"status={r.status_code}")
    finally:
        for k, v in saved_admin.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print(f"\n{passed} passed, 0 failed.")


if __name__ == "__main__":
    run()
