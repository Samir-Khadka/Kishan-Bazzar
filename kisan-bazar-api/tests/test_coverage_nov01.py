"""
Coverage sweep for 2026-11-01 (focus: test coverage).

Two parts:
  A. A real bug found while probing uncovered branches: NaN/Infinity
     passed the money-path validators (`rate <= 0`-style checks are
     always False for NaN). NaN/Inf are now rejected with 400 via the
     new web.valid_positive_number() helper on: confirm_lot rate,
     make_offer rate, decide_offer counter rate, record_settlement
     amount, create_rental rate_per_day. (NaN stored in SQLite became
     NULL on INSERT -> NOT NULL violation -> unhandled 500 on rentals,
     and invalid-JSON totals elsewhere.)
  B. Regression checks for previously untested but real branches:
     idempotency-key length cap, serve_site path-safety, signup
     validation + update-existing, /v1/me 404, admin-login 401 paths,
     request-code/verify 400s, dev latest-code 404, assistant intent
     routing + the honest price/ledger failure paths.

Run: python3 tests/test_coverage_nov01.py
"""
import sys
import os

# DEMO_MODE must be set before the app module is imported: it switches on
# the /v1/dev/latest-code branch and the default admin credentials used below.
os.environ["KISAN_BAZAR_DEMO_MODE"] = "1"
os.environ["KISAN_ADMIN_USER"] = "admin"
os.environ["KISAN_ADMIN_PASS"] = "kisan123"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seed
import db
import auth
import repositories
import app as app_module
import web
import assistant

SARITA = "9841000001"
STAFF = "9801100000"
GHOST = "9841000999"   # valid-format phone with NO farmer record
GHOST2 = "9841000888"  # second unused phone for the /v1/me 404 check

passed = 0


def check(name, cond, extra=""):
    global passed
    assert cond, f"{name} {extra}"
    passed += 1
    print(f"  PASS — {name}")


def tok(phone):
    return {"Authorization": f"Bearer {auth.issue_token(phone)}"}


def forming_lot(client, lot_id):
    conn = db.get_db()
    conn.execute(
        "INSERT INTO lots (id, coop_id, crop_code, grade, pooled_kg, target_kg, status) "
        "VALUES (?, 'kavre-veg', 'KAULI', 'A', 130, 130, 'forming')", (lot_id,))
    conn.commit()
    conn.close()


def ready_lot(client, lot_id, rate=45):
    conn = db.get_db()
    conn.execute(
        "INSERT INTO lots (id, coop_id, crop_code, grade, pooled_kg, target_kg, rate_per_kg, status) "
        "VALUES (?, 'kavre-veg', 'KAULI', 'A', 130, 130, ?, 'ready')", (lot_id, rate))
    conn.commit()
    conn.close()


def run():
    seed.seed()
    client = app_module.app.test_client()
    auth._attempts.clear()

    print("== A. valid_positive_number unit ==")
    v = web.valid_positive_number
    check("NaN rejected", v(float("nan")) is False)
    check("Inf rejected", v(float("inf")) is False)
    check("-Inf rejected", v(float("-inf")) is False)
    check("0 rejected", v(0) is False)
    check("negative rejected", v(-3) is False)
    check("bool rejected", v(True) is False)
    check("string rejected", v("5") is False)
    check("None rejected", v(None) is False)
    check("positive int ok", v(5) is True)
    check("positive float ok", v(0.5) is True)

    print("== B. money paths reject NaN/Inf ==")
    forming_lot(client, "NAN-LOT")
    r = client.post("/v1/lots/NAN-LOT/confirm", json={"rate_per_kg": float("nan")},
                    headers=tok(STAFF))
    check("confirm_lot NaN rate -> 400", r.status_code == 400, r.get_data(as_text=True))
    r = client.post("/v1/lots/NAN-LOT/confirm", json={"rate_per_kg": float("inf")},
                    headers=tok(STAFF))
    check("confirm_lot Inf rate -> 400", r.status_code == 400, r.get_data(as_text=True))
    r = client.post("/v1/lots/NAN-LOT/confirm", json={"rate_per_kg": 55}, headers=tok(STAFF))
    check("confirm_lot valid rate still -> 200", r.status_code == 200, r.get_data(as_text=True))

    ready_lot(client, "OFFER-LOT")
    offer_body = {"buyer_name": "B", "buyer_phone": SARITA, "lot_id": "OFFER-LOT",
                  "quantity_kg": 10}
    r = client.post("/v1/offers", json={**offer_body, "bid_rate_per_kg": float("nan")},
                    headers=tok(SARITA))
    check("make_offer NaN rate -> 400", r.status_code == 400, r.get_data(as_text=True))
    r = client.post("/v1/offers", json={**offer_body, "bid_rate_per_kg": 40,
                                       "quantity_kg": float("inf")}, headers=tok(SARITA))
    check("make_offer Inf qty -> 400", r.status_code == 400, r.get_data(as_text=True))
    r = client.post("/v1/offers", json={**offer_body, "bid_rate_per_kg": 40,
                                       "quantity_kg": float("nan")}, headers=tok(SARITA))
    check("make_offer NaN qty -> 400", r.status_code == 400, r.get_data(as_text=True))
    r = client.post("/v1/offers", json={**offer_body, "bid_rate_per_kg": 40},
                    headers=tok(SARITA))
    check("make_offer valid -> 201", r.status_code == 201, r.get_data(as_text=True))
    offer_id = r.get_json()["offer_id"]

    r = client.post(f"/v1/offers/{offer_id}/decide",
                    json={"decision": "countered", "counter_rate_per_kg": float("nan")},
                    headers=tok(STAFF))
    check("decide_offer NaN counter -> 400", r.status_code == 400, r.get_data(as_text=True))
    r = client.post(f"/v1/offers/{offer_id}/decide",
                    json={"decision": "countered", "counter_rate_per_kg": float("inf")},
                    headers=tok(STAFF))
    check("decide_offer Inf counter -> 400", r.status_code == 400, r.get_data(as_text=True))
    r = client.post(f"/v1/offers/{offer_id}/decide",
                    json={"decision": "countered", "counter_rate_per_kg": 50},
                    headers=tok(STAFF))
    check("decide_offer valid counter -> 200", r.status_code == 200, r.get_data(as_text=True))

    ready_lot(client, "SETL-LOT", rate=45)
    r = client.post("/v1/orders",
                    json={"buyer_name": "Hotel", "buyer_phone": SARITA,
                          "lot_id": "SETL-LOT", "quantity_kg": 100},
                    headers=tok(SARITA))
    check("seed order placed", r.status_code == 201, r.get_json())
    order_id = r.get_json()["order_id"]
    r = client.post(f"/v1/orders/{order_id}/settle",
                    json={"amount": float("nan"), "method": "cash"}, headers=tok(STAFF))
    check("settle NaN amount -> 400", r.status_code == 400, r.get_data(as_text=True))
    r = client.post(f"/v1/orders/{order_id}/settle",
                    json={"amount": float("inf"), "method": "cash"}, headers=tok(STAFF))
    check("settle Inf amount -> 400", r.status_code == 400, r.get_data(as_text=True))
    r = client.post(f"/v1/orders/{order_id}/settle",
                    json={"amount": 4500, "method": "cash"}, headers=tok(STAFF))
    check("settle valid amount still -> 201", r.status_code == 201, r.get_data(as_text=True))

    rental_body = {"machine_type": "tractor", "machine_name": "T", "location": "L",
                   "contact_phone": SARITA}
    r = client.post("/v1/rentals", json={**rental_body, "rate_per_day": float("nan")},
                    headers=tok(SARITA))
    check("create_rental NaN rate -> 400 (was unhandled 500)",
          r.status_code == 400, r.get_data(as_text=True))
    r = client.post("/v1/rentals", json={**rental_body, "rate_per_day": float("inf")},
                    headers=tok(SARITA))
    check("create_rental Inf rate -> 400", r.status_code == 400, r.get_data(as_text=True))
    r = client.post("/v1/rentals", json={**rental_body, "rate_per_day": 500},
                    headers=tok(SARITA))
    check("create_rental valid rate -> 201", r.status_code == 201, r.get_data(as_text=True))

    print("== C. idempotency key length cap ==")
    long_key = "k" * 129
    r = client.post("/v1/listings",
                    json={"farmer_phone": SARITA, "coop_id": "kavre-veg",
                          "crop_code": "KAULI", "grade": "A", "quantity_kg": 5},
                    headers={**tok(SARITA), "Idempotency-Key": long_key})
    check("129-char Idempotency-Key -> 400", r.status_code == 400, r.get_json())

    print("== D. serve_site path safety ==")
    r = client.get("/site/index.html")
    check("real page serves 200", r.status_code == 200)
    r = client.get("/site/%2e%2e/kisan-bazar.html")
    check("encoded ../ escape -> 404 (no directory escape)",
          r.status_code == 404, r.status_code)
    r = client.get("/site/x.exe")
    check("disallowed extension -> 404", r.status_code == 404)
    r = client.get("/site/no-such-page.html")
    check("missing file -> 404", r.status_code == 404)

    print("== E. auth edge branches ==")
    r = client.post("/v1/auth/request-code", json={})
    check("request-code without phone -> 400", r.status_code == 400, r.get_json())
    r = client.post("/v1/auth/verify", json={"phone": SARITA})
    check("verify without code -> 400", r.status_code == 400, r.get_json())
    r = client.get(f"/v1/dev/latest-code/{GHOST2}")
    check("dev latest-code unknown phone -> 404", r.status_code == 404, r.get_json())
    r = client.get("/v1/me", headers=tok(GHOST2))
    check("/v1/me with no profile -> 404", r.status_code == 404, r.get_json())

    r = client.post("/v1/auth/admin", json={"username": "wrong", "password": "kisan123"})
    check("admin login wrong username -> 401", r.status_code == 401, r.get_json())
    r = client.post("/v1/auth/admin", json={"username": "admin", "password": "wrong"})
    check("admin login wrong password -> 401", r.status_code == 401, r.get_json())

    base_signup = {"phone": GHOST, "name": "Ghost", "coop_id": "kavre-veg"}
    r = client.post("/v1/auth/signup", json={**base_signup, "lat": "abc"},
                    headers=tok(GHOST))
    check("signup non-numeric lat -> 400", r.status_code == 400, r.get_json())
    r = client.post("/v1/auth/signup", json={**base_signup, "land_type": "ocean"},
                    headers=tok(GHOST))
    check("signup unknown land_type -> 400", r.status_code == 400, r.get_json())
    r = client.post("/v1/auth/signup", json={**base_signup, "coop_id": "nope"},
                    headers=tok(GHOST))
    check("signup unknown coop -> 400", r.status_code == 400, r.get_json())
    r = client.post("/v1/auth/signup", json=base_signup, headers=tok(GHOST))
    check("signup valid -> 201", r.status_code == 201, r.get_json())
    r = client.post("/v1/auth/signup", json={**base_signup, "name": "Ghost Two"},
                    headers=tok(GHOST))
    body = r.get_json()
    check("re-signup updates existing farmer -> 201",
          r.status_code == 201 and body["name"] == "Ghost Two", body)
    conn = db.get_db()
    check("update branch persisted the new name",
          repositories.get_farmer(conn, GHOST)["name"] == "Ghost Two")
    conn.close()

    print("== F. web.py units ==")
    check("_idem_claim_age_seconds on garbage date -> 0.0",
          web._idem_claim_age_seconds({"created_at": "not-a-date"}) == 0.0)
    with app_module.app.test_request_context("/v1/orders", method="POST"):
        action = web._idem_claim("cov-key-1")
        check("_idem_claim wins on a fresh key", action[0] == "run", action)
        resp = app_module.app.response_class('{"ok": true}', 200,
                                             content_type="application/json")
        web._idem_store("cov-key-1", resp)
    conn = db.get_db()
    row = conn.execute(
        "SELECT status_code, response_body FROM idempotency_keys WHERE key='cov-key-1'").fetchone()
    check("_idem_store non-tuple response stores 200",
          row is not None and row["status_code"] == 200
          and row["response_body"] == '{"ok": true}', dict(row) if row else None)
    conn.execute("DELETE FROM idempotency_keys WHERE key='cov-key-1'")
    conn.commit()
    conn.close()

    print("== G. assistant routing + honest failures ==")
    conn = db.get_db()
    r = assistant.chat("c1", "आजको मौसम कस्तो छ?", conn)
    check("weather intent via chat", r["intent"] == "weather" and "नमुना" in r["reply"], r["intent"])
    r = assistant.chat("c2", "kasari bechne?", conn)
    check("sell intent via chat", r["intent"] == "sell", r["intent"])
    r = assistant.chat("c3", "आलु लगाउने सल्लाह", conn)
    check("plant intent names the crop",
          r["intent"] == "plant" and "आलु" in r["reply"], r["intent"])
    r = assistant.chat("c4", "धन्यवाद", conn)
    check("thanks intent via chat", r["intent"] == "thanks", r["intent"])
    r = assistant.chat("c5", "bye", conn)
    check("bye intent via chat", r["intent"] == "bye", r["intent"])
    r = assistant.chat("c6", "मद्दत चाहियो", conn)
    check("help intent via chat", r["intent"] == "help", r["intent"])
    r = assistant.chat("c7", "mero order ke cha?", conn, {"phone": SARITA})
    check("order with farmer: no login prompt",
          r["intent"] == "order" and "login" not in r["reply"].lower(), r["reply"][:60])
    r = assistant.chat("c8", "mero order ke cha?", conn)
    check("order without farmer: login prompt",
          r["intent"] == "order" and "login" in r["reply"].lower(), r["reply"][:60])
    r = assistant.chat("c9", "", conn)
    check("empty message -> clarify fallback",
          r["intent"] == "fallback" and "बुझिनँ" in r["reply"], r["intent"])
    r = assistant.chat("c10", "aalu ko bhau", conn)
    check("roman alias -> ro language", r["lang"] == "ro", r["lang"])
    r = assistant.chat("c11", "bhau", conn)
    check("price with no crop asks which crop",
          r["intent"] == "price" and "chahiyo" in r["reply"], r["reply"][:60])
    assistant.chat("c12", "aaluko bhau?", conn)
    r = assistant.chat("c12", "???", conn)
    check("context carryover keeps price intent + crops",
          r["intent"] == "price" and "Potato" in r["reply"], (r["intent"], r["reply"][:60]))
    r = assistant.chat("c13", "gahunma kira lagyo", conn)
    check("agronomy fallback loop when crop not in KB",
          r["intent"] == "agronomy" and "Which crop" in r["reply"], r["reply"][:60])

    orig = repositories.recent_lot_rates
    def _boom(conn, code, limit=2):
        raise RuntimeError("db down")
    repositories.recent_lot_rates = _boom
    try:
        r = assistant.chat("c14", "aaluko bhau?", conn)
    finally:
        repositories.recent_lot_rates = orig
    check("price lookup failure is honest, never invented",
          r["intent"] == "price" and "upalabdha chhaina" in r["reply"], r["reply"][:60])

    r = assistant.chat("c15", "mero hisab", None, {"phone": SARITA})
    check("ledger failure is honest, never 'nothing owed'",
          "sakiena" in r["reply"] and "baaki chhaina" not in r["reply"], r["reply"][:60])
    conn.close()

    print(f"\n{passed} checks passed.")


if __name__ == "__main__":
    run()
