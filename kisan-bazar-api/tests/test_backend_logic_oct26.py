"""
Backend-logic regression tests for the 2026-10-26 daily-improvement
fixes (focus: backend logic). Three real bugs, all in request paths
that previously trusted their inputs or their read timing:

  1. POST /v1/lots/<id>/confirm raced: two staff confirming the same
     'forming' lot both passed the pre-lock status check, and the
     second silently overwrote the first's rate. The handler now
     takes BEGIN IMMEDIATE before reading, so the loser gets 409
     and the winning rate stands.
  2. GET /v1/lots?limit=-1 bypassed the pagination cap: SQLite
     treats a negative LIMIT as "no limit", dumping the whole table
     despite the max-200 clamp. limit is now clamped to 1..200.
  3. POST /v1/rentals/<id>/book accepted bookings in the past (and
     silently truncated float days: int(2.9) -> 2). Past dates now
     get 400, measured against Nepal time; float/bool days get 400.

Run: python3 tests/test_backend_logic_oct26.py
"""
import sys
import os
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seed
import auth
import app as app_module

STAFF = "9801100000"
SARITA = "9841000001"
RAM = "9841000002"
KRISHNA = "9841000004"

passed = 0


def check(name, cond, extra=""):
    global passed
    assert cond, f"{name} {extra}"
    passed += 1
    print(f"  PASS — {name}")


def tok(phone):
    return {"Authorization": f"Bearer {auth.issue_token(phone)}"}


def form_lot(client):
    """Listings totalling 134kg against the 130kg threshold, as in
    test_api.py — returns the formed lot id (status 'forming')."""
    for phone, kg in ((SARITA, 38), (RAM, 52), (KRISHNA, 44)):
        r = client.post("/v1/listings", json={
            "farmer_phone": phone, "coop_id": "kavre-veg",
            "crop_code": "KAULI", "grade": "A", "quantity_kg": kg,
        }, headers=tok(phone))
        assert r.status_code == 201, r.get_json()
        if "lot_formed" in r.get_json():
            return r.get_json()["lot_formed"]["lot_id"]
    raise AssertionError("no lot formed from the listings")


def run():
    seed.seed()
    client = app_module.app.test_client()

    # ---------- A. Lot pagination: negative / zero / huge limits ----------
    print("\n=== A. /v1/lots limit clamping ===")
    r = client.get("/v1/lots?limit=-1")
    body = r.get_json()
    check("limit=-1 clamped to 1 (was: whole table)",
          r.status_code == 200 and body["limit"] == 1
          and len(body["data"]) <= 1, body)
    r = client.get("/v1/lots?limit=0")
    check("limit=0 clamped to 1",
          r.status_code == 200 and r.get_json()["limit"] == 1, r.get_json())
    r = client.get("/v1/lots?limit=99999")
    check("limit=99999 capped at 200",
          r.status_code == 200 and r.get_json()["limit"] == 200, r.get_json())
    r = client.get("/v1/lots?limit=abc")
    check("non-numeric limit -> 400", r.status_code == 400, r.get_json())

    # ---------- B. Rental booking: past dates + float/bool days ----------
    print("\n=== B. Rental booking validation ===")
    # Seeded rentals are ids 1-4; rental 1 is available.
    r = client.post("/v1/rentals/1/book",
                    json={"days": 2, "book_date": "2020-01-01"},
                    headers=tok(SARITA))
    check("past book_date -> 400", r.status_code == 400, r.get_json())
    r = client.post("/v1/rentals/1/book",
                    json={"days": 2.9, "book_date": "2026-12-01"},
                    headers=tok(SARITA))
    check("float days -> 400 (was: silently truncated to 2)",
          r.status_code == 400, r.get_json())
    r = client.post("/v1/rentals/1/book",
                    json={"days": True, "book_date": "2026-12-01"},
                    headers=tok(SARITA))
    check("bool days -> 400", r.status_code == 400, r.get_json())
    r = client.post("/v1/rentals/1/book",
                    json={"days": 2, "book_date": "2026-12-01"},
                    headers=tok(SARITA))
    check("future book_date still books (201)",
          r.status_code == 201, r.get_json())
    booking_id = r.get_json()["booking_id"]
    r = client.post(f"/v1/rentals/bookings/{booking_id}/cancel",
                    headers=tok(SARITA))
    check("cleanup: cancel the future booking", r.status_code == 200,
          r.get_json())

    # ---------- C. Concurrent lot confirms: exactly one winner ----------
    print("\n=== C. Concurrent lot confirm race ===")
    lot_id = form_lot(client)
    results = []

    def confirmer(rate):
        c = app_module.app.test_client()
        r = c.post(f"/v1/lots/{lot_id}/confirm", json={"rate_per_kg": rate},
                   headers=tok(STAFF))
        results.append((rate, r.status_code))

    threads = [threading.Thread(target=confirmer, args=(rate,))
               for rate in (58, 99)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = sorted(code for _, code in results)
    check("one confirm wins (200), the other gets 409",
          codes == [200, 409], results)
    winner_rate = next(rate for rate, code in results if code == 200)
    r = client.get(f"/v1/lots/{lot_id}")
    lot = r.get_json()
    check("lot is 'ready' at exactly the winner's rate",
          lot["status"] == "ready" and lot["rate_per_kg"] == winner_rate,
          lot)
    r = client.post(f"/v1/lots/{lot_id}/confirm",
                    json={"rate_per_kg": 77}, headers=tok(STAFF))
    check("sequential re-confirm still 409", r.status_code == 409,
          r.get_json())

    print(f"\n{passed} passed, 0 failed.")


if __name__ == "__main__":
    run()
